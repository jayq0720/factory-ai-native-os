"""企业微信适配器：自建应用，回调 URL 模式 + WXBizMsgCrypt（方案第 6 章）。

- 收：HTTPS 回调（GET 校验 URL、POST 收加密 XML 消息与模板卡片按钮事件），内建 SHA-1 签名校验与
  AES-256-CBC 加解密（基于 cryptography，不引入第三方 SDK）。
- 发：企业微信服务端 API（单聊 /cgi-bin/message/send，群聊 /cgi-bin/appchat/send）。
- 卡片：单聊带按钮时发送 button_interaction 模板卡片（支持按钮回调）；群聊或无按钮时发 Markdown，
  附备用文字指令。

凭据只从环境变量读取，不进代码、不进租户配置、不进模型上下文：
  FOS_WECOM_<TENANT>_CORP_ID / _AGENT_ID / _SECRET / _TOKEN（回调校验）/ _ENCODING_AES_KEY（回调解密）
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from fos.channels.base import ChannelError
from fos.channels.cards import Card, to_markdown
from fos.channels.types import Attachment, CardAction, ConversationRef, InboundMessage

API = "https://qyapi.weixin.qq.com"


@dataclass(frozen=True)
class WeComApp:
    tenant_id: str
    corp_id: str
    agent_id: int
    secret: str
    token: str | None = None
    encoding_aes_key: str | None = None

    def __repr__(self) -> str:
        return f"WeComApp(tenant_id={self.tenant_id!r}, corp_id={self.corp_id!r}, agent_id={self.agent_id!r})"

    @classmethod
    def from_env(cls, tenant_id: str, env: dict[str, str] | None = None) -> WeComApp | None:
        env = dict(os.environ) if env is None else env
        p = f"FOS_WECOM_{tenant_id.upper()}_"
        corp_id = env.get(p + "CORP_ID", "")
        agent_raw = env.get(p + "AGENT_ID", "")
        secret = env.get(p + "SECRET", "")
        if not (corp_id and agent_raw and secret):
            return None
        try:
            agent_id = int(agent_raw)
        except ValueError:
            return None
        return cls(
            tenant_id=tenant_id,
            corp_id=corp_id,
            agent_id=agent_id,
            secret=secret,
            token=env.get(p + "TOKEN") or None,
            encoding_aes_key=env.get(p + "ENCODING_AES_KEY") or None,
        )


# ---------------- WXBizMsgCrypt（SHA-1 签名 + AES-256-CBC） ----------------


def _aes_key(encoding_aes_key: str) -> bytes:
    try:
        key = base64.b64decode(encoding_aes_key + "=", validate=True)
    except (binascii.Error, ValueError) as e:
        raise ChannelError("企业微信 EncodingAESKey 不是合法 Base64") from e
    if len(key) != 32:
        raise ChannelError("企业微信 EncodingAESKey 解码后必须为 32 字节")
    return key


def verify_signature(token: str, timestamp: str, nonce: str, encrypt_str: str, msg_signature: str) -> bool:
    """校验企业微信回调签名（SHA-1，恒定时间比较）。"""
    if not (token and timestamp and nonce and encrypt_str and msg_signature):
        return False
    raw = "".join(sorted([token, timestamp, nonce, encrypt_str])).encode("utf-8")
    return hmac.compare_digest(hashlib.sha1(raw).hexdigest(), msg_signature)


def encrypt_message(
    encoding_aes_key: str, corp_id: str, plaintext: str, *, rand16: bytes | None = None
) -> str:
    """按企业微信 WXBizMsgCrypt 规范加密消息（32 字节 PKCS7 填充 + AES-256-CBC）。"""
    key = _aes_key(encoding_aes_key)
    msg_bytes = plaintext.encode("utf-8")
    prefix = rand16 if rand16 is not None else os.urandom(16)
    body = prefix + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    pad_len = 32 - (len(body) % 32)
    padded = body + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode("ascii")


def decrypt_message(encoding_aes_key: str, corp_id: str, encrypt_b64: str) -> str:
    """按企业微信 WXBizMsgCrypt 规范解密消息并校验 receive_id == corp_id。"""
    key = _aes_key(encoding_aes_key)
    try:
        ct = base64.b64decode(encrypt_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ChannelError("企业微信加密消息不是合法 Base64") from e
    if len(ct) < 32 or len(ct) % 16 != 0:
        raise ChannelError("企业微信加密消息长度非法")
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = dec.update(ct) + dec.finalize()
    pad = padded[-1]
    if pad < 1 or pad > 32 or padded[-pad:] != bytes([pad]) * pad:
        raise ChannelError("企业微信解密填充非法")
    raw = padded[:-pad]
    if len(raw) < 20:
        raise ChannelError("企业微信解密内容过短")
    msg_len = struct.unpack("!I", raw[16:20])[0]
    if 20 + msg_len > len(raw):
        raise ChannelError("企业微信解密消息长度越界")
    msg_bytes = raw[20 : 20 + msg_len]
    recv_id = raw[20 + msg_len :].decode("utf-8", errors="replace")
    if not hmac.compare_digest(recv_id, corp_id):
        raise ChannelError("企业微信 CorpID 校验失败")
    return msg_bytes.decode("utf-8")


# ---------------- XML 安全解析 → InboundMessage ----------------


def _parse_xml_safe(xml_text: str) -> ET.Element:
    """拒绝 DTD / ENTITY 声明，防止 XXE。"""
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ChannelError("拒绝包含 DTD/ENTITY 的 XML 负载")
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ChannelError("企业微信 XML 解析失败") from e


def extract_encrypt_xml(xml_text: str) -> str:
    root = _parse_xml_safe(xml_text)
    enc = root.findtext("Encrypt") or ""
    if not enc:
        raise ChannelError("企业微信回调 XML 缺少 Encrypt 字段")
    return enc


def _sec(v: str | None) -> datetime:
    try:
        return datetime.fromtimestamp(int(v or 0), tz=UTC)
    except ValueError:
        return datetime.now(UTC)


def parse_xml_message(tenant_id: str, xml_text: str) -> InboundMessage | None:
    """解析解密后的企业微信回调 XML（消息或模板卡片事件）。"""
    root = _parse_xml_safe(xml_text)
    msg_type = (root.findtext("MsgType") or "").strip().lower()
    sender = (root.findtext("FromUserName") or "").strip()
    chat_id = (root.findtext("ChatId") or "").strip()
    is_group = bool(chat_id)
    conv_id = chat_id if is_group else sender
    if not (sender and conv_id):
        return None
    received_at = _sec(root.findtext("CreateTime"))

    if msg_type == "event":
        ev = (root.findtext("Event") or "").strip().lower()
        if ev != "template_card_event":
            return None
        task_id = (root.findtext("TaskId") or "").strip()
        event_key = (root.findtext("EventKey") or "").strip()
        if not (task_id and event_key):
            return None
        msg_id = f"card:{task_id}:{event_key}:{root.findtext('CreateTime') or ''}"
        return InboundMessage(
            channel="wecom",
            tenant_id=tenant_id,
            external_msg_id=msg_id[:128],
            conversation_id=conv_id,
            conversation_type="group" if is_group else "direct",
            sender_id=sender,
            mentioned=True,
            kind="card_action",
            action=CardAction(card_id=task_id[:64], action=event_key[:32]),
            received_at=received_at,
        )

    msg_id = (root.findtext("MsgId") or "").strip()
    if not msg_id:
        return None
    text, attachments, kind = "", [], "other"
    if msg_type == "text":
        kind = "text"
        text = root.findtext("Content") or ""
    elif msg_type == "image":
        kind = "image"
        media_id = (root.findtext("MediaId") or "").strip()
        if media_id:
            attachments.append(Attachment(kind="image", ref=media_id, filename="photo.jpg"))
    elif msg_type == "file":
        kind = "file"
        media_id = (root.findtext("MediaId") or "").strip()
        if media_id:
            attachments.append(
                Attachment(kind="file", ref=media_id, filename=root.findtext("FileName") or "file")
            )
    elif msg_type == "voice":
        kind = "audio"
        media_id = (root.findtext("MediaId") or "").strip()
        if media_id:
            attachments.append(Attachment(kind="audio", ref=media_id, filename="voice.amr"))

    return InboundMessage(
        channel="wecom",
        tenant_id=tenant_id,
        external_msg_id=msg_id,
        conversation_id=conv_id,
        conversation_type="group" if is_group else "direct",
        sender_id=sender,
        mentioned=True,
        kind=kind,
        text=text.strip()[:4000],
        attachments=attachments,
        received_at=received_at,
    )


# ---------------- 发送 ----------------


def build_template_card(card: Card, *, card_id: str, short_code: str) -> dict[str, Any]:
    """将统一 Card DSL 渲染为企业微信 button_interaction 模板卡片。"""
    style_map = {"primary": 1, "default": 2, "danger": 3}
    desc = card.text[:200]
    if card.data_mode == "mock":
        desc = f"【模拟数据】{desc}"[:200]
    h_list = [{"keyname": f.label[:5], "value": f.value[:30]} for f in card.fields[:6]]
    buttons = [
        {"text": b.label[:10], "style": style_map.get(b.style, 2), "key": b.id[:32]}
        for b in card.buttons[:6]
    ]
    sub_text = card.footer or (f"备用文字指令：确认 {short_code}" if short_code else "")
    out: dict[str, Any] = {
        "card_type": "button_interaction",
        "main_title": {"title": card.title[:26], "desc": desc},
        "task_id": card_id,
        "button_list": buttons,
    }
    if h_list:
        out["horizontal_content_list"] = h_list
    if sub_text:
        out["sub_title_text"] = sub_text[:112]
    return out


class WeComAdapter:
    name = "wecom"
    supports_card_callback = True

    def __init__(self, app: WeComApp, http: httpx.AsyncClient | None = None):
        self.app = app
        self._http = http or httpx.AsyncClient(base_url=API, timeout=15.0)
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and self._token[1] > time.monotonic() + 60:
                return self._token[0]
            r = await self._http.get(
                f"{API}/cgi-bin/gettoken",
                params={"corpid": self.app.corp_id, "corpsecret": self.app.secret},
            )
            if r.status_code != 200:
                raise ChannelError(f"企业微信获取 access_token 失败：HTTP {r.status_code}")
            body = r.json()
            if body.get("errcode", 0) != 0 or not body.get("access_token"):
                raise ChannelError(f"企业微信获取 token 失败：{body.get('errmsg', '未知错误')}")
            self._token = (
                body["access_token"],
                time.monotonic() + int(body.get("expires_in", 7200)),
            )
            return self._token[0]

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        r = await self._http.post(f"{API}{path}", params={"access_token": token}, json=body)
        if r.status_code >= 300:
            raise ChannelError(f"企业微信接口 {path} 失败：HTTP {r.status_code} {r.text[:200]}")
        data = r.json() if r.content else {}
        if data.get("errcode", 0) != 0:
            raise ChannelError(f"企业微信接口 {path} 失败：{data.get('errmsg', '未知错误')}")
        return data

    async def send_text(self, to: ConversationRef, text: str) -> None:
        if to.conversation_type == "group":
            await self._post(
                "/cgi-bin/appchat/send",
                {"chatid": to.conversation_id, "msgtype": "text", "text": {"content": text}},
            )
            return
        if not to.user_id:
            raise ChannelError("企业微信单聊回复需要对方的 userid")
        await self._post(
            "/cgi-bin/message/send",
            {
                "touser": to.user_id,
                "agentid": self.app.agent_id,
                "msgtype": "text",
                "text": {"content": text},
            },
        )

    async def send_card(self, to: ConversationRef, card: Card, *, card_id: str, short_code: str) -> None:
        # 企业微信单聊带按钮时走 template_card（原生回调）；群聊或无按钮退回 Markdown
        if to.conversation_type == "direct" and card.buttons:
            if not to.user_id:
                raise ChannelError("企业微信单聊卡片需要对方的 userid")
            await self._post(
                "/cgi-bin/message/send",
                {
                    "touser": to.user_id,
                    "agentid": self.app.agent_id,
                    "msgtype": "template_card",
                    "template_card": build_template_card(card, card_id=card_id, short_code=short_code),
                },
            )
            return
        md = to_markdown(card, short_code=short_code, mention_hint=False)
        if to.conversation_type == "group":
            await self._post(
                "/cgi-bin/appchat/send",
                {"chatid": to.conversation_id, "msgtype": "markdown", "markdown": {"content": md}},
            )
        else:
            if not to.user_id:
                raise ChannelError("企业微信单聊回复需要对方的 userid")
            await self._post(
                "/cgi-bin/message/send",
                {
                    "touser": to.user_id,
                    "agentid": self.app.agent_id,
                    "msgtype": "markdown",
                    "markdown": {"content": md},
                },
            )

    async def fetch_attachment(self, att: Attachment, *, max_bytes: int) -> bytes:
        token = await self._access_token()
        buf = bytearray()
        async with self._http.stream(
            "GET",
            f"{API}/cgi-bin/media/get",
            params={"access_token": token, "media_id": att.ref},
        ) as r:
            if r.status_code != 200:
                raise ChannelError(f"企业微信下载附件失败：HTTP {r.status_code}")
            ctype = (r.headers.get("content-type") or "").lower()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ChannelError("附件过大")
            if "application/json" in ctype:
                raise ChannelError(f"企业微信下载附件失败：{buf.decode('utf-8', errors='replace')[:200]}")
        return bytes(buf)

    async def aclose(self) -> None:
        await self._http.aclose()
