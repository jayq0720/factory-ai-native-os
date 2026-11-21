"""研发配方试验与新产品试产核心服务（W27）。

- 研发项目：立项、目标理化指标下限配置、进度更新；
- 配方版本：确定性校验组分总和=100%并核算理论 Al2O3 含量与吨原料成本；
- 试验检测：对照项目目标下限确定性判定达标与否；
- 配方定型放行：守卫强制至少 1 次合格试验，策略触发总经理审批。
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import date
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.data import get
from fos.orders.service import new_id
from fos.rnd.config import RndConfig
from fos.rnd.formulation import evaluate_formula, evaluate_trial
from fos.rnd.guards import (
    FORMULA_CREATE_EVENT,
    FORMULA_REJECT_EVENT,
    FORMULA_RELEASE_EVENT,
    PROJ_CREATE_EVENT,
    PROJ_UPDATE_EVENT,
    TRIAL_LOG_EVENT,
)

PROJ_STATUS_TEXT = {"active": "研发中", "trial": "试产中", "released": "已量产", "archived": "已归档"}
FORMULA_STATUS_TEXT = {"draft": "草稿", "testing": "试验中", "approved": "已定型", "rejected": "已作废"}
STAGE_TEXT = {"lab": "实验室小试", "pilot": "中试", "kiln_trial": "窑炉试烧"}


class RndError(ValueError):
    """研发模块业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class RndNotFound(RndError):
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


# ---------------- 研发项目 ----------------


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_product: str = Field(min_length=1, max_length=120)
    target_al2o3_min: FiniteFloat | None = Field(default=None, ge=0.0, le=100.0)
    target_bd_min: FiniteFloat | None = Field(default=None, ge=0.0, le=10.0)
    target_ccs_min: FiniteFloat | None = Field(default=None, ge=0.0, le=1000.0)
    owner_name: str = Field(min_length=1, max_length=64)
    due_date: date
    note: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class ProjectUpdateIn(BaseModel):
    target_al2o3_min: FiniteFloat | None = Field(default=None, ge=0.0, le=100.0)
    target_bd_min: FiniteFloat | None = Field(default=None, ge=0.0, le=10.0)
    target_ccs_min: FiniteFloat | None = Field(default=None, ge=0.0, le=1000.0)
    due_date: date | None = None
    status: Literal["active", "trial", "released", "archived"] | None = None
    note: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _proj_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "active"
    return {
        "id": o.object_id,
        "project_no": s.get("project_no") or o.object_id,
        "name": s.get("name") or o.object_id,
        "target_product": s.get("target_product") or "",
        "target_al2o3_min": s.get("target_al2o3_min"),
        "target_bd_min": s.get("target_bd_min"),
        "target_ccs_min": s.get("target_ccs_min"),
        "owner_name": s.get("owner_name") or "",
        "due_date": s.get("due_date"),
        "status": st,
        "status_text": PROJ_STATUS_TEXT.get(st, st),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def projects_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "rnd_project", mode)
    items = [_proj_out(o) for o in reversed(rows)]
    if status:
        items = [it for it in items if it["status"] == status]
    counts = {
        "total": len(items),
        "active": sum(1 for x in items if x["status"] == "active"),
        "trial": sum(1 for x in items if x["status"] == "trial"),
        "released": sum(1 for x in items if x["status"] == "released"),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


async def create_project(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: ProjectIn
) -> dict[str, Any]:
    pid = new_id("RND", tz)
    payload: dict[str, Any] = {
        "project_no": pid,
        "name": body.name.strip(),
        "target_product": body.target_product.strip(),
        "owner_name": body.owner_name.strip(),
        "due_date": body.due_date.isoformat(),
        "status": "active",
    }
    for k in ("target_al2o3_min", "target_bd_min", "target_ccs_min"):
        v = getattr(body, k)
        if v is not None:
            payload[k] = float(v)
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"rnd:{pid}", note="研发项目立项")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=PROJ_CREATE_EVENT,
            object_id=pid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": pid, "event": res.model_dump()}


async def update_project(
    session: AsyncSession, principal: Principal, ledger: Ledger, project_id: str, body: ProjectUpdateIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "rnd_project", project_id)
    if o is None:
        raise RndNotFound(f"研发项目 {project_id} 不存在")
    payload = body.model_dump(exclude_none=True, exclude={"evidence"})
    if "due_date" in payload:
        payload["due_date"] = payload["due_date"].isoformat()
    if not payload:
        raise RndError("至少需要提供一个更新字段", 422)
    ev = body.evidence or [Evidence(kind="manual", ref=f"rnd:{project_id}:upd", note="研发项目更新")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=PROJ_UPDATE_EVENT,
            object_id=project_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 配方版本 ----------------


class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    pct: FiniteFloat = Field(gt=0.0, le=100.0)
    al2o3_pct: FiniteFloat = Field(default=0.0, ge=0.0, le=100.0)
    unit_cost: FiniteFloat = Field(default=0.0, ge=0.0)


class FormulaIn(BaseModel):
    project: str = Field(min_length=1, max_length=64)
    version_code: str = Field(min_length=1, max_length=32)
    ingredients: list[IngredientIn] = Field(min_length=1, max_length=40)
    firing_temp_c: FiniteFloat = Field(gt=0.0, le=3000.0)
    note: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class FormulaActionIn(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _formula_out(o: ObjectState, proj_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    pid = s.get("project") or ""
    st = s.get("status") or "draft"
    ing: list[dict[str, Any]] = []
    with suppress(ValueError):
        parsed = json.loads(str(s.get("ingredients_json") or "[]"))
        if isinstance(parsed, list):
            ing = parsed
    return {
        "id": o.object_id,
        "formula_no": s.get("formula_no") or o.object_id,
        "project": pid,
        "project_name": proj_names.get(pid, pid),
        "version_code": s.get("version_code") or "",
        "ingredients": ing,
        "total_pct": float(s.get("total_pct") or 0.0),
        "calc_al2o3_pct": s.get("calc_al2o3_pct"),
        "est_cost_per_ton": s.get("est_cost_per_ton"),
        "firing_temp_c": s.get("firing_temp_c"),
        "status": st,
        "status_text": FORMULA_STATUS_TEXT.get(st, st),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def formulas_board(
    session: AsyncSession, tenant: str, *, project: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    projs = await _scan(session, tenant, "rnd_project", mode)
    proj_names = {p.object_id: str(p.state.get("name") or p.object_id) for p in projs}
    rows = await _scan(session, tenant, "formula_version", mode)
    items = [_formula_out(o, proj_names) for o in reversed(rows)]
    if project:
        items = [it for it in items if it["project"] == project]
    counts = {
        "total": len(items),
        "draft": sum(1 for x in items if x["status"] == "draft"),
        "testing": sum(1 for x in items if x["status"] == "testing"),
        "approved": sum(1 for x in items if x["status"] == "approved"),
        "rejected": sum(1 for x in items if x["status"] == "rejected"),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


async def create_formula(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    tz: ZoneInfo,
    cfg: RndConfig,
    body: FormulaIn,
) -> dict[str, Any]:
    try:
        calc = evaluate_formula([i.model_dump() for i in body.ingredients], pct_tolerance=cfg.pct_tolerance)
    except ValueError as e:
        raise RndError(str(e), 422) from e
    fid = new_id("FRM", tz)
    payload: dict[str, Any] = {
        "formula_no": fid,
        "project": body.project.strip(),
        "version_code": body.version_code.strip(),
        "ingredients_json": json.dumps(calc["ingredients"], ensure_ascii=False),
        "total_pct": calc["total_pct"],
        "calc_al2o3_pct": calc["calc_al2o3_pct"],
        "est_cost_per_ton": calc["est_cost_per_ton"],
        "firing_temp_c": float(body.firing_temp_c),
        "status": "testing",
    }
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="calculation", ref=f"formula:{fid}", note="配方确定性核算建档")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=FORMULA_CREATE_EVENT,
            object_id=fid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": fid, "calculation": calc, "event": res.model_dump()}


async def release_formula_request(
    session: AsyncSession, principal: Principal, formula_id: str, body: FormulaActionIn
) -> AppendRequest:
    """构造配方定型放行请求（路由层捕获 ApprovalRequired 转总经理审批）。"""
    o = await get(session, principal.tenant_id, "formula_version", formula_id)
    if o is None:
        raise RndNotFound(f"配方版本 {formula_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"formula:{formula_id}:release", note="配方定型申请")]
    return AppendRequest(
        type=FORMULA_RELEASE_EVENT,
        object_id=formula_id,
        payload={"status": "approved", "note": body.note.strip()},
        evidence=ev,
        expected_version=o.version,
        data_mode=o.data_mode,
    )


async def reject_formula(
    session: AsyncSession, principal: Principal, ledger: Ledger, formula_id: str, body: FormulaActionIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "formula_version", formula_id)
    if o is None:
        raise RndNotFound(f"配方版本 {formula_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"formula:{formula_id}:reject", note="配方作废")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=FORMULA_REJECT_EVENT,
            object_id=formula_id,
            payload={"status": "rejected", "note": body.note.strip()},
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 试验检测记录 ----------------


class TrialIn(BaseModel):
    formula: str = Field(min_length=1, max_length=64)
    stage: Literal["lab", "pilot", "kiln_trial"] = "lab"
    trial_date: date
    al2o3_pct: FiniteFloat | None = Field(default=None, ge=0.0, le=100.0)
    bulk_density: FiniteFloat | None = Field(default=None, ge=0.0, le=10.0)
    ccs_mpa: FiniteFloat | None = Field(default=None, ge=0.0, le=1000.0)
    thermal_shock_cycles: int | None = Field(default=None, ge=0, le=1000)
    conclusion: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _trial_out(o: ObjectState, formula_codes: dict[str, str]) -> dict[str, Any]:
    s = o.state
    fid = s.get("formula") or ""
    st = s.get("stage") or "lab"
    return {
        "id": o.object_id,
        "trial_no": s.get("trial_no") or o.object_id,
        "formula": fid,
        "formula_code": formula_codes.get(fid, fid),
        "stage": st,
        "stage_text": STAGE_TEXT.get(st, st),
        "trial_date": s.get("trial_date"),
        "al2o3_pct": s.get("al2o3_pct"),
        "bulk_density": s.get("bulk_density"),
        "ccs_mpa": s.get("ccs_mpa"),
        "thermal_shock_cycles": s.get("thermal_shock_cycles"),
        "passed": bool(s.get("passed")),
        "conclusion": s.get("conclusion") or "",
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def trials_board(
    session: AsyncSession, tenant: str, *, formula: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    forms = await _scan(session, tenant, "formula_version", mode)
    fcodes = {f.object_id: str(f.state.get("version_code") or f.object_id) for f in forms}
    rows = await _scan(session, tenant, "lab_trial", mode)
    items = [_trial_out(o, fcodes) for o in reversed(rows)]
    if formula:
        items = [it for it in items if it["formula"] == formula]
    passed = sum(1 for x in items if x["passed"])
    rate = round(passed / len(items) * 100.0, 2) if items else 100.0
    return {
        "data_mode": mode,
        "counts": {"total": len(items), "passed": passed, "failed": len(items) - passed, "pass_rate": rate},
        "items": items,
    }


async def log_trial(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: TrialIn
) -> dict[str, Any]:
    f_obj = await get(session, principal.tenant_id, "formula_version", body.formula.strip())
    if f_obj is None:
        raise RndNotFound(f"配方版本 {body.formula} 不存在")
    p_obj = await get(session, principal.tenant_id, "rnd_project", str(f_obj.state.get("project") or ""))
    proj_state = p_obj.state if p_obj is not None else {}
    try:
        passed, reasons = evaluate_trial(
            proj_state,
            al2o3_pct=float(body.al2o3_pct) if body.al2o3_pct is not None else None,
            bulk_density=float(body.bulk_density) if body.bulk_density is not None else None,
            ccs_mpa=float(body.ccs_mpa) if body.ccs_mpa is not None else None,
        )
    except ValueError as e:
        raise RndError(str(e), 422) from e
    tid = new_id("TRL", tz)
    payload: dict[str, Any] = {
        "trial_no": tid,
        "formula": body.formula.strip(),
        "stage": body.stage,
        "trial_date": body.trial_date.isoformat(),
        "passed": passed,
        "conclusion": body.conclusion.strip(),
    }
    for k in ("al2o3_pct", "bulk_density", "ccs_mpa"):
        v = getattr(body, k)
        if v is not None:
            payload[k] = float(v)
    if body.thermal_shock_cycles is not None:
        payload["thermal_shock_cycles"] = int(body.thermal_shock_cycles)
    ev = body.evidence or [Evidence(kind="calculation", ref=f"trial:{tid}", note="试验理化指标确定性判定")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=TRIAL_LOG_EVENT,
            object_id=tid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": tid, "passed": passed, "reasons": reasons, "event": res.model_dump()}
