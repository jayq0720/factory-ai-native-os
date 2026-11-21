"""设备管理（EAM）核心服务（W24）。

- 设备台账：登记、扩展属性维护、A 类关键设备报废走总经理审批；
- 预防性保养（PM）：周期计划、到期/超期看板、完工自动顺延下次保养日、去重到期提醒；
- 维修工单（WO）：故障报修自动切换设备状态与派发待办、开工/完工记录根因/备件/停机时长并恢复运行；
- 日常点检：记录点检结果，发现异常自动开启维修工单；
- 备件库存联动：从仓储账本投影汇总 `category == spare_part` 物料现存与安全库存告警；
- 可靠性报表：确定性计算设备可用率、MTBF、MTTR。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState, Todo, utcnow
from fos.eam.config import EamConfig
from fos.eam.guards import (
    EQUIP_REG_EVENT,
    EQUIP_RETIRE_EVENT,
    EQUIP_UPD_EVENT,
    INSP_LOG_EVENT,
    PM_CREATE_EVENT,
    PM_DONE_EVENT,
    WO_DONE_EVENT,
    WO_OPEN_EVENT,
    WO_START_EVENT,
)
from fos.eam.metrics import compute_reliability
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.data import get
from fos.orders.service import new_id

EQUIP_STATUS_TEXT = {"running": "运行中", "idle": "闲置", "maintenance": "维修保养", "retired": "已报废"}
EQUIP_CAT_TEXT = {
    "kiln": "窑炉",
    "press": "压机",
    "mixer": "混炼机",
    "dryer": "干燥窑",
    "crusher": "破碎机",
    "dust_collector": "除尘器",
    "crane": "行车",
    "other": "其他",
}
WO_KIND_TEXT = {"breakdown": "故障维修", "preventive": "预防保养", "inspection": "点检消缺"}
WO_PRIO_TEXT = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}
WO_PRIO_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
WO_STATUS_TEXT = {"open": "待处理", "in_progress": "维修中", "completed": "已完工", "cancelled": "已取消"}


class EamError(ValueError):
    """设备管理业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class EamNotFound(EamError):
    def __init__(self, msg: str):
        super().__init__(msg, 404)


class EamConflict(EamError):
    def __init__(self, msg: str):
        super().__init__(msg, 409)


async def _scan(session: AsyncSession, tenant: str, obj_type: str, mode: str = "real") -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == tenant,
            ObjectState.object_type == obj_type,
            ObjectState.data_mode == mode,
        )
    )
    return sorted(rows, key=lambda o: o.created_seq)


# ---------------- 设备台账 ----------------


class EquipmentIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=80)
    category: Literal["kiln", "press", "mixer", "dryer", "crusher", "dust_collector", "crane", "other"] = (
        "other"
    )
    location: str | None = Field(default=None, max_length=120)
    manufacturer: str | None = Field(default=None, max_length=120)
    commission_date: date | None = None
    criticality: Literal["A", "B", "C"] = "B"
    pm_cycle_days: int | None = Field(default=None, ge=1, le=3650)
    next_pm_date: date | None = None
    status: Literal["running", "idle", "maintenance"] = "running"
    note: str | None = Field(default=None, max_length=500)
    data_mode: Literal["real", "mock"] = "real"


class EquipmentRetireIn(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _equip_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "running"
    cat = s.get("category") or "other"
    return {
        "id": o.object_id,
        "code": s.get("code") or o.object_id,
        "name": s.get("name") or o.object_id,
        "model": s.get("model"),
        "category": cat,
        "category_text": EQUIP_CAT_TEXT.get(cat, "其他"),
        "location": s.get("location"),
        "manufacturer": s.get("manufacturer"),
        "commission_date": s.get("commission_date"),
        "criticality": s.get("criticality") or "B",
        "pm_cycle_days": s.get("pm_cycle_days"),
        "last_pm_date": s.get("last_pm_date"),
        "next_pm_date": s.get("next_pm_date"),
        "status": st,
        "status_text": EQUIP_STATUS_TEXT.get(st, st),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def equipment_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    items = [_equip_out(o) for o in await _scan(session, tenant, "equipment", mode)]
    if status:
        items = [x for x in items if x["status"] == status]
    order = {"maintenance": 0, "running": 1, "idle": 2, "retired": 3}
    items.sort(key=lambda x: (order.get(x["status"], 9), x["criticality"], x["code"]))
    return {
        "data_mode": mode,
        "items": items,
        "counts": {
            "total": len(items),
            "running": sum(1 for x in items if x["status"] == "running"),
            "maintenance": sum(1 for x in items if x["status"] == "maintenance"),
            "idle": sum(1 for x in items if x["status"] == "idle"),
            "retired": sum(1 for x in items if x["status"] == "retired"),
        },
    }


async def register_equipment(
    session: AsyncSession, principal: Principal, ledger: Ledger, body: EquipmentIn
) -> dict[str, Any]:
    oid = f"EQ-{body.code}"[:64]
    base_payload: dict[str, Any] = {"name": body.name, "status": body.status}
    if body.model:
        base_payload["model"] = body.model
    if body.location:
        base_payload["location"] = body.location
    res1 = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=EQUIP_REG_EVENT,
            object_id=oid,
            payload=base_payload,
            evidence=[Evidence(kind="manual", ref=f"eam:{oid}", note="登记设备台账")],
            data_mode=body.data_mode,
        ),
    )
    ext: dict[str, Any] = {
        "code": body.code,
        "category": body.category,
        "criticality": body.criticality,
    }
    if body.manufacturer:
        ext["manufacturer"] = body.manufacturer
    if body.commission_date:
        ext["commission_date"] = body.commission_date.isoformat()
    if body.pm_cycle_days is not None:
        ext["pm_cycle_days"] = body.pm_cycle_days
    if body.next_pm_date:
        ext["next_pm_date"] = body.next_pm_date.isoformat()
    if body.note:
        ext["note"] = body.note
    res2 = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=EQUIP_UPD_EVENT,
            object_id=oid,
            payload=ext,
            expected_version=res1.version,
            data_mode=body.data_mode,
        ),
    )
    return {"id": oid, "event_id": res2.event_id, "seq": res2.seq}


async def retire_equipment_request(
    session: AsyncSession, principal: Principal, equip_id: str, body: EquipmentRetireIn
) -> AppendRequest:
    o = await get(session, principal.tenant_id, "equipment", equip_id)
    if o is None:
        raise EamNotFound(f"设备 {equip_id} 不存在")
    if o.state.get("status") == "retired":
        raise EamConflict(f"设备 {equip_id} 已报废停用")
    ev = body.evidence or [Evidence(kind="manual", ref=f"eam:{equip_id}:retire", note=body.note[:200])]
    return AppendRequest(
        type=EQUIP_RETIRE_EVENT,
        object_id=equip_id,
        payload={"status": "retired", "note": body.note},
        evidence=ev,
        expected_version=o.version,
        data_mode=o.data_mode,
    )


# ---------------- 预防性保养计划（PM） ----------------


class PmPlanIn(BaseModel):
    equipment: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    cycle_days: int = Field(ge=1, le=3650)
    last_done_date: date | None = None
    next_due_date: date
    checklist: str | None = Field(default=None, max_length=1000)
    assignee_role: str = Field(default="shift_lead", max_length=32)
    note: str | None = Field(default=None, max_length=500)
    data_mode: Literal["real", "mock"] = "real"


class PmCompleteIn(BaseModel):
    done_date: date
    next_due_date: date | None = None
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _pm_out(o: ObjectState, cfg: EamConfig, today: date, eq_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    due_raw = s.get("next_due_date")
    due = date.fromisoformat(due_raw) if due_raw else None
    days_left = (due - today).days if due else None
    if s.get("status") != "active":
        pm_state, pm_text = "inactive", "已暂停/停用"
    elif days_left is not None and days_left < 0:
        pm_state, pm_text = "overdue", f"已超期 {-days_left} 天"
    elif days_left is not None and days_left <= cfg.pm_remind_days:
        pm_state, pm_text = "due_soon", f"{days_left} 天后到期"
    else:
        pm_state, pm_text = "ok", "正常"
    eid = s.get("equipment") or ""
    return {
        "id": o.object_id,
        "plan_no": s.get("plan_no") or o.object_id,
        "equipment": eid,
        "equipment_name": eq_names.get(eid, eid),
        "title": s.get("title"),
        "cycle_days": s.get("cycle_days"),
        "last_done_date": s.get("last_done_date"),
        "next_due_date": due_raw,
        "days_left": days_left,
        "pm_state": pm_state,
        "pm_text": pm_text,
        "checklist": s.get("checklist"),
        "assignee_role": s.get("assignee_role"),
        "status": s.get("status"),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def pm_board(
    session: AsyncSession, tenant: str, cfg: EamConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    eq_rows = await _scan(session, tenant, "equipment", mode)
    eq_names = {e.object_id: e.state.get("name") or e.object_id for e in eq_rows}
    items = [_pm_out(o, cfg, today, eq_names) for o in await _scan(session, tenant, "maintenance_plan", mode)]
    order = {"overdue": 0, "due_soon": 1, "ok": 2, "inactive": 3}
    items.sort(key=lambda x: (order.get(x["pm_state"], 9), x["next_due_date"] or "9999-12-31"))
    return {
        "date": today.isoformat(),
        "data_mode": mode,
        "items": items,
        "counts": {
            "total": len(items),
            "overdue": sum(1 for x in items if x["pm_state"] == "overdue"),
            "due_soon": sum(1 for x in items if x["pm_state"] == "due_soon"),
            "ok": sum(1 for x in items if x["pm_state"] == "ok"),
        },
    }


async def create_pm_plan(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: PmPlanIn
) -> dict[str, Any]:
    eq = await get(session, principal.tenant_id, "equipment", body.equipment)
    if eq is None:
        raise EamNotFound(f"设备 {body.equipment} 不存在")
    pid = new_id("PM", tz)
    payload: dict[str, Any] = {
        "plan_no": pid,
        "equipment": body.equipment,
        "title": body.title,
        "cycle_days": body.cycle_days,
        "next_due_date": body.next_due_date.isoformat(),
        "assignee_role": body.assignee_role,
        "status": "active",
    }
    if body.last_done_date:
        payload["last_done_date"] = body.last_done_date.isoformat()
    if body.checklist:
        payload["checklist"] = body.checklist
    if body.note:
        payload["note"] = body.note
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=PM_CREATE_EVENT,
            object_id=pid,
            payload=payload,
            evidence=[Evidence(kind="manual", ref=f"pm:{pid}", note="创建设备保养计划")],
            data_mode=body.data_mode,
        ),
    )
    return {"id": pid, "event_id": res.event_id, "seq": res.seq}


async def complete_pm(
    session: AsyncSession, principal: Principal, ledger: Ledger, plan_id: str, body: PmCompleteIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "maintenance_plan", plan_id)
    if o is None:
        raise EamNotFound(f"保养计划 {plan_id} 不存在")
    cycle = int(o.state.get("cycle_days") or 30)
    next_due = body.next_due_date or (body.done_date + timedelta(days=cycle))
    payload: dict[str, Any] = {
        "last_done_date": body.done_date.isoformat(),
        "next_due_date": next_due.isoformat(),
    }
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="manual", ref=f"pm:{plan_id}:done", note="保养完成登记")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=PM_DONE_EVENT,
            object_id=plan_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )
    # 同步刷新关联设备的上次/下次保养日期
    eid = o.state.get("equipment")
    if eid and principal.can("eam.equipment:write"):
        eq = await get(session, principal.tenant_id, "equipment", eid)
        if eq is not None and eq.state.get("status") != "retired":
            await ledger.append(
                session,
                principal,
                AppendRequest(
                    type=EQUIP_UPD_EVENT,
                    object_id=eid,
                    payload={
                        "last_pm_date": body.done_date.isoformat(),
                        "next_pm_date": next_due.isoformat(),
                    },
                    causation_id=res.event_id,
                    expected_version=eq.version,
                    data_mode=eq.data_mode,
                ),
            )
    return res


async def remind_pm_due(
    session: AsyncSession, tenant: str, cfg: EamConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    """为即将到期或已超期的保养计划生成去重待办。"""
    board = await pm_board(session, tenant, cfg, today, mode=mode)
    existing = {
        (t.object_id, t.due_at.date().isoformat() if t.due_at else "")
        for t in (
            await session.scalars(
                select(Todo).where(
                    Todo.tenant_id == tenant,
                    Todo.object_type == "maintenance_plan",
                    Todo.status == "open",
                    Todo.data_mode == mode,
                )
            )
        ).all()
    }
    created: list[str] = []
    default_role = cfg.notify_roles[0] if cfg.notify_roles else "shift_lead"
    for it in board["items"]:
        if it["pm_state"] not in ("overdue", "due_soon"):
            continue
        due_str = it["next_due_date"] or ""
        if (it["id"], due_str) in existing:
            continue
        due_dt = datetime.combine(date.fromisoformat(due_str), time(17, 0), tzinfo=UTC) if due_str else None
        prefix = "【超期】" if it["pm_state"] == "overdue" else "【待保养】"
        session.add(
            Todo(
                tenant_id=tenant,
                title=f"{prefix}{it['equipment_name']}：{it['title']}"[:80],
                body=f"到期日 {due_str}（{it['pm_text']}），计划号 {it['plan_no']}。",
                assignee_role=it.get("assignee_role") or default_role,
                object_type="maintenance_plan",
                object_id=it["id"],
                due_at=due_dt,
                source="system:eam",
                data_mode=mode,
                created_by="system",
            )
        )
        created.append(it["id"])
    await session.flush()
    return {"created": created, "count": len(created), "counts": board["counts"]}


# ---------------- 维修工单（WO）与日常点检 ----------------


class SparePartUse(BaseModel):
    material: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)
    qty: FiniteFloat = Field(gt=0)
    unit: str = Field(default="件", max_length=24)


class WorkOrderOpenIn(BaseModel):
    equipment: str = Field(min_length=1, max_length=64)
    kind: Literal["breakdown", "preventive", "inspection"] = "breakdown"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    symptom: str = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class WorkOrderStartIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class WorkOrderCompleteIn(BaseModel):
    root_cause: str | None = Field(default=None, max_length=500)
    action_taken: str = Field(min_length=1, max_length=1000)
    spare_parts: list[SparePartUse] = Field(default_factory=list, max_length=30)
    downtime_minutes: FiniteFloat = Field(default=0.0, ge=0)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class InspectionLogIn(BaseModel):
    equipment: str = Field(min_length=1, max_length=64)
    result: Literal["normal", "abnormal"]
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    note: str | None = Field(default=None, max_length=500)
    data_mode: Literal["real", "mock"] = "real"


def _wo_out(o: ObjectState, eq_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    raw_sp = s.get("spare_parts")
    if isinstance(raw_sp, str):
        try:
            sp = json.loads(raw_sp)
        except json.JSONDecodeError:
            sp = []
    else:
        sp = raw_sp or []
    eid = s.get("equipment") or ""
    st = s.get("status") or "open"
    kind = s.get("kind") or "breakdown"
    prio = s.get("priority") or "normal"
    return {
        "id": o.object_id,
        "wo_no": s.get("wo_no") or o.object_id,
        "equipment": eid,
        "equipment_name": eq_names.get(eid, eid),
        "kind": kind,
        "kind_text": WO_KIND_TEXT.get(kind, kind),
        "priority": prio,
        "priority_text": WO_PRIO_TEXT.get(prio, prio),
        "symptom": s.get("symptom"),
        "root_cause": s.get("root_cause"),
        "action_taken": s.get("action_taken"),
        "spare_parts": sp,
        "downtime_minutes": float(s.get("downtime_minutes") or 0.0),
        "reported_at": s.get("reported_at"),
        "started_at": s.get("started_at"),
        "completed_at": s.get("completed_at"),
        "status": st,
        "status_text": WO_STATUS_TEXT.get(st, st),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def work_orders_board(
    session: AsyncSession,
    tenant: str,
    *,
    status: str | None = None,
    equipment: str | None = None,
    mode: str = "real",
) -> list[dict[str, Any]]:
    eq_rows = await _scan(session, tenant, "equipment", mode)
    eq_names = {e.object_id: e.state.get("name") or e.object_id for e in eq_rows}
    rows = [_wo_out(o, eq_names) for o in await _scan(session, tenant, "work_order", mode)]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if equipment:
        rows = [r for r in rows if r["equipment"] == equipment]
    rows.sort(
        key=lambda r: (
            r["status"] in ("completed", "cancelled"),
            WO_PRIO_RANK.get(r["priority"], 9),
            r["id"],
        )
    )
    return rows


async def open_work_order(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    cfg: EamConfig,
    tz: ZoneInfo,
    body: WorkOrderOpenIn,
    *,
    causation_id: str | None = None,
) -> dict[str, Any]:
    eq = await get(session, principal.tenant_id, "equipment", body.equipment)
    if eq is None:
        raise EamNotFound(f"设备 {body.equipment} 不存在")
    if eq.state.get("status") == "retired":
        raise EamConflict(f"设备 {body.equipment} 已报废，不能开立维修工单")
    wo_id = new_id("WO", tz)
    at = utcnow().isoformat()
    payload: dict[str, Any] = {
        "wo_no": wo_id,
        "equipment": body.equipment,
        "kind": body.kind,
        "priority": body.priority,
        "symptom": body.symptom,
        "reported_at": at,
        "status": "open",
    }
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="manual", ref=f"wo:{wo_id}", note=body.symptom[:200])]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=WO_OPEN_EVENT,
            object_id=wo_id,
            payload=payload,
            evidence=ev,
            causation_id=causation_id,
            data_mode=body.data_mode,
        ),
    )
    # 高/紧急故障自动将设备置为 maintenance
    if (
        body.kind == "breakdown"
        and body.priority in ("high", "urgent")
        and eq.state.get("status") == "running"
        and principal.can("eam.equipment:write")
    ):
        await ledger.append(
            session,
            principal,
            AppendRequest(
                type=EQUIP_UPD_EVENT,
                object_id=body.equipment,
                payload={"status": "maintenance"},
                causation_id=res.event_id,
                expected_version=eq.version,
                data_mode=eq.data_mode,
            ),
        )
    role = cfg.notify_roles[0] if cfg.notify_roles else "shift_lead"
    session.add(
        Todo(
            tenant_id=principal.tenant_id,
            title=f"维修工单 {wo_id}：{eq.state.get('name') or body.equipment}"[:80],
            body=f"优先级：{WO_PRIO_TEXT.get(body.priority, body.priority)}；现象：{body.symptom}",
            assignee_role=role,
            object_type="work_order",
            object_id=wo_id,
            source="system:eam",
            data_mode=body.data_mode,
            created_by=principal.subject_id,
        )
    )
    return {"id": wo_id, "event_id": res.event_id, "seq": res.seq}


async def start_work_order(
    session: AsyncSession, principal: Principal, ledger: Ledger, wo_id: str, body: WorkOrderStartIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "work_order", wo_id)
    if o is None:
        raise EamNotFound(f"维修工单 {wo_id} 不存在")
    payload: dict[str, Any] = {"started_at": utcnow().isoformat(), "status": "in_progress"}
    if body.note:
        payload["note"] = body.note
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=WO_START_EVENT,
            object_id=wo_id,
            payload=payload,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def complete_work_order(
    session: AsyncSession, principal: Principal, ledger: Ledger, wo_id: str, body: WorkOrderCompleteIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "work_order", wo_id)
    if o is None:
        raise EamNotFound(f"维修工单 {wo_id} 不存在")
    payload: dict[str, Any] = {
        "action_taken": body.action_taken,
        "spare_parts": json.dumps([p.model_dump() for p in body.spare_parts], ensure_ascii=False),
        "downtime_minutes": round(float(body.downtime_minutes), 1),
        "completed_at": utcnow().isoformat(),
        "status": "completed",
    }
    if body.root_cause:
        payload["root_cause"] = body.root_cause
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="manual", ref=f"wo:{wo_id}:done", note=body.action_taken[:200])]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=WO_DONE_EVENT,
            object_id=wo_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )
    # 维修完工后，若设备原为 maintenance，自动恢复 running
    eid = o.state.get("equipment")
    if eid and principal.can("eam.equipment:write"):
        eq = await get(session, principal.tenant_id, "equipment", eid)
        if eq is not None and eq.state.get("status") == "maintenance":
            await ledger.append(
                session,
                principal,
                AppendRequest(
                    type=EQUIP_UPD_EVENT,
                    object_id=eid,
                    payload={"status": "running"},
                    causation_id=res.event_id,
                    expected_version=eq.version,
                    data_mode=eq.data_mode,
                ),
            )
    return res


async def log_inspection(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    cfg: EamConfig,
    tz: ZoneInfo,
    body: InspectionLogIn,
) -> dict[str, Any]:
    eq = await get(session, principal.tenant_id, "equipment", body.equipment)
    if eq is None:
        raise EamNotFound(f"设备 {body.equipment} 不存在")
    lid = new_id("CHK", tz)
    payload: dict[str, Any] = {
        "equipment": body.equipment,
        "checked_at": utcnow().isoformat(),
        "result": body.result,
        "items": json.dumps(body.items, ensure_ascii=False),
    }
    if body.note:
        payload["note"] = body.note
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=INSP_LOG_EVENT,
            object_id=lid,
            payload=payload,
            data_mode=body.data_mode,
        ),
    )
    wo_info = None
    if body.result == "abnormal" and cfg.auto_wo_on_abnormal and principal.can("eam.work_order:write"):
        wo_info = await open_work_order(
            session,
            principal,
            ledger,
            cfg,
            tz,
            WorkOrderOpenIn(
                equipment=body.equipment,
                kind="inspection",
                priority="high",
                symptom=f"日常点检发现异常：{body.note or '详见点检项'}",
                data_mode=body.data_mode,
            ),
            causation_id=res.event_id,
        )
    return {"id": lid, "event_id": res.event_id, "seq": res.seq, "result": body.result, "work_order": wo_info}


# ---------------- 备件库存与设备可靠性报表 ----------------


async def spare_parts_board(session: AsyncSession, tenant: str, *, mode: str = "real") -> dict[str, Any]:
    """从仓储账本投影汇总备品备件（category == spare_part）现存与低库存预警。"""
    mats = [
        m for m in await _scan(session, tenant, "material", mode) if m.state.get("category") == "spare_part"
    ]
    lots = await _scan(session, tenant, "material_lot", mode)
    moves = await _scan(session, tenant, "stock_move", mode)
    on_hand: dict[str, float] = {}
    for lot in lots:
        mid = lot.state.get("material")
        if mid and lot.state.get("status") != "rejected":
            on_hand[mid] = on_hand.get(mid, 0.0) + float(lot.state.get("qty") or 0.0)
    for mv in moves:
        mid = mv.state.get("material")
        if not mid:
            continue
        q = float(mv.state.get("qty") or 0.0)
        k = mv.state.get("kind")
        if k in ("issue", "count_loss"):
            on_hand[mid] = on_hand.get(mid, 0.0) - q
        elif k in ("return", "count_gain"):
            on_hand[mid] = on_hand.get(mid, 0.0) + q

    items: list[dict[str, Any]] = []
    for m in mats:
        s = m.state
        qty = round(max(on_hand.get(m.object_id, 0.0), 0.0), 4)
        safety = float(s["safety_stock"]) if s.get("safety_stock") is not None else None
        low = safety is not None and qty < safety
        items.append(
            {
                "id": m.object_id,
                "name": s.get("name") or m.object_id,
                "spec": s.get("spec"),
                "unit": s.get("unit") or "件",
                "on_hand": qty,
                "safety_stock": safety,
                "low_stock": low,
            }
        )
    items.sort(key=lambda x: (not x["low_stock"], x["id"]))
    return {
        "data_mode": mode,
        "items": items,
        "counts": {"total": len(items), "low_stock": sum(1 for x in items if x["low_stock"])},
    }


async def reliability_report(
    session: AsyncSession,
    tenant: str,
    cfg: EamConfig,
    *,
    window_days: int | None = None,
    equipment: str | None = None,
    mode: str = "real",
) -> dict[str, Any]:
    days = window_days or cfg.default_window_days
    cutoff = (utcnow() - timedelta(days=days)).isoformat()
    wos = await work_orders_board(session, tenant, equipment=equipment, mode=mode)
    in_win = [w for w in wos if (w.get("reported_at") or "") >= cutoff]
    m = compute_reliability(
        window_days=days, planned_hours_per_day=cfg.planned_hours_per_day, work_orders=in_win
    )
    return {
        "equipment": equipment,
        "data_mode": mode,
        "metrics": m.out(),
        "work_orders_in_window": len(in_win),
    }
