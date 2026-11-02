"""Trigger 入口 + Fast Path（方案 2.4.2）。

每条 IM 消息进来依次经过：
1. 群聊没 @机器人 → 直接丢弃，不落库（只响应 @，也不留存闲聊内容）；
2. 去重：同一平台消息 id 只处理一次（平台重推、网络重试）；
3. 归入 Thread：同一个会话一个 Thread，卡片点击归入卡片所在的 Thread；
4. 落到人：IM 账号 → 系统用户；没绑定的账号只回一句提示，不做任何事；
5. 分流（零模型成本的确定性规则）：
   - 卡片按钮 / 文字指令「确认 K7Q2M」→ 卡片确认（L2）；
   - 图片、文件 → 直接进文档解析；
   - 其他文字 → 交给 Router（fos.agents.runtime，事务提交后执行）。

IM 消息是不可信输入：这里不把消息内容交给模型，也不据此执行任何写操作；写操作只发生在卡片确认，
并以点击人自己的权限执行。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.cards import Card, CardField
from fos.channels.types import ConversationRef, InboundMessage
from fos.config import Settings
from fos.db.models import ChannelIdentity, Thread, Trigger, User, utcnow
from fos.governance.principal import Principal, principal_for_user
from fos.ledger import Ledger
from fos.perception.blobs import LocalBlobStore
from fos.perception.ocr import OCRProvider
from fos.perception.service import IngestError, ingest_document
from fos.tenancy import TenantRegistry
from fos.triggers.cards import CODE_ALPHABET, CODE_LEN, act_on_card, find_card

log = logging.getLogger(__name__)

# 文字指令：「确认 K7Q2M」「取消 #K7Q2M」，前面可以带 @机器人
COMMAND = re.compile(
    rf"^(?:@\S+\s+)*(\S{{1,10}})\s*#?([{CODE_ALPHABET}{CODE_ALPHABET.lower()}]{{{CODE_LEN}}})$"
)
MAX_ATTACHMENTS = 5
METHOD_TEXT = {
    "text_layer": "文字层",
    "ocr": "OCR",
    "ocr_mock": "OCR（模拟）",
    "docx": "Word",
    "xlsx": "Excel",
    "image": "OCR",
}


@dataclass
class Outcome:
    trigger: Trigger | None  # None：Fast Path 直接丢弃（如群聊未 @）
    duplicate: bool = False
    replies: list[dict[str, Any]] = field(default_factory=list)  # 给调用方看的回复摘要
    outbox: list[tuple[ConversationRef, str | Card]] = field(default_factory=list)


class TriggerIntake:
    def __init__(
        self,
        *,
        registry: TenantRegistry,
        ledger: Ledger,
        ocr: OCRProvider,
        blobs: LocalBlobStore,
        settings: Settings,
    ):
        self.registry = registry
        self.ledger = ledger
        self.ocr = ocr
        self.blobs = blobs
        self.settings = settings

    def channel_enabled(self, tenant_id: str, channel: str) -> bool:
        tenant = self.registry.get(tenant_id)
        if tenant is None:
            return False
        if channel == "mock":
            return self.settings.env != "prod"
        return channel in tenant.im

    async def handle(self, session: AsyncSession, adapter: ChannelAdapter, msg: InboundMessage) -> Outcome:
        if adapter.name != msg.channel:
            raise ValueError("消息来源与适配器不一致")
        if not self.channel_enabled(msg.tenant_id, msg.channel):
            raise ValueError(f"租户 {msg.tenant_id} 没有启用 {msg.channel}")

        command = self._parse_command(msg)
        card = None
        if msg.kind == "card_action" and msg.action:
            card = await find_card(session, msg.tenant_id, card_id=msg.action.card_id)
        elif command:
            card = await find_card(session, msg.tenant_id, code=command[1])
            if card is None or card.channel != msg.channel:
                card, command = None, None  # 不是指令，当普通文字处理

        if msg.conversation_type == "group" and not msg.mentioned and card is None:
            return Outcome(None)

        thread_id = card.thread_id if card else await thread_for(session, msg.tenant_id, msg.reply_to())
        trig = Trigger(
            tenant_id=msg.tenant_id,
            source=msg.channel,
            external_id=msg.external_msg_id,
            thread_id=thread_id,
            sender_ref=msg.sender_id,
            kind=msg.kind,
            route="received",
            status="pending",
            text=msg.text,
            payload={
                "conversation_id": msg.conversation_id,
                "conversation_type": msg.conversation_type,
                "attachments": [a.model_dump() for a in msg.attachments],
                **({"action": msg.action.model_dump()} if msg.action else {}),
            },
            created_at=utcnow(),
        )
        try:
            async with session.begin_nested():
                session.add(trig)
        except IntegrityError:
            prev = (
                await session.scalars(
                    select(Trigger).where(
                        Trigger.tenant_id == msg.tenant_id,
                        Trigger.source == msg.channel,
                        Trigger.external_id == msg.external_msg_id,
                    )
                )
            ).one()
            return Outcome(prev, duplicate=True)

        out = Outcome(trig)
        principal = await self._principal(session, msg)
        if principal is None:
            trig.route, trig.status = "unbound", "rejected"
            await self._reply(
                adapter,
                msg,
                out,
                f"你的{_CHANNEL_TEXT.get(msg.channel, msg.channel)}账号还没有绑定系统用户，"
                f"请把这条消息转给管理员绑定（账号：{msg.sender_id}）。",
            )
        else:
            trig.user_id = principal.subject_id
            if card is not None:
                action = msg.action.action if msg.action else (command[0] if command else "")
                await self._card_action(session, adapter, msg, out, principal, card, action)
            elif msg.attachments and msg.kind in ("image", "file", "rich"):
                await self._documents(session, adapter, msg, out, principal)
            elif msg.kind in ("text", "rich") and msg.text:
                trig.route, trig.status = "router", "pending"  # 提交后交给 AgentRuntime
            else:
                trig.route, trig.status = "unsupported", "rejected"
                await self._reply(adapter, msg, out, "暂时不支持这类消息，可以发文字、照片或文件。")

        th = await session.get(Thread, thread_id)
        if th is not None:
            th.last_trigger_at = utcnow()
        await session.flush()
        return out

    # ---------------- 分流 ----------------

    async def _card_action(
        self, session, adapter, msg, out: Outcome, principal: Principal, card, action: str
    ):
        trig = out.trigger
        assert trig is not None
        trig.route = "card_action"
        res = await act_on_card(session, self.ledger, principal, card, action, channel=msg.channel)
        trig.status = "done" if res.status in ("done", "cancelled") else "rejected"
        trig.result = {"card_id": card.id, "outcome": res.status, **res.result}
        await self._reply(adapter, msg, out, res.message)

    async def _documents(self, session, adapter, msg, out: Outcome, principal: Principal):
        trig = out.trigger
        assert trig is not None
        trig.route = "doc_intake"
        if not principal.can("doc.document:write"):
            trig.status = "rejected"
            await self._reply(adapter, msg, out, "你没有上传文件的权限，请联系管理员。")
            return
        docs: list[dict[str, Any]] = []
        limit = self.settings.max_upload_mb * 1024 * 1024
        for att in msg.attachments[:MAX_ATTACHMENTS]:
            try:
                data = await adapter.fetch_attachment(att, max_bytes=limit)
                r = await ingest_document(
                    session,
                    tenant_id=msg.tenant_id,
                    uploaded_by=principal.subject_id,
                    data=data,
                    filename=att.filename or ("photo.jpg" if att.kind == "image" else "file"),
                    kind="other",
                    ocr=self.ocr,
                    blobs=self.blobs,
                    settings=self.settings,
                )
            except (ChannelError, IngestError) as e:
                docs.append({"error": str(e)[:200], "filename": att.filename})
                await self._reply(adapter, msg, out, f"文件没有处理成功：{e}")
                continue
            d = r.document
            methods = sorted({METHOD_TEXT.get(p.method, p.method) for p in r.pages})
            docs.append({"document_id": d.id, "status": d.status, "duplicate": r.duplicate})
            fields = [
                CardField(label="文件", value=d.filename),
                CardField(label="页数", value=str(d.page_count)),
                CardField(label="识别方式", value="、".join(methods) or "—"),
            ]
            if d.status == "failed":
                title, text = "识别失败", f"原件已保存，识别没有成功：{(d.error or '')[:100]}"
            elif r.duplicate:
                title, text = "这份文件之前已收到", "不重复识别，沿用上次的结果。"
            else:
                title, text = "已收到并识别", "原件和识别结果已保存，可以在控制台查看每一页。"
            card = Card(
                title=title, text=text, fields=fields, data_mode=d.data_mode, footer=f"文档 {d.id[:8]}"
            )
            await self._reply(adapter, msg, out, card)
        trig.result = {"documents": docs}
        trig.status = "done" if docs and all("document_id" in x for x in docs) else "failed"

    # ---------------- 工具 ----------------

    def _parse_command(self, msg: InboundMessage) -> tuple[str, str] | None:
        if msg.kind != "text":
            return None
        m = COMMAND.match(msg.text.strip())
        return (m.group(1), m.group(2).upper()) if m else None

    async def _principal(self, session: AsyncSession, msg: InboundMessage) -> Principal | None:
        ident = await session.get(ChannelIdentity, (msg.tenant_id, msg.channel, msg.sender_id))
        if ident is None:
            return None
        user = await session.get(User, ident.user_id)
        tenant = self.registry.get(msg.tenant_id)
        if user is None or not user.is_active or user.tenant_id != msg.tenant_id or tenant is None:
            return None
        return principal_for_user(user, tenant)

    async def _reply(self, adapter: ChannelAdapter, msg: InboundMessage, out: Outcome, body: str | Card):
        """回复先排队，等事务提交后再由 deliver() 发出，避免“回复了已记账但其实回滚了”。"""
        out.outbox.append((msg.reply_to(), body))
        if isinstance(body, Card):
            out.replies.append({"type": "card", "card": body.model_dump()})
        else:
            out.replies.append({"type": "text", "text": body})


async def deliver(adapter: ChannelAdapter, out: Outcome) -> list[str]:
    """事务提交后调用：发出排队的回复。返回发送失败的原因（不影响已落库的结果）。"""
    errors: list[str] = []
    for to, body in out.outbox:
        try:
            if isinstance(body, Card):
                await adapter.send_card(to, body, card_id="", short_code="")
            else:
                await adapter.send_text(to, body)
        except ChannelError as e:
            log.warning("回复失败 trigger=%s：%s", out.trigger.id if out.trigger else "-", e)
            errors.append(str(e)[:200])
    out.outbox.clear()
    return errors


def thread_key(to: ConversationRef) -> str:
    if to.conversation_type == "group":
        return f"{to.channel}:group:{to.conversation_id}"
    return f"{to.channel}:dm:{to.user_id}"


async def thread_for(session: AsyncSession, tenant_id: str, to: ConversationRef) -> str:
    """一个 IM 会话（群或单聊）一个 Thread。"""
    key = thread_key(to)
    q = select(Thread.id).where(Thread.tenant_id == tenant_id, Thread.key == key)
    tid = (await session.scalars(q)).one_or_none()
    if tid:
        return tid
    th = Thread(tenant_id=tenant_id, key=key, kind="im")
    try:
        async with session.begin_nested():
            session.add(th)
        return th.id
    except IntegrityError:  # 并发下另一条消息刚建好
        return (await session.scalars(q)).one()


_CHANNEL_TEXT = {"dingtalk": "钉钉", "feishu": "飞书", "wecom": "企业微信", "mock": "模拟信道"}
