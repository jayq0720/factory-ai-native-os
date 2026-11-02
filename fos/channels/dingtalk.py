"""钉钉适配器：企业内部应用机器人，Stream 模式（长连接，不需要公网回调地址）。

- 收：`DingTalkStream` 建立长连接，订阅机器人消息和互动卡片回调，解析成 InboundMessage。
- 发：钉钉开放平台 OpenAPI（群聊 groupMessages/send，单聊 oToMessages/batchSend）。
- 卡片：配置了互动卡片模板（FOS_DINGTALK_<租户>_CARD_TEMPLATE_ID）时发互动卡片，按钮点击走
  Stream 回调；没配置时退回 Markdown，用文字指令确认（群里需要 @机器人）。

凭据只从环境变量读取，不进代码、不进租户配置、不进模型上下文：
  FOS_DINGTALK_<TENANT>_CLIENT_ID / _CLIENT_SECRET / _ROBOT_CODE（默认等于 CLIENT_ID）/ _CARD_TEMPLATE_ID

接口字段依据钉钉开放平台文档整理，首次接入真实应用时需要逐项核对（docs/channels.md）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from fos import __version__
from fos.channels.base import ChannelError
from fos.channels.cards import Card, to_markdown
from fos.channels.types import Attachment, CardAction, ConversationRef, InboundMessage

log = logging.getLogger(__name__)

API = "https://api.dingtalk.com"
TOPIC_BOT_MESSAGE = "/v1.0/im/bot/messages/get"
TOPIC_CARD_CALLBACK = "/v1.0/card/instances/callback"


@dataclass(frozen=True)
class DingTalkApp:
    tenant_id: str
    client_id: str
    client_secret: str
    robot_code: str
    card_template_id: str | None = None

    def __repr__(self) -> str:  # 避免密钥出现在日志里
        return f"DingTalkApp(tenant_id={self.tenant_id!r}, client_id={self.client_id!r})"

    @classmethod
    def from_env(cls, tenant_id: str, env: dict[str, str] | None = None) -> DingTalkApp | None:
        env = dict(os.environ) if env is None else env
        p = f"FOS_DINGTALK_{tenant_id.upper()}_"
        cid, secret = env.get(p + "CLIENT_ID", ""), env.get(p + "CLIENT_SECRET", "")
        if not cid or not secret:
            return None
        return cls(
            tenant_id=tenant_id,
            client_id=cid,
            client_secret=secret,
            robot_code=env.get(p + "ROBOT_CODE") or cid,
            card_template_id=env.get(p + "CARD_TEMPLATE_ID") or None,
        )


# ---------------- 解析：钉钉回调 → InboundMessage ----------------


def _ms(v: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def parse_bot_message(tenant_id: str, data: dict[str, Any]) -> InboundMessage:
    """机器人收到的消息（单聊或群里 @机器人）。"""
    msgtype = data.get("msgtype", "")
    content = data.get("content") or {}
    text, attachments, kind = "", [], "other"
    if msgtype == "text":
        kind, text = "text", (data.get("text") or {}).get("content", "")
    elif msgtype == "picture":
        code = content.get("downloadCode") or content.get("pictureDownloadCode")
        kind = "image"
        if code:
            attachments.append(Attachment(kind="image", ref=code, filename="photo.jpg"))
    elif msgtype == "file":
        kind = "file"
        if content.get("downloadCode"):
            attachments.append(
                Attachment(kind="file", ref=content["downloadCode"], filename=content.get("fileName"))
            )
    elif msgtype == "audio":
        kind, text = "audio", content.get("recognition", "")  # 钉钉自带的语音转文字
    elif msgtype == "richText":
        kind = "rich"
        for part in content.get("richText") or []:
            if "text" in part:
                text += part["text"]
            elif part.get("downloadCode"):
                attachments.append(Attachment(kind="image", ref=part["downloadCode"], filename="photo.jpg"))
    is_group = str(data.get("conversationType")) == "2"
    return InboundMessage(
        channel="dingtalk",
        tenant_id=tenant_id,
        external_msg_id=data["msgId"],
        conversation_id=data["conversationId"],
        conversation_type="group" if is_group else "direct",
        sender_id=data.get("senderStaffId") or data["senderId"],
        sender_name=(data.get("senderNick") or "")[:64],
        # 钉钉群里只有 @机器人 的消息才会推送过来；isInAtList 缺省视为已 @
        mentioned=bool(data.get("isInAtList", True)) if is_group else True,
        kind=kind,
        text=text.strip()[:4000],
        attachments=attachments,
        received_at=_ms(data.get("createAt")),
    )


def parse_card_callback(tenant_id: str, data: dict[str, Any], message_id: str) -> InboundMessage | None:
    """互动卡片按钮回调。按钮的回调参数里约定带 action（见 docs/channels.md 模板约定）。"""
    content = data.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    private = (content or {}).get("cardPrivateData") or {}
    params = private.get("params") or {}
    action = params.get("action") or next(iter(private.get("actionIds") or []), None)
    card_id = data.get("outTrackId")
    user = data.get("userId")
    if not (action and card_id and user):
        return None
    space = str(data.get("spaceId") or "")
    is_group = str(data.get("spaceType", "")).upper() == "IM_GROUP"
    return InboundMessage(
        channel="dingtalk",
        tenant_id=tenant_id,
        external_msg_id=f"card:{message_id}",
        conversation_id=space or f"card:{card_id}",
        conversation_type="group" if is_group else "direct",
        sender_id=user,
        mentioned=True,
        kind="card_action",
        action=CardAction(card_id=card_id, action=str(action)[:32]),
    )


# ---------------- 发送 ----------------


class DingTalkAdapter:
    name = "dingtalk"

    def __init__(self, app: DingTalkApp, http: httpx.AsyncClient | None = None):
        self.app = app
        self._http = http or httpx.AsyncClient(base_url=API, timeout=15.0)
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    @property
    def supports_card_callback(self) -> bool:
        return self.app.card_template_id is not None

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and self._token[1] > time.monotonic() + 60:
                return self._token[0]
            r = await self._http.post(
                f"{API}/v1.0/oauth2/accessToken",
                json={"appKey": self.app.client_id, "appSecret": self.app.client_secret},
            )
            if r.status_code != 200:
                raise ChannelError(f"钉钉获取 accessToken 失败：HTTP {r.status_code}")
            body = r.json()
            self._token = (body["accessToken"], time.monotonic() + int(body.get("expireIn", 7200)))
            return self._token[0]

    async def _call(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        r = await self._http.request(
            method, f"{API}{path}", json=body, headers={"x-acs-dingtalk-access-token": token}
        )
        if r.status_code >= 300:
            raise ChannelError(f"钉钉接口 {path} 失败：HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    async def _send(self, to: ConversationRef, msg_key: str, param: dict[str, Any]) -> None:
        body: dict[str, Any] = {
            "robotCode": self.app.robot_code,
            "msgKey": msg_key,
            "msgParam": json.dumps(param, ensure_ascii=False),
        }
        if to.conversation_type == "group":
            await self._call(
                "POST", "/v1.0/robot/groupMessages/send", {**body, "openConversationId": to.conversation_id}
            )
        else:
            if not to.user_id:
                raise ChannelError("单聊回复需要对方的 userid")
            await self._call("POST", "/v1.0/robot/oToMessages/batchSend", {**body, "userIds": [to.user_id]})

    async def send_text(self, to: ConversationRef, text: str) -> None:
        await self._send(to, "sampleText", {"content": text})

    async def send_card(self, to: ConversationRef, card: Card, *, card_id: str, short_code: str) -> None:
        if not (card.buttons and self.app.card_template_id):
            md = to_markdown(card, short_code=short_code, mention_hint=to.conversation_type == "group")
            await self._send(to, "sampleMarkdown", {"title": card.title, "text": md})
            return
        # 互动卡片：模板变量约定见 docs/channels.md
        params = {
            "title": card.title,
            "content": to_markdown(card.model_copy(update={"buttons": []})),
            "buttons": json.dumps([b.model_dump() for b in card.buttons], ensure_ascii=False),
            "status": "open",
        }
        if to.conversation_type == "group":
            space = {
                "openSpaceId": f"dtv1.card//IM_GROUP.{to.conversation_id}",
                "imGroupOpenSpaceModel": {"supportForward": False},
                "imGroupOpenDeliverModel": {"robotCode": self.app.robot_code},
            }
        else:
            space = {
                "openSpaceId": f"dtv1.card//IM_ROBOT.{to.user_id}",
                "imRobotOpenSpaceModel": {"supportForward": False},
                "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT", "robotCode": self.app.robot_code},
            }
        await self._call(
            "POST",
            "/v1.0/card/instances/createAndDeliver",
            {
                "cardTemplateId": self.app.card_template_id,
                "outTrackId": card_id,
                "callbackType": "STREAM",
                "cardData": {"cardParamMap": params},
                **space,
            },
        )

    async def fetch_attachment(self, att: Attachment, *, max_bytes: int) -> bytes:
        body = await self._call(
            "POST",
            "/v1.0/robot/messageFiles/download",
            {"downloadCode": att.ref, "robotCode": self.app.robot_code},
        )
        url = body.get("downloadUrl")
        if not url:
            raise ChannelError("钉钉没有返回下载地址")
        buf = bytearray()
        async with self._http.stream("GET", url) as r:
            if r.status_code != 200:
                raise ChannelError(f"下载附件失败：HTTP {r.status_code}")
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ChannelError("附件过大")
        return bytes(buf)

    async def aclose(self) -> None:
        await self._http.aclose()


# ---------------- Stream 长连接 ----------------

Handler = Callable[[InboundMessage], Awaitable[None]]


def ack(message_id: str, response: Any = None) -> str:
    return json.dumps(
        {
            "code": 200,
            "headers": {"contentType": "application/json", "messageId": message_id},
            "message": "OK",
            "data": json.dumps({"response": response}),
        }
    )


def decode_frame(tenant_id: str, frame: dict[str, Any]) -> tuple[str | None, InboundMessage | None]:
    """处理一帧 Stream 消息。返回（要回给钉钉的 ack 文本，解析出的消息）。"""
    headers = frame.get("headers") or {}
    mid = headers.get("messageId", "")
    topic = headers.get("topic", "")
    ftype = frame.get("type")
    if ftype == "SYSTEM":
        if topic == "ping":
            return json.dumps(
                {
                    "code": 200,
                    "headers": headers,
                    "message": "OK",
                    "data": frame.get("data", ""),
                }
            ), None
        return None, None  # disconnect 等由调用方处理
    if ftype not in ("CALLBACK", "EVENT"):
        return None, None
    try:
        data = json.loads(frame.get("data") or "{}")
    except json.JSONDecodeError:
        return ack(mid), None
    msg: InboundMessage | None = None
    try:
        if topic == TOPIC_BOT_MESSAGE:
            msg = parse_bot_message(tenant_id, data)
        elif topic == TOPIC_CARD_CALLBACK:
            msg = parse_card_callback(tenant_id, data, mid)
    except (KeyError, ValueError) as e:
        log.warning("钉钉消息解析失败 topic=%s: %s", topic, e)
    return ack(mid), msg


class DingTalkStream:
    """长连接客户端：断线自动重连（指数退避，最长 60 秒）。"""

    def __init__(self, app: DingTalkApp, handler: Handler, http: httpx.AsyncClient | None = None):
        self.app = app
        self.handler = handler
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._tasks: set[asyncio.Task] = set()

    async def _open(self) -> str:
        r = await self._http.post(
            f"{API}/v1.0/gateway/connections/open",
            json={
                "clientId": self.app.client_id,
                "clientSecret": self.app.client_secret,
                "subscriptions": [
                    {"type": "CALLBACK", "topic": TOPIC_BOT_MESSAGE},
                    {"type": "CALLBACK", "topic": TOPIC_CARD_CALLBACK},
                ],
                "ua": f"fos/{__version__}",
            },
        )
        if r.status_code != 200:
            raise ChannelError(f"钉钉 Stream 建连失败：HTTP {r.status_code}")
        body = r.json()
        return f"{body['endpoint']}?ticket={body['ticket']}"

    async def run_forever(self) -> None:
        import websockets

        delay = 1.0
        while True:
            try:
                url = await self._open()
                async with websockets.connect(url, ping_interval=30, max_size=4 * 1024 * 1024) as ws:
                    log.info("钉钉 Stream 已连接 tenant=%s", self.app.tenant_id)
                    delay = 1.0
                    async for raw in ws:
                        frame = json.loads(raw)
                        headers = frame.get("headers") or {}
                        if frame.get("type") == "SYSTEM" and headers.get("topic") == "disconnect":
                            break
                        reply, msg = decode_frame(self.app.tenant_id, frame)
                        if reply:  # 先 ack，避免钉钉超时重推
                            await ws.send(reply)
                        if msg is not None:
                            t = asyncio.create_task(self._dispatch(msg))
                            self._tasks.add(t)
                            t.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 网络抖动、钉钉侧断开
                log.warning("钉钉 Stream 断开 tenant=%s：%s，%.0f 秒后重连", self.app.tenant_id, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _dispatch(self, msg: InboundMessage) -> None:
        try:
            await self.handler(msg)
        except Exception:
            log.exception("处理钉钉消息失败 msg=%s", msg.external_msg_id)
