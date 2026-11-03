"""审批单：Policy 拦下的事件在这里等人审批，全部通过后以申请人身份写入账本。

- 提交：Ledger.append() 抛出 ApprovalRequired 时由调用方（API、卡片确认）调用 submit()；
- 推送：给当前这一级的每个审批人在其绑定的 IM 上发一张“同意 / 驳回”卡片（只有本人能点）；
- 审批：任一审批人处理即完成这一级（并发时只有一个生效），其他人的卡片随即失效；
- 记账：最后一级通过后，以申请人身份追加事件，审批单作为证据；规则已变化或数据冲突则审批单标记失败。

所有推送都在事务提交后由调用方发出（send_notices），与“回复在事务提交后再发”的约定一致。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.cards import Card, CardButton, CardField
from fos.channels.types import ConversationRef
from fos.db.models import Approval, CardRecord, ChannelIdentity, User, utcnow
from fos.governance.principal import Principal, principal_for_user
from fos.ledger.service import (
    AppendRequest,
    ApprovalGrant,
    ApprovalRequired,
    Evidence,
    Ledger,
    LedgerError,
)
from fos.ontology import Ontology
from fos.policy.schema import Decision, Step
from fos.tenancy import TenantRegistry

log = logging.getLogger(__name__)

AdapterLookup = Callable[[str, str], ChannelAdapter | None]  # (租户, 信道) → 可用的适配器

STATUS_TEXT = {
    "pending": "审批中",
    "approved": "已通过",
    "rejected": "已驳回",
    "cancelled": "已撤回",
    "expired": "已过期",
    "failed": "通过但记账失败",
}


def no_channels(_tenant: str, _channel: str) -> ChannelAdapter | None:
    return None


@dataclass
class Notice:
    """事务提交后要发出的一条消息：文字或一张已落库的卡片。"""

    to: ConversationRef
    text: str | None = None
    card: CardRecord | None = None


@dataclass
class Submitted:
    approval: Approval
    approvers: list[str]  # 当前这一级可以审批的人（显示名）
    notices: list[Notice] = field(default_factory=list)

    def message(self) -> str:
        step = self.approval.steps[0]["name"]
        who = "、".join(self.approvers[:5]) if self.approvers else "（暂时没有找到审批人，请联系管理员）"
        return f"需要审批，已提交：{self.approval.title}。第 1 级「{step}」，审批人：{who}"


@dataclass
class DecideResult:
    status: Literal["approved_step", "approved", "rejected", "cancelled", "failed", "not_eligible", "closed"]
    message: str
    notices: list[Notice] = field(default_factory=list)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_open(ap: Approval) -> bool:
    return ap.status == "pending" and _aware(ap.expires_at) > utcnow()


class ApprovalService:
    def __init__(self, *, registry: TenantRegistry, ontologies: dict[str, Ontology], ledger: Ledger):
        self.registry = registry
        self.ontologies = ontologies
        self.ledger = ledger

    # ---------------- 提交 ----------------

    async def submit(
        self,
        session: AsyncSession,
        principal: Principal,
        req: AppendRequest,
        decision: Decision,
        *,
        source: str,
        thread_id: str | None = None,
        lookup: AdapterLookup = no_channels,
    ) -> Submitted:
        if decision.effect != "approve":
            raise ValueError("只有需要审批的事件才能提交审批单")
        onto = self.ontologies[principal.tenant_id]
        ev = onto.events[req.type]
        if ev.op == "create" and not req.object_id:
            req = req.model_copy(update={"object_id": str(uuid.uuid4())})  # 审批期间就有稳定的对象 id
        now = utcnow()
        ap = Approval(
            tenant_id=principal.tenant_id,
            title=_title(onto, req),
            request=req.model_dump(mode="json"),
            rules=[{"id": rid, "name": r.name} for rid, r in decision.rules],
            steps=decision.steps(),
            current_step=0,
            allow_self=decision.allow_self(),
            decisions=[],
            status="pending",
            requester_id=principal.subject_id,
            requester_name=principal.display_name,
            source=source[:64],
            thread_id=thread_id,
            data_mode=req.data_mode,
            notified_cards=[],
            result={},
            expires_at=now + _hours(decision.expires_hours()),
            created_at=now,
            updated_at=now,
        )
        session.add(ap)
        await session.flush()
        approvers, notices = await self._notify_step(session, ap, lookup)
        return Submitted(ap, approvers, notices)

    # ---------------- 审批 ----------------

    async def eligible(self, session: AsyncSession, principal: Principal, ap: Approval) -> bool:
        if principal.tenant_id != ap.tenant_id or not is_open(ap):
            return False
        user = await session.get(User, principal.subject_id)
        return self._eligible(ap, principal, user.login if user else None)

    def _eligible(self, ap: Approval, principal: Principal, login: str | None) -> bool:
        if ap.current_step >= len(ap.steps):
            return False
        if not ap.allow_self and principal.subject_id == ap.requester_id:
            return False  # 申请人不能审批自己的申请
        if any(d["user_id"] == principal.subject_id for d in ap.decisions):
            return False  # 同一个人不能连批两级
        return _step(ap).eligible(principal, login)

    async def decide(
        self,
        session: AsyncSession,
        principal: Principal,
        ap: Approval,
        decision: Literal["approve", "reject"],
        *,
        comment: str = "",
        via: str = "api",
        lookup: AdapterLookup = no_channels,
    ) -> DecideResult:
        if ap.status != "pending":
            return DecideResult("closed", f"这张审批单{STATUS_TEXT.get(ap.status, ap.status)}，不需要再处理")
        if _aware(ap.expires_at) <= utcnow():
            ap.status, ap.updated_at = "expired", utcnow()
            await self._close_cards(session, ap)
            return DecideResult("closed", "这张审批单已过期，请申请人重新提交")
        if not await self.eligible(session, principal, ap):
            if principal.subject_id == ap.requester_id and not ap.allow_self:
                return DecideResult("not_eligible", "不能审批自己提交的申请")
            return DecideResult("not_eligible", f"你不是「{_step(ap).name}」这一级的审批人")

        step = ap.current_step
        # 抢占这一级：并发审批时只有一个人生效（PostgreSQL 下在这里串行）
        claimed = await session.execute(
            update(Approval)
            .where(Approval.id == ap.id, Approval.status == "pending", Approval.current_step == step)
            .values(current_step=step + 1, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.refresh(ap)
            return DecideResult("closed", "这一级已经有人处理了")
        await session.refresh(ap)
        now = utcnow()
        ap.decisions = [
            *ap.decisions,
            {
                "step": step,
                "step_name": ap.steps[step]["name"],
                "user_id": principal.subject_id,
                "name": principal.display_name,
                "decision": decision,
                "comment": comment[:200],
                "via": via,
                "at": now.isoformat(),
            },
        ]
        await self._close_cards(session, ap)

        if decision == "reject":
            ap.status, ap.decided_at, ap.updated_at = "rejected", now, now
            tail = f"，意见：{comment[:100]}" if comment else ""
            notices = await self._notify_user(
                session, ap.tenant_id, ap.requester_id,
                f"你的申请「{ap.title}」被 {principal.display_name} 驳回{tail}。",
                lookup,
            )  # fmt: skip
            return DecideResult("rejected", f"已驳回：{ap.title}", notices)

        if ap.current_step < len(ap.steps):
            _, notices = await self._notify_step(session, ap, lookup)
            return DecideResult("approved_step", f"已同意，转下一级「{_step(ap).name}」", notices)
        return await self._execute(session, ap, lookup)

    async def cancel(
        self,
        session: AsyncSession,
        principal: Principal,
        ap: Approval,
        *,
        lookup: AdapterLookup = no_channels,
    ) -> DecideResult:
        if principal.subject_id != ap.requester_id:
            return DecideResult("not_eligible", "只有申请人可以撤回")
        if ap.status != "pending":
            return DecideResult("closed", f"这张审批单{STATUS_TEXT.get(ap.status, ap.status)}，不能撤回")
        now = utcnow()
        ap.status, ap.decided_at, ap.updated_at = "cancelled", now, now
        await self._close_cards(session, ap)
        return DecideResult("cancelled", f"已撤回：{ap.title}")

    async def _execute(self, session: AsyncSession, ap: Approval, lookup: AdapterLookup) -> DecideResult:
        """最后一级通过：以申请人身份记账（申请人的权限在这一刻重新检查）。"""
        now = utcnow()
        ap.decided_at = ap.updated_at = now
        tenant = self.registry.get(ap.tenant_id)
        requester = await session.get(User, ap.requester_id)
        if tenant is None or requester is None or not requester.is_active:
            ap.status, ap.result = "failed", {"error": "申请人账号已停用，没有记账"}
            return DecideResult("failed", "审批已通过，但申请人账号已停用，没有记账")
        rp = principal_for_user(requester, tenant)
        req = AppendRequest.model_validate(ap.request)
        names = "、".join(d["name"] for d in ap.decisions)
        req = req.model_copy(
            update={
                "idempotency_key": req.idempotency_key or f"approval:{ap.id}",
                "correlation_id": req.correlation_id or ap.thread_id or ap.id,
                "evidence": [
                    *req.evidence,
                    Evidence(kind="approval", ref=f"approval:{ap.id}", note=f"{names} 审批同意"[:500]),
                ],
            }
        )
        grant = ApprovalGrant(ap.id, frozenset(r["id"] for r in ap.rules))
        try:
            async with session.begin_nested():
                res = await self.ledger.append(session, rp, req, approval=grant)
        except ApprovalRequired as e:
            err = f"审批规则已变化，请重新提交（{e}）"
        except LedgerError as e:
            err = str(e)
        else:
            ap.status = "approved"
            ap.result = {"event_id": res.event_id, "seq": res.seq, "object_id": res.object_id}
            tail = "（模拟数据）" if req.data_mode == "mock" else ""
            notices = await self._notify_user(
                session, ap.tenant_id, ap.requester_id,
                f"你的申请「{ap.title}」已审批通过并记账{tail}，事件序号 {res.seq}。",
                lookup,
            )  # fmt: skip
            return DecideResult("approved", f"已同意，审批完成并记账{tail}，事件序号 {res.seq}", notices)
        ap.status, ap.result = "failed", {"error": err[:300]}
        notices = await self._notify_user(
            session,
            ap.tenant_id,
            ap.requester_id,
            f"你的申请「{ap.title}」审批通过，但没有记账：{err}",
            lookup,
        )
        return DecideResult("failed", f"审批已通过，但没有记账：{err}", notices)

    # ---------------- 推送 ----------------

    async def approvers(self, session: AsyncSession, ap: Approval) -> list[User]:
        """当前这一级可以审批的人。单厂用户量小，直接遍历本租户用户。"""
        tenant = self.registry.get(ap.tenant_id)
        if tenant is None or not is_open(ap):
            return []
        users = (
            await session.scalars(
                select(User).where(User.tenant_id == ap.tenant_id, User.is_active.is_(True))
            )
        ).all()
        return [u for u in users if self._eligible(ap, principal_for_user(u, tenant), u.login)]

    async def _notify_step(
        self, session: AsyncSession, ap: Approval, lookup: AdapterLookup
    ) -> tuple[list[str], list[Notice]]:
        from fos.triggers.cards import ApprovalDecideAction, propose_card
        from fos.triggers.intake import thread_for

        users = await self.approvers(session, ap)
        card = _approval_card(self.ontologies[ap.tenant_id], ap)
        notices: list[Notice] = []
        cards: list[str] = []
        for u in users:
            for ident in await identities(session, ap.tenant_id, u.id):
                if lookup(ap.tenant_id, ident.channel) is None:
                    continue
                to = dm_ref(ident)
                rec = await propose_card(
                    session,
                    tenant_id=ap.tenant_id,
                    to=to,
                    thread_id=await thread_for(session, ap.tenant_id, to),
                    card=card,
                    created_by="system:approval",
                    action=ApprovalDecideAction(approval_id=ap.id, step=ap.current_step),
                    allowed_user_id=u.id,
                    ttl=max(_aware(ap.expires_at) - utcnow(), _hours(1)),
                )
                cards.append(rec.id)
                notices.append(Notice(to, card=rec))
        ap.notified_cards = [*ap.notified_cards, *cards]
        await session.flush()
        return [u.display_name for u in users], notices

    async def _notify_user(
        self, session: AsyncSession, tenant_id: str, user_id: str, text: str, lookup: AdapterLookup
    ) -> list[Notice]:
        return [
            Notice(dm_ref(i), text=text)
            for i in await identities(session, tenant_id, user_id)
            if lookup(tenant_id, i.channel) is not None
        ]

    async def _close_cards(self, session: AsyncSession, ap: Approval) -> None:
        """这一级已处理（或审批单关闭）：发给其他审批人的卡片失效。"""
        if not ap.notified_cards:
            return
        await session.execute(
            update(CardRecord)
            .where(
                CardRecord.tenant_id == ap.tenant_id,
                CardRecord.id.in_(ap.notified_cards),
                CardRecord.status == "open",
            )
            .values(status="superseded")
            .execution_options(synchronize_session=False)
        )


async def send_notices(lookup: AdapterLookup, tenant_id: str, notices: list[Notice]) -> list[str]:
    """事务提交后调用。返回发送失败的原因（不影响已落库的结果）。"""
    from fos.triggers.cards import send_card_record

    errors: list[str] = []
    for n in notices:
        adapter = lookup(tenant_id, n.to.channel)
        if adapter is None:
            continue
        if n.card is not None:
            err = await send_card_record(adapter, n.card)
            if err:
                errors.append(err)
        elif n.text:
            try:
                await adapter.send_text(n.to, n.text)
            except ChannelError as e:
                log.warning("通知发送失败：%s", e)
                errors.append(str(e)[:200])
    return errors


# ---------------- 工具 ----------------


def _hours(h: int):
    from datetime import timedelta

    return timedelta(hours=h)


def _step(ap: Approval) -> Step:
    s = ap.steps[min(ap.current_step, len(ap.steps) - 1)]
    return Step.model_validate({k: s.get(k) for k in ("name", "role", "permission", "login")})


def dm_ref(ident: ChannelIdentity) -> ConversationRef:
    return ConversationRef(
        channel=ident.channel,  # type: ignore[arg-type]
        conversation_id=ident.external_id,
        conversation_type="direct",
        user_id=ident.external_id,
    )


async def identities(session: AsyncSession, tenant_id: str, user_id: str) -> list[ChannelIdentity]:
    q = select(ChannelIdentity).where(
        ChannelIdentity.tenant_id == tenant_id, ChannelIdentity.user_id == user_id
    )
    return list((await session.scalars(q)).all())


def _title(onto: Ontology, req: AppendRequest) -> str:
    ev = onto.events[req.type]
    obj = onto.objects[ev.object]
    label = next((str(req.payload[k]) for k in ("name", "code", "title") if req.payload.get(k)), "")
    return f"{ev.name}：{label or obj.name}"[:80]


def fmt_value(v: Any, unit: str | None = None) -> str:
    if isinstance(v, list):
        return "、".join(map(str, v))
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int | float) and unit:
        return f"{v:,} {unit}" if not isinstance(v, float) or not v.is_integer() else f"{int(v):,} {unit}"
    return str(v)


def payload_fields(onto: Ontology, req: AppendRequest, limit: int) -> list[CardField]:
    ev = onto.events[req.type]
    props = onto.objects[ev.object].properties
    out: list[CardField] = []
    for k, v in list(req.payload.items())[:limit]:
        p = props.get(k)
        out.append(
            CardField(label=(p.name if p else k)[:20], value=fmt_value(v, p.unit if p else None)[:200])
        )
    return out


def _approval_card(onto: Ontology, ap: Approval) -> Card:
    req = AppendRequest.model_validate(ap.request)
    ev = onto.events[req.type]
    step = ap.steps[ap.current_step]
    done = "；".join(f"{d['step_name']}：{d['name']} 已同意" for d in ap.decisions)
    text = (
        f"{ap.requester_name} 提交，规则：{'、'.join(r['name'] for r in ap.rules)}。\n"
        f"第 {ap.current_step + 1}/{len(ap.steps)} 级「{step['name']}」。"
        + (f"\n{done}" if done else "")
        + f"\n全部同意后记账「{ev.name}」。"
    )
    return Card(
        title=f"审批：{ap.title}"[:40],
        text=text[:500],
        fields=payload_fields(onto, req, 12),
        buttons=[
            CardButton(id="approve", label="同意", style="primary"),
            CardButton(id="reject", label="驳回", style="danger"),
        ],
        data_mode=ap.data_mode,  # type: ignore[arg-type]
        footer=f"审批单 {ap.id[:8]}",
    )
