"""飞书适配器：企业自建应用，事件订阅 + 互动卡片回调（方案第 6 章）。

- 收：HTTPS 事件回调（POST /v1/channels/feishu/{tenant_id}/webhook），支持 URL 校验、SHA-256 签名校验、
  AES-256-CBC 事件解密、文本/图片/文件/富文本/语音消息解析与互动卡片按钮回调。
- 发：飞书开放平台 OpenAPI（/open-apis/im/v1/messages），原生支持 JSON 互动卡片与按钮回传。

凭据只从环境变量读取，不进代码、不进租户配置、不进模型上下文：
  FOS_FEISHU_<TENANT>_APP_ID / _APP_SECRET / _VERIFICATION_TOKEN（可选）/ _ENCRYPT_KEY（可选）
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from fos.channels.base import ChannelError
from fos.channels.cards import Card, to_markdown
from fos.channels.types import Attachment, CardAction, ConversationRef, InboundMessage

API = "https://open.feishu.cn"
_MENTION_RE = re.compile(r"@_user_\d+\s*")


@dataclass(frozen=True)
class FeishuApp:
    tenant_id: str
    app_id: str
    app_secret: str
    verification_token: str | None = None
    encrypt_key: str | None = None

    def __repr__(self) -> str:
        return f"FeishuApp(tenant_id={self.tenant_id!r}, app_id={self.app_id!r})"

    @classmethod
    def from_env(cls, tenant_id: str, env: dict[str, str] | None = None) -> FeishuApp | None:
        env = dict(os.environ) if env is None else env
        p = f"FOS_FEISHU_{tenant_id.upper()}_"
        aid, secret = env.get(p + "APP_ID", ""), env.get(p + "APP_SECRET", "")
        if not aid or not secret:
            return None
        return cls(
            tenant_id=tenant_id,
            app_id=aid,
            app_secret=secret,
            verification_token=env.get(p + "VERIFICATION_TOKEN") or None,
            encrypt_key=env.get(p + "ENCRYPT_KEY") or None,
        )


# ---------------- 签名校验与 AES-256-CBC 解密 ----------------


def verify_signature(encrypt_key: str, timestamp: str, nonce: str, raw_body: bytes, signature: str) -> bool:
    """校验飞书事件回调请求头 X-Lark-Signature（SHA-256）。"""
    if not (encrypt_key and timestamp and nonce and signature):
        return False
    digest = hashlib.sha256((timestamp + nonce + encrypt_key).encode("utf-8") + raw_body).hexdigest()
    return hmac.compare_digest(digest, signature)


def encrypt_event(encrypt_key: str, payload: dict[str, Any], *, iv: bytes | None = None) -> str:
    """按飞书事件加密规范加密 JSON（AES-256-CBC + PKCS7），主要供单测与工具使用。"""
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv_bytes = iv if iv is not None else os.urandom(16)
    padder = sym_padding.PKCS7(128).padder()
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    padded = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv_bytes)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(iv_bytes + ct).decode("ascii")


def decrypt_event(encrypt_key: str, encrypted_b64: str) -> dict[str, Any]:
    """按飞书事件加密规范解密 encrypt 字段。"""
    try:
        raw = base64.b64decode(encrypted_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ChannelError("飞书加密负载不是合法 Base64") from e
    if len(raw) < 32 or (len(raw) - 16) % 16 != 0:
        raise ChannelError("飞书加密负载长度非法")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv_bytes, ct = raw[:16], raw[16:]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv_bytes)).decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    try:
        plain = unpadder.update(padded) + unpadder.finalize()
        data = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ChannelError("飞书事件解密失败，请核对 ENCRYPT_KEY") from e
    if not isinstance(data, dict):
        raise ChannelError("飞书解密结果不是 JSON 对象")
    return data


# ---------------- 解析：飞书事件 / 卡片回调 → InboundMessage ----------------


def _ms(v: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _extract_post(content: dict[str, Any]) -> tuple[str, list[Attachment], str]:
    """解析飞书富文本（post）里的文字与内嵌图片。"""
    body = content.get("zh_cn") or content.get("en_us") or content
    parts: list[str] = []
    if body.get("title"):
        parts.append(str(body["title"]))
    img_keys: list[str] = []
    for row in body.get("content") or []:
        if not isinstance(row, list):
            continue
        row_text: list[str] = []
        for node in row:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "text" and node.get("text"):
                row_text.append(str(node["text"]))
            elif tag == "img" and node.get("image_key"):
                img_keys.append(str(node["image_key"]))
        if row_text:
            parts.append("".join(row_text))
    return "\n".join(parts), [Attachment(kind="image", ref=k, filename="photo.jpg") for k in img_keys], "rich"


def parse_event(tenant_id: str, body: dict[str, Any]) -> InboundMessage | None:
    """将飞书事件（v2.0 im.message.receive_v1 / card.action.trigger）或旧版卡片回调转为 InboundMessage。"""
    header = body.get("header") or {}
    event_type = header.get("event_type") or ""
    event = body.get("event") or {}

    if event_type == "im.message.receive_v1":
        return _parse_message_event(tenant_id, event)
    if event_type == "card.action.trigger":
        return _parse_card_v2(tenant_id, header, event)
    # 兼容飞书旧版互动卡片回调（顶层含 action + open_id）
    if isinstance(body.get("action"), dict) and body.get("open_id"):
        return _parse_card_legacy(tenant_id, body)
    return None


def _parse_message_event(tenant_id: str, event: dict[str, Any]) -> InboundMessage | None:
    msg = event.get("message") or {}
    sender = event.get("sender") or {}
    sid_obj = sender.get("sender_id") or {}
    sender_id = sid_obj.get("open_id") or sid_obj.get("user_id") or sid_obj.get("union_id") or ""
    msg_id = msg.get("message_id") or ""
    chat_id = msg.get("chat_id") or ""
    if not (sender_id and msg_id and chat_id):
        return None

    mtype = msg.get("message_type") or ""
    raw_content = msg.get("content") or "{}"
    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else dict(raw_content)
    except (json.JSONDecodeError, TypeError):
        content = {}

    text, attachments, kind = "", [], "other"
    if mtype == "text":
        kind = "text"
        text = _MENTION_RE.sub("", str(content.get("text") or ""))
    elif mtype == "image":
        kind = "image"
        if content.get("image_key"):
            attachments.append(
                Attachment(kind="image", ref=f"{msg_id}:image:{content['image_key']}", filename="photo.jpg")
            )
    elif mtype == "file":
        kind = "file"
        if content.get("file_key"):
            attachments.append(
                Attachment(
                    kind="file",
                    ref=f"{msg_id}:file:{content['file_key']}",
                    filename=content.get("file_name") or "file",
                )
            )
    elif mtype == "audio":
        kind = "audio"
        if content.get("file_key"):
            attachments.append(
                Attachment(kind="audio", ref=f"{msg_id}:file:{content['file_key']}", filename="voice.opus")
            )
    elif mtype == "post":
        text, raw_atts, kind = _extract_post(content)
        attachments = [
            Attachment(kind="image", ref=f"{msg_id}:image:{a.ref}", filename=a.filename) for a in raw_atts
        ]

    is_group = str(msg.get("chat_type") or "") == "group"
    mentions = msg.get("mentions") or []
    mentioned = bool(mentions) if is_group else True
    return InboundMessage(
        channel="feishu",
        tenant_id=tenant_id,
        external_msg_id=msg_id,
        conversation_id=chat_id,
        conversation_type="group" if is_group else "direct",
        sender_id=sender_id,
        mentioned=mentioned,
        kind=kind,
        text=text.strip()[:4000],
        attachments=attachments,
        received_at=_ms(msg.get("create_time")),
    )


def _parse_card_v2(tenant_id: str, header: dict[str, Any], event: dict[str, Any]) -> InboundMessage | None:
    act = event.get("action") or {}
    val = act.get("value") or {}
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except json.JSONDecodeError:
            val = {}
    card_id = val.get("card_id")
    action_id = val.get("action")
    op = event.get("operator") or {}
    user = op.get("open_id") or op.get("user_id") or ""
    if not (card_id and action_id and user):
        return None
    ctx = event.get("context") or {}
    chat_id = ctx.get("open_chat_id") or f"card:{card_id}"
    ev_id = header.get("event_id") or f"{card_id}:{action_id}:{user}"
    return InboundMessage(
        channel="feishu",
        tenant_id=tenant_id,
        external_msg_id=f"card:{ev_id}",
        conversation_id=chat_id,
        conversation_type="group" if ctx.get("open_chat_id") else "direct",
        sender_id=user,
        mentioned=True,
        kind="card_action",
        action=CardAction(card_id=str(card_id)[:64], action=str(action_id)[:32]),
    )


def _parse_card_legacy(tenant_id: str, body: dict[str, Any]) -> InboundMessage | None:
    val = (body.get("action") or {}).get("value") or {}
    card_id = val.get("card_id")
    action_id = val.get("action")
    user = body.get("open_id") or ""
    if not (card_id and action_id and user):
        return None
    chat_id = body.get("open_chat_id") or f"card:{card_id}"
    token = body.get("token") or f"{card_id}:{action_id}:{user}"
    return InboundMessage(
        channel="feishu",
        tenant_id=tenant_id,
        external_msg_id=f"card:{token}",
        conversation_id=chat_id,
        conversation_type="group" if body.get("open_chat_id") else "direct",
        sender_id=user,
        mentioned=True,
        kind="card_action",
        action=CardAction(card_id=str(card_id)[:64], action=str(action_id)[:32]),
    )


# ---------------- 发送 ----------------


def build_card_json(card: Card, *, card_id: str, short_code: str) -> dict[str, Any]:
    """将统一 Card DSL 渲染为飞书互动卡片 JSON。"""
    body_md = to_markdown(card.model_copy(update={"buttons": []}))
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": body_md}]
    if card.buttons:
        btn_style = {"primary": "primary", "danger": "danger", "default": "default"}
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": b.label},
                "type": btn_style.get(b.style, "default"),
                "value": {"card_id": card_id, "action": b.id},
            }
            for b in card.buttons
        ]
        elements.append({"tag": "action", "actions": actions})
        if short_code:
            elements.append(
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": f"备用文字指令：确认 {short_code}"}],
                }
            )
    template = "orange" if card.data_mode == "mock" else "blue"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": card.title}, "template": template},
        "elements": elements,
    }


class FeishuAdapter:
    name = "feishu"
    supports_card_callback = True

    def __init__(self, app: FeishuApp, http: httpx.AsyncClient | None = None):
        self.app = app
        self._http = http or httpx.AsyncClient(base_url=API, timeout=15.0)
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and self._token[1] > time.monotonic() + 60:
                return self._token[0]
            r = await self._http.post(
                f"{API}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app.app_id, "app_secret": self.app.app_secret},
            )
            if r.status_code != 200:
                raise ChannelError(f"飞书获取 tenant_access_token 失败：HTTP {r.status_code}")
            body = r.json()
            if body.get("code", 0) != 0 or not body.get("tenant_access_token"):
                raise ChannelError(f"飞书获取 token 失败：{body.get('msg', '未知错误')}")
            self._token = (
                body["tenant_access_token"],
                time.monotonic() + int(body.get("expire", 7200)),
            )
            return self._token[0]

    async def _send_msg(self, to: ConversationRef, msg_type: str, content: dict[str, Any]) -> None:
        token = await self._access_token()
        if to.conversation_type == "group":
            id_type, receive_id = "chat_id", to.conversation_id
        else:
            if not to.user_id:
                raise ChannelError("飞书单聊回复需要对方的 open_id")
            id_type, receive_id = "open_id", to.user_id
        r = await self._http.post(
            f"{API}/open-apis/im/v1/messages",
            params={"receive_id_type": id_type},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code >= 300:
            raise ChannelError(f"飞书发消息失败：HTTP {r.status_code} {r.text[:200]}")
        body = r.json() if r.content else {}
        if body.get("code", 0) != 0:
            raise ChannelError(f"飞书发消息失败：{body.get('msg', '未知错误')}")

    async def send_text(self, to: ConversationRef, text: str) -> None:
        await self._send_msg(to, "text", {"text": text})

    async def send_card(self, to: ConversationRef, card: Card, *, card_id: str, short_code: str) -> None:
        await self._send_msg(to, "interactive", build_card_json(card, card_id=card_id, short_code=short_code))

    async def fetch_attachment(self, att: Attachment, *, max_bytes: int) -> bytes:
        parts = att.ref.split(":", 2)
        if len(parts) != 3:
            raise ChannelError("飞书附件引用格式非法（期望 message_id:type:file_key）")
        msg_id, res_type, file_key = parts
        token = await self._access_token()
        buf = bytearray()
        url = f"{API}/open-apis/im/v1/messages/{msg_id}/resources/{file_key}"
        async with self._http.stream(
            "GET", url, params={"type": res_type}, headers={"Authorization": f"Bearer {token}"}
        ) as r:
            if r.status_code != 200:
                raise ChannelError(f"飞书下载附件失败：HTTP {r.status_code}")
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ChannelError("附件过大")
        return bytes(buf)

    async def aclose(self) -> None:
        await self._http.aclose()
