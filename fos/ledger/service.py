"""事件账本：所有事实以不可篡改的事件记账（方案 5.1 原则 2、7）。

一次 append 在同一事务里完成：权限 → 证据 → 幂等 → 本体校验 → 引用检查 → Policy（红线、审批）→
分配租户内序号 → 写事件 → 更新投影。任何一步失败，整个事务回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event, ObjectState, TenantCounter, utcnow
from fos.governance.principal import Principal
from fos.ledger.projection import apply_event
from fos.ontology import Ontology, OntologyError
from fos.policy.schema import Decision, Policy


class LedgerError(Exception):
    pass


class Forbidden(LedgerError):
    pass


class NotFound(LedgerError):
    pass


class Conflict(LedgerError):
    pass


class Invalid(LedgerError):
    pass


class ApprovalRequired(LedgerError):
    """命中审批规则：事件没有写入，需要先走审批（fos.policy.approvals）。"""

    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__("需要审批：" + "；".join(r.name for _, r in decision.rules))


@dataclass(frozen=True)
class ApprovalGrant:
    """审批通过的凭据。只由审批服务在内部传入，不能从 API 请求里带进来。"""

    approval_id: str
    rule_ids: frozenset[str]


class Evidence(BaseModel):
    """证据引用。数字从哪里来：照片、文件页码、传感器、审批、计算、IM 消息。"""

    kind: Literal["photo", "document", "sensor", "approval", "calculation", "im_message", "manual"]
    ref: str = Field(min_length=1, max_length=512)  # OSS 对象键、文件 id、点位 id、消息 id…
    page: int | None = Field(default=None, ge=1)
    quote: str | None = Field(default=None, max_length=500)  # 原文片段
    note: str | None = Field(default=None, max_length=500)


class AppendRequest(BaseModel):
    type: str
    object_id: str | None = Field(default=None, min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    occurred_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    causation_id: str | None = None
    correlation_id: str | None = Field(default=None, max_length=64)
    data_mode: Literal["real", "mock"] = "real"
    expected_version: int | None = Field(default=None, ge=0)  # 乐观并发：0 表示对象必须不存在

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("occurred_at 必须带时区")
        return v


class AppendResult(BaseModel):
    event_id: str
    seq: int
    object_type: str
    object_id: str
    version: int
    duplicate: bool = False


class Ledger:
    def __init__(
        self,
        ontology_for: Callable[[str], Ontology],
        policy_for: Callable[[str], Policy | None] | None = None,
    ):
        self._ontology_for = ontology_for
        self._policy_for = policy_for or (lambda _tid: None)

    def evaluate(
        self, tenant_id: str, event_type: str, payload: dict[str, Any], state: dict | None
    ) -> Decision:
        policy = self._policy_for(tenant_id)
        return policy.evaluate(event_type, payload, state) if policy else Decision("allow")

    async def append(
        self,
        session: AsyncSession,
        principal: Principal,
        req: AppendRequest,
        *,
        approval: ApprovalGrant | None = None,
    ) -> AppendResult:
        tenant = principal.tenant_id
        onto = self._ontology_for(tenant)
        evdef = onto.events.get(req.type)
        if evdef is None:
            raise Invalid(f"未知事件类型：{req.type}")
        if not principal.can(evdef.permission):
            raise Forbidden(f"记录“{evdef.name}”需要权限 {evdef.permission}")
        if evdef.evidence == "required" and not req.evidence:
            raise Invalid(f"“{evdef.name}”必须附证据（照片、文件或审批记录）")

        if req.idempotency_key:
            prev = (
                await session.execute(
                    select(Event).where(
                        Event.tenant_id == tenant, Event.idempotency_key == req.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if prev is not None:
                if prev.type != req.type:
                    raise Conflict("幂等键已被另一类事件使用")
                obj = await session.get(ObjectState, (tenant, prev.object_type, prev.object_id))
                return AppendResult(
                    event_id=prev.id,
                    seq=prev.seq,
                    object_type=prev.object_type,
                    object_id=prev.object_id,
                    version=obj.version if obj else 0,
                    duplicate=True,
                )

        creating = evdef.op == "create"
        try:
            payload = onto.check_payload(req.type, req.payload, creating=creating)
        except OntologyError as e:
            raise Invalid(str(e)) from e

        object_id = req.object_id or (str(uuid.uuid4()) if creating else None)
        if object_id is None:
            raise Invalid("更新类事件必须给出 object_id")
        current = await session.get(ObjectState, (tenant, evdef.object, object_id))
        if creating and current is not None:
            raise Conflict(f"{onto.objects[evdef.object].name} {object_id} 已存在")
        if not creating:
            if current is None:
                raise NotFound(f"{onto.objects[evdef.object].name} {object_id} 不存在")
            if current.data_mode != req.data_mode:
                raise Conflict(f"对象是 {current.data_mode} 数据，不能用 {req.data_mode} 事件修改")
        if req.expected_version is not None:
            have = current.version if current else 0
            if have != req.expected_version:
                raise Conflict(f"版本冲突：期望 {req.expected_version}，实际 {have}")

        await self._check_refs(session, onto, tenant, evdef.object, payload, req.data_mode)

        decision = self.evaluate(tenant, req.type, payload, current.state if current else None)
        if decision.effect == "deny":
            raise Forbidden(f"红线：{decision.reason}")
        if decision.effect == "approve" and not (
            approval is not None and set(decision.rule_ids) <= approval.rule_ids
        ):
            raise ApprovalRequired(decision)

        seq = await _next_seq(session, tenant)
        now = utcnow()
        ev = Event(
            tenant_id=tenant,
            seq=seq,
            type=req.type,
            object_type=evdef.object,
            object_id=object_id,
            payload=payload,
            evidence=[e.model_dump(exclude_none=True) for e in req.evidence],
            actor_kind=principal.kind,
            actor_id=principal.subject_id,
            actor_name=principal.display_name,
            data_mode=req.data_mode,
            # 统一存 UTC：PostgreSQL 本来如此；SQLite 不存时区，不转换会按各自时区的钟点混存
            occurred_at=(req.occurred_at or now).astimezone(UTC),
            recorded_at=now,
            idempotency_key=req.idempotency_key,
            causation_id=req.causation_id,
            correlation_id=req.correlation_id,
            ontology_layers=list(onto.layers),
        )
        session.add(ev)
        try:
            await session.flush()
            obj = await apply_event(session, onto, ev, evdef.op)
            await session.flush()
        except IntegrityError as e:
            raise Conflict("并发写入冲突，请重试") from e
        return AppendResult(
            event_id=ev.id, seq=seq, object_type=evdef.object, object_id=object_id, version=obj.version
        )

    async def _check_refs(
        self,
        session: AsyncSession,
        onto: Ontology,
        tenant: str,
        object_type: str,
        payload: dict[str, Any],
        data_mode: str,
    ) -> None:
        for rel, prop in onto.ref_props(object_type).items():
            val = payload.get(rel)
            if val is None:
                continue
            for to_id in val if prop.many else [val]:
                target = await session.get(ObjectState, (tenant, prop.target, to_id))
                if target is None:
                    raise Invalid(f"{prop.name}引用的{onto.objects[prop.target].name} {to_id} 不存在")
                if data_mode == "real" and target.data_mode == "mock":
                    raise Invalid(f"真实数据不能引用模拟对象 {prop.target}/{to_id}")


async def _next_seq(session: AsyncSession, tenant: str) -> int:
    """行锁递增，租户内 seq 连续。同一租户的追加因此串行化，单厂规模足够。"""
    stmt = (
        update(TenantCounter)
        .where(TenantCounter.tenant_id == tenant)
        .values(last_seq=TenantCounter.last_seq + 1)
        .returning(TenantCounter.last_seq)
    )
    seq = (await session.execute(stmt)).scalar_one_or_none()
    if seq is not None:
        return seq
    try:
        async with session.begin_nested():
            session.add(TenantCounter(tenant_id=tenant, last_seq=0))
    except IntegrityError:
        pass  # 并发下另一事务刚建好
    return (await session.execute(stmt)).scalar_one()
