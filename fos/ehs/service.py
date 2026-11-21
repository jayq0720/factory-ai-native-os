"""EHS 安全环保与特种作业核心服务（W25）。

- 安全隐患排查与闭环：上报、登记整改措施、复查销项（守卫强制先整改后销项）、到期去重提醒；
- 特种作业票：动火/受限空间走总经理审批策略，守卫校验安全措施与有效期上限，支持完工关闭/撤销；
- 安全事故与险情：登记、根因与纠正预防措施闭环；
- 环保排放监测：确定性限值判定，超标自动开启重大安全隐患并派发待办；
- EHS 合规看板：确定性统计隐患闭环率、特种作业、事故与环保达标率。
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState, Todo, utcnow
from fos.ehs.compliance import compute_ehs_kpis, evaluate_env_reading
from fos.ehs.config import EhsConfig
from fos.ehs.guards import (
    ENV_RECORD_EVENT,
    HAZARD_RECTIFY_EVENT,
    HAZARD_REPORT_EVENT,
    HAZARD_VERIFY_EVENT,
    INCIDENT_CLOSE_EVENT,
    INCIDENT_REPORT_EVENT,
    PERMIT_CLOSE_EVENT,
    PERMIT_ISSUE_EVENT,
)
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.data import get
from fos.orders.service import new_id

HAZARD_CAT_TEXT = {
    "dust": "粉尘/颗粒物",
    "gas": "有害气体/烟气",
    "electrical": "电气安全",
    "mechanical": "机械伤害",
    "fire": "消防火灾",
    "high_temp": "高温灼烫",
    "ppe": "劳保穿戴",
    "other": "其他",
}
HAZARD_LEVEL_TEXT = {"general": "一般隐患", "major": "重大隐患"}
HAZARD_STATUS_TEXT = {"open": "待整改", "rectifying": "整改中", "verified": "已销项"}
PERMIT_KIND_TEXT = {
    "hot_work": "动火作业",
    "confined_space": "受限空间",
    "high_altitude": "高处作业",
    "electrical": "临时用电",
    "lifting": "吊装作业",
}
PERMIT_STATUS_TEXT = {"pending": "待审批", "active": "作业中", "closed": "已完工", "revoked": "已撤销"}
INCIDENT_KIND_TEXT = {
    "near_miss": "未遂险情",
    "minor_injury": "轻伤事故",
    "major_injury": "重伤事故",
    "equipment_fire": "设备火灾",
    "env_leak": "环保泄漏",
}
INCIDENT_STATUS_TEXT = {"open": "待调查", "investigated": "调查中", "closed": "已闭环"}


class EhsError(ValueError):
    """EHS 业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class EhsNotFound(EhsError):
    def __init__(self, msg: str):
        super().__init__(msg, 404)


async def _scan(session: AsyncSession, tenant: str, obj_type: str, mode: str = "real") -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == tenant,
            ObjectState.object_type == obj_type,
            ObjectState.data_mode == mode,
        )
    )
    return sorted(rows, key=lambda o: o.created_seq)


# ---------------- 安全隐患排查 ----------------


class HazardIn(BaseModel):
    location: str = Field(min_length=1, max_length=120)
    category: Literal["dust", "gas", "electrical", "mechanical", "fire", "high_temp", "ppe", "other"] = (
        "other"
    )
    level: Literal["general", "major"] = "general"
    description: str = Field(min_length=1, max_length=1000)
    due_date: date
    assignee_role: str | None = Field(default=None, max_length=64)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class HazardRectifyIn(BaseModel):
    rectification_plan: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class HazardVerifyIn(BaseModel):
    verified_note: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _hazard_out(o: ObjectState, today: date, remind_days: int) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "open"
    cat = s.get("category") or "other"
    lvl = s.get("level") or "general"
    due_str = s.get("due_date")
    overdue = False
    due_soon = False
    if st != "verified" and due_str:
        with suppress(ValueError):
            d = date.fromisoformat(str(due_str))
            overdue = d < today
            due_soon = today <= d <= today + timedelta(days=remind_days)
    return {
        "id": o.object_id,
        "hazard_no": s.get("hazard_no") or o.object_id,
        "location": s.get("location") or "",
        "category": cat,
        "category_text": HAZARD_CAT_TEXT.get(cat, cat),
        "level": lvl,
        "level_text": HAZARD_LEVEL_TEXT.get(lvl, lvl),
        "description": s.get("description") or "",
        "rectification_plan": s.get("rectification_plan"),
        "due_date": due_str,
        "assignee_role": s.get("assignee_role"),
        "status": st,
        "status_text": HAZARD_STATUS_TEXT.get(st, st),
        "verified_note": s.get("verified_note"),
        "reported_at": s.get("reported_at"),
        "closed_at": s.get("closed_at"),
        "overdue": overdue,
        "due_soon": due_soon,
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def hazards_board(
    session: AsyncSession,
    tenant: str,
    cfg: EhsConfig,
    today: date,
    *,
    status: str | None = None,
    mode: str = "real",
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "safety_hazard", mode)
    items = [_hazard_out(o, today, cfg.hazard_remind_days) for o in rows]
    if status:
        items = [it for it in items if it["status"] == status]
    items.sort(key=lambda x: (x["status"] == "verified", not x["overdue"], x["level"] != "major", x["id"]))
    counts = {
        "total": len(items),
        "open": sum(1 for x in items if x["status"] == "open"),
        "rectifying": sum(1 for x in items if x["status"] == "rectifying"),
        "verified": sum(1 for x in items if x["status"] == "verified"),
        "overdue": sum(1 for x in items if x["overdue"]),
        "major_open": sum(1 for x in items if x["level"] == "major" and x["status"] != "verified"),
    }
    return {"date": today.isoformat(), "data_mode": mode, "counts": counts, "items": items}


async def report_hazard(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    tz: ZoneInfo,
    cfg: EhsConfig,
    body: HazardIn,
    *,
    causation_id: str | None = None,
) -> dict[str, Any]:
    hid = new_id("HAZ", tz)
    now_iso = utcnow().isoformat()
    role = body.assignee_role or (cfg.notify_roles[0] if cfg.notify_roles else "shift_lead")
    payload: dict[str, Any] = {
        "hazard_no": hid,
        "location": body.location.strip(),
        "category": body.category,
        "level": body.level,
        "description": body.description.strip(),
        "due_date": body.due_date.isoformat(),
        "assignee_role": role,
        "status": "open",
        "reported_at": now_iso,
    }
    ev = body.evidence or [Evidence(kind="manual", ref=f"hazard:{hid}", note="安全隐患上报")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=HAZARD_REPORT_EVENT,
            object_id=hid,
            payload=payload,
            evidence=ev,
            causation_id=causation_id,
            data_mode=body.data_mode,
        ),
    )
    return {"id": hid, "event": res.model_dump()}


async def rectify_hazard(
    session: AsyncSession, principal: Principal, ledger: Ledger, hazard_id: str, body: HazardRectifyIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "safety_hazard", hazard_id)
    if o is None:
        raise EhsNotFound(f"安全隐患 {hazard_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"hazard:{hazard_id}:rectify", note="隐患整改登记")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=HAZARD_RECTIFY_EVENT,
            object_id=hazard_id,
            payload={"rectification_plan": body.rectification_plan.strip(), "status": "rectifying"},
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def verify_hazard(
    session: AsyncSession, principal: Principal, ledger: Ledger, hazard_id: str, body: HazardVerifyIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "safety_hazard", hazard_id)
    if o is None:
        raise EhsNotFound(f"安全隐患 {hazard_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"hazard:{hazard_id}:verify", note="隐患复查销项")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=HAZARD_VERIFY_EVENT,
            object_id=hazard_id,
            payload={
                "verified_note": body.verified_note.strip(),
                "status": "verified",
                "closed_at": utcnow().isoformat(),
            },
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def remind_overdue_hazards(
    session: AsyncSession, tenant: str, cfg: EhsConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    """为即将到期或已超期的未销项安全隐患生成去重待办。"""
    board = await hazards_board(session, tenant, cfg, today, mode=mode)
    existing = {
        (t.object_id, t.due_at.date().isoformat() if t.due_at else "")
        for t in (
            await session.scalars(
                select(Todo).where(
                    Todo.tenant_id == tenant,
                    Todo.object_type == "safety_hazard",
                    Todo.status == "open",
                    Todo.data_mode == mode,
                )
            )
        ).all()
    }
    created: list[str] = []
    default_role = cfg.notify_roles[0] if cfg.notify_roles else "shift_lead"
    for it in board["items"]:
        if it["status"] == "verified" or not (it["overdue"] or it["due_soon"]):
            continue
        due_str = it["due_date"] or ""
        if (it["id"], due_str) in existing:
            continue
        due_dt = datetime.combine(date.fromisoformat(due_str), time(17, 0), tzinfo=UTC) if due_str else None
        prefix = "【隐患超期】" if it["overdue"] else "【隐患临期】"
        session.add(
            Todo(
                tenant_id=tenant,
                title=f"{prefix}{it['hazard_no']} {it['location']}（{it['level_text']}）",
                body=f"{it['description']}；整改期限：{due_str}。",
                assignee_role=it.get("assignee_role") or default_role,
                object_type="safety_hazard",
                object_id=it["id"],
                source="system:ehs",
                created_by="system:ehs",
                due_at=due_dt,
                data_mode=mode,
            )
        )
        created.append(it["id"])
    return {"date": today.isoformat(), "created": len(created), "hazard_ids": created}


# ---------------- 特种作业票 ----------------


class PermitIn(BaseModel):
    kind: Literal["hot_work", "confined_space", "high_altitude", "electrical", "lifting"]
    location: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    worker_name: str = Field(min_length=1, max_length=64)
    safety_measures: str = Field(min_length=1, max_length=1000)
    valid_from: datetime
    valid_until: datetime
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class PermitCloseIn(BaseModel):
    status: Literal["closed", "revoked"] = "closed"
    closed_note: str = Field(min_length=1, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _permit_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "active"
    kd = s.get("kind") or "hot_work"
    return {
        "id": o.object_id,
        "permit_no": s.get("permit_no") or o.object_id,
        "kind": kd,
        "kind_text": PERMIT_KIND_TEXT.get(kd, kd),
        "location": s.get("location") or "",
        "description": s.get("description") or "",
        "worker_name": s.get("worker_name") or "",
        "safety_measures": s.get("safety_measures") or "",
        "valid_from": s.get("valid_from"),
        "valid_until": s.get("valid_until"),
        "status": st,
        "status_text": PERMIT_STATUS_TEXT.get(st, st),
        "closed_note": s.get("closed_note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def permits_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "work_permit", mode)
    items = [_permit_out(o) for o in rows]
    if status:
        items = [it for it in items if it["status"] == status]
    items.sort(key=lambda x: (x["status"] in ("closed", "revoked"), x["id"]))
    counts = {
        "total": len(items),
        "active": sum(1 for x in items if x["status"] == "active"),
        "closed": sum(1 for x in items if x["status"] in ("closed", "revoked")),
        "high_risk": sum(1 for x in items if x["kind"] in ("hot_work", "confined_space")),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


def issue_permit_request(tz: ZoneInfo, body: PermitIn) -> tuple[str, AppendRequest]:
    """构造特种作业票签发请求（由路由层执行 append，以便捕获动火/受限空间审批异常）。"""
    pid = new_id("PTW", tz)
    ev = body.evidence or [Evidence(kind="manual", ref=f"permit:{pid}", note="特种作业票申请")]
    req = AppendRequest(
        type=PERMIT_ISSUE_EVENT,
        object_id=pid,
        payload={
            "permit_no": pid,
            "kind": body.kind,
            "location": body.location.strip(),
            "description": body.description.strip(),
            "worker_name": body.worker_name.strip(),
            "safety_measures": body.safety_measures.strip(),
            "valid_from": body.valid_from.isoformat(),
            "valid_until": body.valid_until.isoformat(),
            "status": "active",
        },
        evidence=ev,
        data_mode=body.data_mode,
    )
    return pid, req


async def close_permit(
    session: AsyncSession, principal: Principal, ledger: Ledger, permit_id: str, body: PermitCloseIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "work_permit", permit_id)
    if o is None:
        raise EhsNotFound(f"特种作业票 {permit_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"permit:{permit_id}:close", note="特种作业票关闭")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=PERMIT_CLOSE_EVENT,
            object_id=permit_id,
            payload={"status": body.status, "closed_note": body.closed_note.strip()},
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 安全事故与险情 ----------------


class IncidentIn(BaseModel):
    kind: Literal["near_miss", "minor_injury", "major_injury", "equipment_fire", "env_leak"]
    location: str = Field(min_length=1, max_length=120)
    occurred_at: datetime | None = None
    description: str = Field(min_length=1, max_length=1500)
    injured_count: int = Field(default=0, ge=0, le=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class IncidentCloseIn(BaseModel):
    root_cause: str = Field(min_length=1, max_length=1000)
    corrective_action: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _incident_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "open"
    kd = s.get("kind") or "near_miss"
    return {
        "id": o.object_id,
        "incident_no": s.get("incident_no") or o.object_id,
        "kind": kd,
        "kind_text": INCIDENT_KIND_TEXT.get(kd, kd),
        "location": s.get("location") or "",
        "occurred_at": s.get("occurred_at"),
        "description": s.get("description") or "",
        "injured_count": int(s.get("injured_count") or 0),
        "root_cause": s.get("root_cause"),
        "corrective_action": s.get("corrective_action"),
        "status": st,
        "status_text": INCIDENT_STATUS_TEXT.get(st, st),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def incidents_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "safety_incident", mode)
    items = [_incident_out(o) for o in rows]
    if status:
        items = [it for it in items if it["status"] == status]
    items.sort(key=lambda x: (x["status"] == "closed", x["id"]))
    return {
        "data_mode": mode,
        "counts": {
            "total": len(items),
            "open": sum(1 for x in items if x["status"] != "closed"),
            "closed": sum(1 for x in items if x["status"] == "closed"),
            "total_injured": sum(x["injured_count"] for x in items),
        },
        "items": items,
    }


async def report_incident(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: IncidentIn
) -> dict[str, Any]:
    iid = new_id("INC", tz)
    when = (body.occurred_at or utcnow()).isoformat()
    ev = body.evidence or [Evidence(kind="manual", ref=f"incident:{iid}", note="安全事故/险情上报")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=INCIDENT_REPORT_EVENT,
            object_id=iid,
            payload={
                "incident_no": iid,
                "kind": body.kind,
                "location": body.location.strip(),
                "occurred_at": when,
                "description": body.description.strip(),
                "injured_count": body.injured_count,
                "status": "open",
            },
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": iid, "event": res.model_dump()}


async def close_incident(
    session: AsyncSession, principal: Principal, ledger: Ledger, incident_id: str, body: IncidentCloseIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "safety_incident", incident_id)
    if o is None:
        raise EhsNotFound(f"安全事故 {incident_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"incident:{incident_id}:close", note="事故闭环")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=INCIDENT_CLOSE_EVENT,
            object_id=incident_id,
            payload={
                "root_cause": body.root_cause.strip(),
                "corrective_action": body.corrective_action.strip(),
                "status": "closed",
            },
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 环保排放监测 ----------------


class EnvReadingIn(BaseModel):
    point_name: str = Field(min_length=1, max_length=120)
    kind: Literal["dust", "nox", "so2", "noise", "voc"]
    value: FiniteFloat = Field(ge=0)
    measured_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _env_out(o: ObjectState, cfg: EhsConfig) -> dict[str, Any]:
    s = o.state
    kd = s.get("kind") or "dust"
    lim = cfg.env_limits.get(kd)
    return {
        "id": o.object_id,
        "point_name": s.get("point_name") or "",
        "kind": kd,
        "kind_text": lim.name if lim else kd,
        "value": s.get("value"),
        "unit": s.get("unit") or (lim.unit if lim else ""),
        "limit_value": s.get("limit_value"),
        "passed": bool(s.get("passed")),
        "measured_at": s.get("measured_at"),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def env_board(
    session: AsyncSession, tenant: str, cfg: EhsConfig, *, kind: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "env_reading", mode)
    items = [_env_out(o, cfg) for o in reversed(rows)]
    if kind:
        items = [it for it in items if it["kind"] == kind]
    passed = sum(1 for x in items if x["passed"])
    exceeded = len(items) - passed
    rate = round(passed / len(items) * 100.0, 2) if items else 100.0
    return {
        "data_mode": mode,
        "counts": {"total": len(items), "passed": passed, "exceeded": exceeded, "compliance_rate_pct": rate},
        "limits": {k: v.model_dump() for k, v in cfg.env_limits.items()},
        "items": items,
    }


async def record_env_reading(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    tz: ZoneInfo,
    cfg: EhsConfig,
    body: EnvReadingIn,
) -> dict[str, Any]:
    try:
        ev_res = evaluate_env_reading(cfg, body.kind, float(body.value))
    except ValueError as e:
        raise EhsError(str(e), 422) from e
    eid = new_id("ENV", tz)
    when = (body.measured_at or utcnow()).isoformat()
    payload: dict[str, Any] = {
        "point_name": body.point_name.strip(),
        "kind": body.kind,
        "value": ev_res["value"],
        "unit": ev_res["unit"],
        "limit_value": ev_res["limit_value"],
        "passed": ev_res["passed"],
        "measured_at": when,
    }
    if body.note:
        payload["note"] = body.note.strip()
    evidence = body.evidence or [Evidence(kind="manual", ref=f"env:{eid}", note="环保监测数据登记")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=ENV_RECORD_EVENT,
            object_id=eid,
            payload=payload,
            evidence=evidence,
            data_mode=body.data_mode,
        ),
    )
    auto_hazard_id: str | None = None
    if not ev_res["passed"] and cfg.auto_hazard_on_env_exceed and principal.can("ehs.hazard:write"):
        today = utcnow().astimezone(tz).date()
        cat: Literal["dust", "gas", "other"] = (
            "dust" if body.kind == "dust" else ("gas" if body.kind in ("nox", "so2", "voc") else "other")
        )
        desc = (
            f"环保监测超标：{body.point_name.strip()} {ev_res['name']} 实测 "
            f"{ev_res['value']} {ev_res['unit']}（限值 {ev_res['limit_value']} {ev_res['unit']}）"
        )
        hz = await report_hazard(
            session,
            principal,
            ledger,
            tz,
            cfg,
            HazardIn(
                location=body.point_name.strip(),
                category=cat,
                level="major",
                description=desc,
                due_date=today + timedelta(days=max(1, cfg.hazard_remind_days)),
                evidence=[Evidence(kind="calculation", ref=f"env:{eid}", note="环保超标自动触发重大隐患")],
                data_mode=body.data_mode,
            ),
            causation_id=res.event_id,
        )
        auto_hazard_id = hz["id"]
        role = cfg.notify_roles[0] if cfg.notify_roles else "shift_lead"
        session.add(
            Todo(
                tenant_id=principal.tenant_id,
                title=f"【环保超标】{body.point_name.strip()} {ev_res['name']} 超限",
                body=f"{desc}；已自动建单重大隐患 {auto_hazard_id}，请立即排查治理。",
                assignee_role=role,
                object_type="safety_hazard",
                object_id=auto_hazard_id,
                source="system:ehs",
                created_by="system:ehs",
                data_mode=body.data_mode,
            )
        )
    return {
        "id": eid,
        "evaluation": ev_res,
        "auto_hazard_id": auto_hazard_id,
        "event": res.model_dump(),
    }


# ---------------- EHS 合规总览 ----------------


async def ehs_summary(
    session: AsyncSession, tenant: str, cfg: EhsConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    hz = await hazards_board(session, tenant, cfg, today, mode=mode)
    pt = await permits_board(session, tenant, mode=mode)
    inc = await incidents_board(session, tenant, mode=mode)
    env = await env_board(session, tenant, cfg, mode=mode)
    kpis = compute_ehs_kpis(hz["items"], pt["items"], inc["items"], env["items"])
    return {
        "date": today.isoformat(),
        "data_mode": mode,
        "kpis": kpis,
        "hazards": hz["counts"],
        "permits": pt["counts"],
        "incidents": inc["counts"],
        "env": env["counts"],
    }
