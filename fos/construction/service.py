"""筑炉施工与烘炉验收核心服务（W27b）。

- 筑炉施工项目：立项、状态更新、竣工验收（正常验收需进度达标，带尾项条件验收走总经理审批）；
- 施工日报：登记每日人数与完成砌筑吨位，自动确定性重算项目累计完成量与进度百分比；
- 烘炉升温记录：确定性校验温差与升温速率，超标自动生成防爆裂预警待办。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.construction.config import ConstructionConfig
from fos.construction.guards import (
    DRYOUT_LOG_EVENT,
    LOG_RECORD_EVENT,
    SITE_ACCEPT_EVENT,
    SITE_CREATE_EVENT,
    SITE_UPDATE_EVENT,
)
from fos.construction.metrics import compute_site_progress, evaluate_dryout
from fos.db.models import ObjectState, Todo, utcnow
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.data import get
from fos.orders.service import new_id

SITE_STATUS_TEXT = {
    "mobilizing": "进场准备",
    "building": "砌筑施工",
    "drying": "烘炉中",
    "accepted": "已竣工验收",
    "suspended": "暂停",
}
ACCEPT_TYPE_TEXT = {"normal": "正常验收", "conditional": "带尾项条件验收"}


class ConstructionError(ValueError):
    """施工模块业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class ConstructionNotFound(ConstructionError):
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


# ---------------- 筑炉施工项目 ----------------


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    customer_name: str = Field(min_length=1, max_length=120)
    furnace_type: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=120)
    supervisor: str = Field(min_length=1, max_length=64)
    planned_tons: FiniteFloat = Field(gt=0.0)
    start_date: date
    due_date: date
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class SiteUpdateIn(BaseModel):
    status: Literal["mobilizing", "building", "drying", "suspended"] | None = None
    due_date: date | None = None
    handover_note: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class SiteAcceptIn(BaseModel):
    acceptance_type: Literal["normal", "conditional"] = "normal"
    handover_note: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _site_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "mobilizing"
    at = s.get("acceptance_type")
    return {
        "id": o.object_id,
        "site_no": s.get("site_no") or o.object_id,
        "name": s.get("name") or o.object_id,
        "customer_name": s.get("customer_name") or "",
        "furnace_type": s.get("furnace_type") or "",
        "location": s.get("location") or "",
        "supervisor": s.get("supervisor") or "",
        "planned_tons": float(s.get("planned_tons") or 0.0),
        "completed_tons": float(s.get("completed_tons") or 0.0),
        "progress_pct": float(s.get("progress_pct") or 0.0),
        "start_date": s.get("start_date"),
        "due_date": s.get("due_date"),
        "status": st,
        "status_text": SITE_STATUS_TEXT.get(st, st),
        "acceptance_type": at,
        "acceptance_type_text": ACCEPT_TYPE_TEXT.get(at, "") if at else "",
        "handover_note": s.get("handover_note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def sites_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "site_project", mode)
    items = [_site_out(o) for o in reversed(rows)]
    if status:
        items = [it for it in items if it["status"] == status]
    counts = {
        "total": len(items),
        "building": sum(1 for x in items if x["status"] == "building"),
        "drying": sum(1 for x in items if x["status"] == "drying"),
        "accepted": sum(1 for x in items if x["status"] == "accepted"),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


async def create_site(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: SiteIn
) -> dict[str, Any]:
    sid = new_id("SITE", tz)
    ev = body.evidence or [Evidence(kind="manual", ref=f"site:{sid}", note="筑炉施工项目立项")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=SITE_CREATE_EVENT,
            object_id=sid,
            payload={
                "site_no": sid,
                "name": body.name.strip(),
                "customer_name": body.customer_name.strip(),
                "furnace_type": body.furnace_type.strip(),
                "location": body.location.strip(),
                "supervisor": body.supervisor.strip(),
                "planned_tons": float(body.planned_tons),
                "completed_tons": 0.0,
                "progress_pct": 0.0,
                "start_date": body.start_date.isoformat(),
                "due_date": body.due_date.isoformat(),
                "status": "mobilizing",
            },
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": sid, "event": res.model_dump()}


async def update_site(
    session: AsyncSession, principal: Principal, ledger: Ledger, site_id: str, body: SiteUpdateIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "site_project", site_id)
    if o is None:
        raise ConstructionNotFound(f"施工项目 {site_id} 不存在")
    payload = body.model_dump(exclude_none=True, exclude={"evidence"})
    if "due_date" in payload:
        payload["due_date"] = payload["due_date"].isoformat()
    if not payload:
        raise ConstructionError("至少需要提供一个更新字段", 422)
    ev = body.evidence or [Evidence(kind="manual", ref=f"site:{site_id}:upd", note="施工项目更新")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=SITE_UPDATE_EVENT,
            object_id=site_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def accept_site_request(
    session: AsyncSession, principal: Principal, site_id: str, body: SiteAcceptIn
) -> AppendRequest:
    """构造竣工验收请求（带尾项条件验收在路由层捕获 ApprovalRequired 转总经理审批）。"""
    o = await get(session, principal.tenant_id, "site_project", site_id)
    if o is None:
        raise ConstructionNotFound(f"施工项目 {site_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"site:{site_id}:accept", note="竣工验收")]
    return AppendRequest(
        type=SITE_ACCEPT_EVENT,
        object_id=site_id,
        payload={
            "status": "accepted",
            "acceptance_type": body.acceptance_type,
            "handover_note": body.handover_note.strip(),
        },
        evidence=ev,
        expected_version=o.version,
        data_mode=o.data_mode,
    )


# ---------------- 施工日报 ----------------


class SiteLogIn(BaseModel):
    site: str = Field(min_length=1, max_length=64)
    log_date: date
    crew_size: int = Field(ge=1, le=500)
    tons_installed: FiniteFloat = Field(ge=0.0)
    weather: str | None = Field(default=None, max_length=32)
    safety_ok: bool = True
    summary: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _log_out(o: ObjectState, site_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    sid = s.get("site") or ""
    return {
        "id": o.object_id,
        "log_no": s.get("log_no") or o.object_id,
        "site": sid,
        "site_name": site_names.get(sid, sid),
        "log_date": s.get("log_date"),
        "crew_size": int(s.get("crew_size") or 0),
        "tons_installed": float(s.get("tons_installed") or 0.0),
        "weather": s.get("weather"),
        "safety_ok": bool(s.get("safety_ok", True)),
        "summary": s.get("summary") or "",
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def logs_board(
    session: AsyncSession, tenant: str, *, site: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    sites = await _scan(session, tenant, "site_project", mode)
    snames = {s.object_id: str(s.state.get("name") or s.object_id) for s in sites}
    rows = await _scan(session, tenant, "site_log", mode)
    items = [_log_out(o, snames) for o in reversed(rows)]
    if site:
        items = [it for it in items if it["site"] == site]
    return {
        "data_mode": mode,
        "counts": {
            "total": len(items),
            "total_tons": round(sum(x["tons_installed"] for x in items), 3),
        },
        "items": items,
    }


async def record_log(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: SiteLogIn
) -> dict[str, Any]:
    sid = body.site.strip()
    s_obj = await get(session, principal.tenant_id, "site_project", sid)
    if s_obj is None:
        raise ConstructionNotFound(f"施工项目 {sid} 不存在")
    lid = new_id("SLG", tz)
    payload: dict[str, Any] = {
        "log_no": lid,
        "site": sid,
        "log_date": body.log_date.isoformat(),
        "crew_size": int(body.crew_size),
        "tons_installed": float(body.tons_installed),
        "safety_ok": body.safety_ok,
        "summary": body.summary.strip(),
    }
    if body.weather:
        payload["weather"] = body.weather.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"site_log:{lid}", note="现场施工日报")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=LOG_RECORD_EVENT,
            object_id=lid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    # 确定性汇总该项目全部施工日报并同步更新项目累计完成量与进度
    all_logs = (await logs_board(session, principal.tenant_id, site=sid, mode=body.data_mode))["items"]
    prog = compute_site_progress(float(s_obj.state.get("planned_tons") or 1.0), all_logs)
    upd_payload: dict[str, Any] = {
        "completed_tons": prog["completed_tons"],
        "progress_pct": prog["progress_pct"],
    }
    if s_obj.state.get("status") == "mobilizing":
        upd_payload["status"] = "building"
    if principal.can("construction.site:write") and s_obj.state.get("status") != "accepted":
        await ledger.append(
            session,
            principal,
            AppendRequest(
                type=SITE_UPDATE_EVENT,
                object_id=sid,
                payload=upd_payload,
                evidence=[Evidence(kind="calculation", ref=f"site_log:{lid}", note="日报自动重算施工进度")],
                causation_id=res.event_id,
                expected_version=s_obj.version,
                data_mode=s_obj.data_mode,
            ),
        )
    return {"id": lid, "progress": prog, "event": res.model_dump()}


# ---------------- 烘炉升温记录 ----------------


class DryoutIn(BaseModel):
    site: str = Field(min_length=1, max_length=64)
    recorded_at: datetime | None = None
    stage_name: str = Field(min_length=1, max_length=64)
    target_temp_c: FiniteFloat = Field(ge=0.0, le=2000.0)
    actual_temp_c: FiniteFloat = Field(ge=0.0, le=2000.0)
    ramp_rate_c_per_h: FiniteFloat = Field(ge=0.0, le=500.0)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _dryout_out(o: ObjectState, site_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    sid = s.get("site") or ""
    return {
        "id": o.object_id,
        "dryout_no": s.get("dryout_no") or o.object_id,
        "site": sid,
        "site_name": site_names.get(sid, sid),
        "recorded_at": s.get("recorded_at"),
        "stage_name": s.get("stage_name") or "",
        "target_temp_c": float(s.get("target_temp_c") or 0.0),
        "actual_temp_c": float(s.get("actual_temp_c") or 0.0),
        "ramp_rate_c_per_h": float(s.get("ramp_rate_c_per_h") or 0.0),
        "within_spec": bool(s.get("within_spec")),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def dryout_board(
    session: AsyncSession, tenant: str, *, site: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    sites = await _scan(session, tenant, "site_project", mode)
    snames = {s.object_id: str(s.state.get("name") or s.object_id) for s in sites}
    rows = await _scan(session, tenant, "dryout_record", mode)
    items = [_dryout_out(o, snames) for o in reversed(rows)]
    if site:
        items = [it for it in items if it["site"] == site]
    ok = sum(1 for x in items if x["within_spec"])
    return {
        "data_mode": mode,
        "counts": {"total": len(items), "within_spec": ok, "alerts": len(items) - ok},
        "items": items,
    }


async def record_dryout(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    tz: ZoneInfo,
    cfg: ConstructionConfig,
    body: DryoutIn,
) -> dict[str, Any]:
    sid = body.site.strip()
    s_obj = await get(session, principal.tenant_id, "site_project", sid)
    if s_obj is None:
        raise ConstructionNotFound(f"施工项目 {sid} 不存在")
    try:
        ev_res = evaluate_dryout(
            cfg,
            target_temp_c=float(body.target_temp_c),
            actual_temp_c=float(body.actual_temp_c),
            ramp_rate_c_per_h=float(body.ramp_rate_c_per_h),
        )
    except ValueError as e:
        raise ConstructionError(str(e), 422) from e
    did = new_id("DRY", tz)
    when = (body.recorded_at or utcnow()).isoformat()
    payload: dict[str, Any] = {
        "dryout_no": did,
        "site": sid,
        "recorded_at": when,
        "stage_name": body.stage_name.strip(),
        "target_temp_c": float(body.target_temp_c),
        "actual_temp_c": float(body.actual_temp_c),
        "ramp_rate_c_per_h": float(body.ramp_rate_c_per_h),
        "within_spec": ev_res["within_spec"],
    }
    if body.note:
        payload["note"] = body.note.strip()
    evidence = body.evidence or [Evidence(kind="calculation", ref=f"dryout:{did}", note="烘炉曲线确定性校验")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=DRYOUT_LOG_EVENT,
            object_id=did,
            payload=payload,
            evidence=evidence,
            data_mode=body.data_mode,
        ),
    )
    if not ev_res["within_spec"]:
        role = cfg.notify_roles[0] if cfg.notify_roles else "engineer"
        sname = str(s_obj.state.get("name") or sid)
        session.add(
            Todo(
                tenant_id=principal.tenant_id,
                title=f"【烘炉曲线告警】{sname} {body.stage_name.strip()}",
                body="；".join(ev_res["reasons"]) + "，请立即调整燃烧器控制升温节奏防爆裂。",
                assignee_role=role,
                object_type="site_project",
                object_id=sid,
                source="system:construction",
                created_by="system:construction",
                data_mode=body.data_mode,
            )
        )
    return {"id": did, "evaluation": ev_res, "event": res.model_dump()}
