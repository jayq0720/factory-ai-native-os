"""研发配方试验与新产品试产 API（W27）：项目立项、配方版本核算、试验判定、定型审批与三表导出。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fos.api.channel_routes import lookup_for
from fos.api.deps import SessionDep, require
from fos.api.inbox_routes import approval_out
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices
from fos.rnd import export as rx
from fos.rnd import service as rs
from fos.rnd.config import RndConfig

router = APIRouter(prefix="/v1/rnd", tags=["rnd"])

ProjRead = Annotated[Principal, Depends(require("rnd.project:read"))]
ProjWrite = Annotated[Principal, Depends(require("rnd.project:write"))]
FrmRead = Annotated[Principal, Depends(require("rnd.formula:read"))]
FrmWrite = Annotated[Principal, Depends(require("rnd.formula:write"))]
TrlRead = Annotated[Principal, Depends(require("rnd.trial:read"))]
TrlWrite = Annotated[Principal, Depends(require("rnd.trial:write"))]


def _cfg(request: Request, tid: str) -> RndConfig:
    cfg = request.app.state.services.rnd.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用研发配方管理模块")
    return cfg


def _tz(request: Request, tid: str) -> ZoneInfo:
    t = request.app.state.registry.get(tid)
    return ZoneInfo(t.timezone if t else "Asia/Shanghai")


def _check_mode(request: Request, mode: str) -> None:
    if mode == "mock" and request.app.state.settings.env == "prod":
        raise HTTPException(403, detail="生产环境不允许写入模拟数据")


def _http_err(e: Exception) -> HTTPException:
    if isinstance(e, rs.RndError):
        return HTTPException(e.status, detail=str(e))
    if isinstance(e, Forbidden):
        return HTTPException(403, detail=str(e))
    if isinstance(e, Conflict):
        return HTTPException(409, detail=str(e))
    if isinstance(e, Invalid | LedgerError):
        return HTTPException(422, detail=str(e))
    raise e


async def _submit_approval(
    request: Request, session: SessionDep, p: Principal, req: Any, err: ApprovalRequired
):
    lookup = lookup_for(request)
    sub = await request.app.state.approvals.submit(
        session,
        p,
        req,
        err.decision,
        source="api:rnd",
        lookup=lookup,
    )
    await session.commit()
    await send_notices(lookup, p.tenant_id, sub.notices)
    return JSONResponse(
        {
            "status": "pending_approval",
            "approval": approval_out(sub.approval),
            "message": sub.message(),
        },
        status_code=202,
    )


# ---------------- 研发项目 ----------------


@router.get("/projects")
async def list_projects(
    request: Request,
    session: SessionDep,
    p: ProjRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await rs.projects_board(session, p.tenant_id, status=status, mode=mode)


@router.post("/projects", status_code=201)
async def create_project_ep(
    body: rs.ProjectIn, request: Request, session: SessionDep, p: ProjWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await rs.create_project(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (rs.RndError, LedgerError) as e:
        raise _http_err(e) from e


@router.patch("/projects/{project_id}")
async def patch_project_ep(
    project_id: str, body: rs.ProjectUpdateIn, request: Request, session: SessionDep, p: ProjWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await rs.update_project(session, p, request.app.state.ledger, project_id, body)
        return res.model_dump()
    except (rs.RndError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 配方版本 ----------------


@router.get("/formulas")
async def list_formulas(
    request: Request,
    session: SessionDep,
    p: FrmRead,
    project: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await rs.formulas_board(session, p.tenant_id, project=project, mode=mode)


@router.post("/formulas", status_code=201)
async def create_formula_ep(
    body: rs.FormulaIn, request: Request, session: SessionDep, p: FrmWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await rs.create_formula(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), cfg, body
        )
    except (rs.RndError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/formulas/{formula_id}/release")
async def release_formula_ep(
    formula_id: str, body: rs.FormulaActionIn, request: Request, session: SessionDep, p: FrmWrite
):
    _cfg(request, p.tenant_id)
    try:
        req = await rs.release_formula_request(session, p, formula_id, body)
        res = await request.app.state.ledger.append(session, p, req)
        return res.model_dump()
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (rs.RndError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/formulas/{formula_id}/reject")
async def reject_formula_ep(
    formula_id: str, body: rs.FormulaActionIn, request: Request, session: SessionDep, p: FrmWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await rs.reject_formula(session, p, request.app.state.ledger, formula_id, body)
        return res.model_dump()
    except (rs.RndError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 试验检测记录 ----------------


@router.get("/trials")
async def list_trials(
    request: Request,
    session: SessionDep,
    p: TrlRead,
    formula: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await rs.trials_board(session, p.tenant_id, formula=formula, mode=mode)


@router.post("/trials", status_code=201)
async def create_trial_ep(
    body: rs.TrialIn, request: Request, session: SessionDep, p: TrlWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await rs.log_trial(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (rs.RndError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 三表导出 ----------------


@router.get("/report.xlsx")
async def export_rnd_report(request: Request, session: SessionDep, p: ProjRead) -> Response:
    """导出研发项目、配方版本与试验检测三表 Excel（规则 4：正式报表仅使用真实数据）。"""
    _cfg(request, p.tenant_id)
    today = utcnow().astimezone(_tz(request, p.tenant_id)).date().isoformat()
    tenant = request.app.state.registry.get(p.tenant_id)
    factory = tenant.name if tenant else p.tenant_id
    projs = (await rs.projects_board(session, p.tenant_id, mode="real"))["items"]
    forms = (await rs.formulas_board(session, p.tenant_id, mode="real"))["items"]
    trials = (await rs.trials_board(session, p.tenant_id, mode="real"))["items"]
    xlsx = rx.rnd_report_xlsx(factory, projs, forms, trials)
    fname = f"研发配方与试验_{today}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
