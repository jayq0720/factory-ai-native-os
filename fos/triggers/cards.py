"""卡片确认（L2 单步获准，方案 2.7）。

Agent 或业务代码提出一张卡片，附上“确认后要执行的动作”：
- ledger.append：向事件账本追加一个事件；命中审批规则时改为以点击人名义提交审批单（W8）；
- approval.decide：审批卡片，同意 / 驳回一张审批单的当前这一级。
动作以**点击人自己的身份和权限**执行，Agent 本身不获得写权限；一张卡片只生效一次。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.cards import Card
from fos.channels.types import ConversationRef
from fos.db.models import CardRecord, utcnow
from fos.governance.principal import Principal
from fos.ledger import Ledger
from fos.ledger.service import AppendRequest, ApprovalRequired, Evidence, Forbidden, LedgerError

if TYPE_CHECKING:
    from fos.policy.approvals import AdapterLookup, ApprovalService, Notice

log = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混的 I L O 0 1
CODE_LEN = 5
DEFAULT_TTL = timedelta(hours=72)


class LedgerAppendAction(BaseModel):
    kind: Literal["ledger.append"] = "ledger.append"
    request: AppendRequest


class ApprovalDecideAction(BaseModel):
    kind: Literal["approval.decide"] = "approval.decide"
    approval_id: str = Field(min_length=1, max_length=36)
    step: int = Field(ge=0)


CardAction = LedgerAppendAction | ApprovalDecideAction


def parse_action(data: dict[str, Any] | None) -> CardAction | None:
    if not data:
        return None
    if data.get("kind") == "approval.decide":
        return ApprovalDecideAction.model_validate(data)
    return LedgerAppendAction.model_validate(data)


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
    action: CardAction | None = None,
    required_permission: str | None = None,
    allowed_user_id: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> CardRecord:
    """记录一张带按钮的卡片。提交事务后用 send_card_record() 发出。"""
    if not card.buttons:
        raise ValueError("需要确认的卡片至少要有一个按钮")
    if isinstance(action, LedgerAppendAction):
        if card.button("confirm") is None:
            raise ValueError("带动作的卡片必须有 id 为 confirm 的按钮")
        if action.request.data_mode != card.data_mode:
            raise ValueError("卡片的 data_mode 必须与要写入的事件一致")
    if isinstance(action, ApprovalDecideAction) and {b.id for b in card.buttons} != {"approve", "reject"}:
        raise ValueError("审批卡片的按钮必须是 approve 和 reject")
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
    notices: list[Notice] = field(default_factory=list)  # 提交后要发的通知（审批卡片、结果通知）


async def act_on_card(
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    rec: CardRecord,
    action_id: str,
    *,
    channel: str,
    approvals: ApprovalService | None = None,
    lookup: AdapterLookup | None = None,
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

    outcome = await _execute(session, ledger, principal, rec, button.id, channel, approvals, lookup)
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
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    rec: CardRecord,
    button: str,
    channel: str,
    approvals: ApprovalService | None,
    lookup: AdapterLookup | None,
) -> ActOutcome:
    from fos.policy.approvals import no_channels

    lookup = lookup or no_channels
    action = parse_action(rec.action)
    if isinstance(action, ApprovalDecideAction):
        return await _decide(session, principal, action, button, channel, approvals, lookup)
    if button == "cancel":
        return ActOutcome("cancelled", "已取消，没有写入任何数据")
    if button != "confirm" or action is None:
        return ActOutcome("done", "已记录你的选择", {"choice": button})

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
    except ApprovalRequired as e:
        if approvals is None:
            return ActOutcome("failed", f"没有写入：{e}", {"error": str(e)[:300]})
        # 点击人就是申请人：以他的名义提交审批单，审批通过后再记账
        sub = await approvals.submit(
            session,
            principal,
            req,
            e.decision,
            source=f"card:{rec.id}",
            thread_id=rec.thread_id,
            lookup=lookup,
        )
        return ActOutcome("done", sub.message(), {"approval_id": sub.approval.id}, sub.notices)
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


async def _decide(
    session: AsyncSession,
    principal: Principal,
    action: ApprovalDecideAction,
    button: str,
    channel: str,
    approvals: ApprovalService | None,
    lookup: AdapterLookup,
) -> ActOutcome:
    from fos.db.models import Approval

    ap = await session.get(Approval, action.approval_id)
    if approvals is None or ap is None or ap.tenant_id != principal.tenant_id:
        return ActOutcome("failed", "审批单不存在")
    if ap.current_step != action.step:
        return ActOutcome("superseded", "这一级已经有人处理了")
    r = await approvals.decide(
        session, principal, ap, "approve" if button == "approve" else "reject", via=channel, lookup=lookup
    )
    if r.status == "not_eligible":
        return ActOutcome("open", r.message)
    status = "superseded" if r.status == "closed" else "done"
    return ActOutcome(status, r.message, {"approval_id": ap.id, "approval": r.status}, r.notices)


_STATUS_TEXT = {
    "superseded": "已由他人处理或已失效",
    "done": "已确认",
    "cancelled": "已取消",
    "expired": "已过期",
    "failed": "执行失败",
    "acting": "正在处理",
}
