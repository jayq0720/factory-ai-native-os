"""卡片确认（L2 单步获准，方案 2.7）。

Agent 或业务代码提出一张卡片，附上“确认后要执行的动作”（目前只有：向事件账本追加一个事件）。
动作以**点击人自己的身份和权限**执行，Agent 本身不获得写权限；一张卡片只生效一次。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.cards import Card
from fos.channels.types import ConversationRef
from fos.db.models import CardRecord, utcnow
from fos.governance.principal import Principal
from fos.ledger import Ledger
from fos.ledger.service import AppendRequest, Evidence, Forbidden, LedgerError

log = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混的 I L O 0 1
CODE_LEN = 5
DEFAULT_TTL = timedelta(hours=72)


class LedgerAppendAction(BaseModel):
    kind: Literal["ledger.append"] = "ledger.append"
    request: AppendRequest


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite 读回来不带时区


def _code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


async def propose_card(
    session: AsyncSession,
    *,
    tenant_id: str,
    to: ConversationRef,
    thread_id: str,
    card: Card,
    created_by: str,
    action: LedgerAppendAction | None = None,
    required_permission: str | None = None,
    allowed_user_id: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> CardRecord:
    """记录一张带按钮的卡片。提交事务后用 send_card_record() 发出。"""
    if not card.buttons:
        raise ValueError("需要确认的卡片至少要有一个按钮")
    if action is not None and card.button("confirm") is None:
        raise ValueError("带动作的卡片必须有 id 为 confirm 的按钮")
    if action is not None and action.request.data_mode != card.data_mode:
        raise ValueError("卡片的 data_mode 必须与要写入的事件一致")
    rec: CardRecord | None = None
    for _ in range(5):
        rec = CardRecord(
            tenant_id=tenant_id,
            short_code=_code(),
            thread_id=thread_id,
            channel=to.channel,
            conversation=to.model_dump(),
            card=card.model_dump(),
            action=action.model_dump(mode="json") if action else None,
            required_permission=required_permission,
            allowed_user_id=allowed_user_id,
            created_by=created_by,
            expires_at=utcnow() + ttl,
        )
        try:
            async with session.begin_nested():
                session.add(rec)
            break
        except IntegrityError:
            rec = None  # 短码撞了，换一个
    if rec is None:
        raise RuntimeError("无法分配卡片短码")
    await session.flush()
    return rec


async def send_card_record(adapter: ChannelAdapter, rec: CardRecord) -> str | None:
    """卡片记录提交之后再发（否则按钮回调可能先于记录到达）。返回失败原因。"""
    try:
        await adapter.send_card(
            ConversationRef.model_validate(rec.conversation),
            Card.model_validate(rec.card),
            card_id=rec.id,
            short_code=rec.short_code,
        )
    except ChannelError as e:
        log.warning("卡片发送失败 card=%s：%s", rec.id, e)
        return str(e)[:300]
    return None


async def find_card(
    session: AsyncSession, tenant_id: str, *, card_id: str | None = None, code: str | None = None
):
    q = select(CardRecord).where(CardRecord.tenant_id == tenant_id)
    q = (
        q.where(CardRecord.id == card_id)
        if card_id
        else q.where(CardRecord.short_code == (code or "").upper())
    )
    return (await session.scalars(q)).one_or_none()


@dataclass
class ActOutcome:
    status: str  # done / cancelled / expired / failed / rejected / already
    message: str  # 回给点击人的话
    result: dict[str, Any] = field(default_factory=dict)


async def act_on_card(
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    rec: CardRecord,
    action_id: str,
    *,
    channel: str,
) -> ActOutcome:
    card = Card.model_validate(rec.card)
    button = card.button(action_id)
    if button is None:
        return ActOutcome("rejected", f"卡片上没有「{action_id}」这个选项")
    if rec.status != "open":
        return ActOutcome("already", f"这张卡片已经处理过了（{_STATUS_TEXT.get(rec.status, rec.status)}）")
    if _aware(rec.expires_at) < utcnow():
        rec.status = "expired"
        await session.flush()
        return ActOutcome("expired", "这张卡片已过期，请重新发起")
    if rec.allowed_user_id and rec.allowed_user_id != principal.subject_id:
        return ActOutcome("rejected", "这张卡片需要指定的人确认")
    if rec.required_permission and not principal.can(rec.required_permission):
        return ActOutcome("rejected", "你没有确认这张卡片的权限")

    # 抢占：只有一个点击能把 open 改成 acting（PostgreSQL 下并发点击会在这里串行）
    claimed = await session.execute(
        update(CardRecord)
        .where(CardRecord.id == rec.id, CardRecord.status == "open")
        .values(status="acting")
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.refresh(rec)
        return ActOutcome("already", "这张卡片已经处理过了")
    await session.refresh(rec)

    outcome = await _execute(session, ledger, principal, rec, button.id, channel)
    if outcome.status == "open":  # 点击人权限不够：卡片保持可用，留给有权限的人
        rec.status = "open"
    else:
        rec.status = outcome.status
        rec.result = {**outcome.result, "action": button.id}
        rec.acted_by = principal.subject_id
        rec.acted_at = utcnow()
    await session.flush()
    if outcome.status == "open":
        outcome.status = "rejected"
    return outcome


async def _execute(
    session: AsyncSession, ledger: Ledger, principal: Principal, rec: CardRecord, button: str, channel: str
) -> ActOutcome:
    if button == "cancel":
        return ActOutcome("cancelled", "已取消，没有写入任何数据")
    if button != "confirm" or not rec.action:
        return ActOutcome("done", "已记录你的选择", {"choice": button})

    action = LedgerAppendAction.model_validate(rec.action)
    req = action.request.model_copy(
        update={
            "idempotency_key": action.request.idempotency_key or f"card:{rec.id}",
            "correlation_id": action.request.correlation_id or rec.thread_id,
            "evidence": [
                *action.request.evidence,
                Evidence(
                    kind="approval", ref=f"card:{rec.id}", note=f"{principal.display_name} 在 {channel} 确认"
                ),
            ],
        }
    )
    try:
        async with session.begin_nested():
            res = await ledger.append(session, principal, req)
    except Forbidden as e:
        return ActOutcome("open", f"没有写入：{e}")
    except LedgerError as e:
        return ActOutcome("failed", f"没有写入：{e}", {"error": str(e)[:300]})
    tail = "（模拟数据）" if req.data_mode == "mock" else ""
    return ActOutcome(
        "done",
        f"已确认并记账{tail}，事件序号 {res.seq}",
        {"event_id": res.event_id, "seq": res.seq, "object_id": res.object_id},
    )


_STATUS_TEXT = {
    "done": "已确认",
    "cancelled": "已取消",
    "expired": "已过期",
    "failed": "执行失败",
    "acting": "正在处理",
}
