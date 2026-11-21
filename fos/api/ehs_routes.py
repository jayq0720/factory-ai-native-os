"""EHS 安全环保与特种作业 API（W25）：安全隐患、特种作业票、事故险情、环保监测与合规导出。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fos.api.channel_routes import lookup_for
from fos.api.deps import SessionDep, require
from fos.api.inbox_routes import approval_out
from fos.db.models import utcnow
from fos.ehs import export as hx
from fos.ehs import service as hs
from fos.ehs.config import EhsConfig
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices

router = APIRouter(prefix="/v1/ehs", tags=["ehs"])

HzRead = Annotated[Principal, Depends(require("ehs.hazard:read"))]
HzWrite = Annotated[Principal, Depends(require("ehs.hazard:write"))]
PtRead = Annotated[Principal, Depends(require("ehs.permit:read"))]
PtWrite = Annotated[Principal, Depends(require("ehs.permit:write"))]
IncRead = Annotated[Principal, Depends(require("ehs.incident:read"))]
IncWrite = Annotated[Principal, Depends(require("ehs.incident:write"))]
EnvRead = Annotated[Principal, Depends(require("ehs.env:read"))]
EnvWrite = Annotated[Principal, Depends(require("ehs.env:write"))]


def _cfg(request: Request, tid: str) -> EhsConfig:
    cfg = request.app.state.services.ehs.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用 EHS 安全环保模块")
    return cfg


def _tz(request: Request, tid: str) -> ZoneInfo:
    t = request.app.state.registry.get(tid)
    return ZoneInfo(t.timezone if t else "Asia/Shanghai")


def _today(request: Request, tid: str) -> date:
    return utcnow().astimezone(_tz(request, tid)).date()


def _check_mode(request: Request, mode: str) -> None:
    if mode == "mock" and request.app.state.settings.env == "prod":
        raise HTTPException(403, detail="生产环境不允许写入模拟数据")


def _http_err(e: Exception) -> HTTPException:
    if isinstance(e, hs.EhsError):
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
        source="api:ehs",
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


# ---------------- 安全隐患排查 ----------------


@router.get("/hazards")
async def list_hazards(
    request: Request,
    session: SessionDep,
    p: HzRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await hs.hazards_board(
        session, p.tenant_id, cfg, _today(request, p.tenant_id), status=status, mode=mode
    )


@router.post("/hazards", status_code=201)
async def create_hazard(
    body: hs.HazardIn, request: Request, session: SessionDep, p: HzWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await hs.report_hazard(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), cfg, body
        )
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/hazards/{hazard_id}/rectify")
async def rectify_hazard_ep(
    hazard_id: str, body: hs.HazardRectifyIn, request: Request, session: SessionDep, p: HzWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hs.rectify_hazard(session, p, request.app.state.ledger, hazard_id, body)
        return res.model_dump()
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/hazards/{hazard_id}/verify")
async def verify_hazard_ep(
    hazard_id: str, body: hs.HazardVerifyIn, request: Request, session: SessionDep, p: HzWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hs.verify_hazard(session, p, request.app.state.ledger, hazard_id, body)
        return res.model_dump()
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/hazards/remind")
async def trigger_hazard_remind(
    request: Request, session: SessionDep, p: HzWrite, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await hs.remind_overdue_hazards(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)


# ---------------- 特种作业票 ----------------


@router.get("/permits")
async def list_permits(
    request: Request,
    session: SessionDep,
    p: PtRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await hs.permits_board(session, p.tenant_id, status=status, mode=mode)


@router.post("/permits", status_code=201)
async def create_permit(body: hs.PermitIn, request: Request, session: SessionDep, p: PtWrite):
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    pid, req = hs.issue_permit_request(_tz(request, p.tenant_id), body)
    try:
        res = await request.app.state.ledger.append(session, p, req)
        return {"id": pid, "event": res.model_dump()}
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/permits/{permit_id}/close")
async def close_permit_ep(
    permit_id: str, body: hs.PermitCloseIn, request: Request, session: SessionDep, p: PtWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hs.close_permit(session, p, request.app.state.ledger, permit_id, body)
        return res.model_dump()
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 安全事故与险情 ----------------


@router.get("/incidents")
async def list_incidents(
    request: Request,
    session: SessionDep,
    p: IncRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await hs.incidents_board(session, p.tenant_id, status=status, mode=mode)


@router.post("/incidents", status_code=201)
async def create_incident(
    body: hs.IncidentIn, request: Request, session: SessionDep, p: IncWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await hs.report_incident(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/incidents/{incident_id}/close")
async def close_incident_ep(
    incident_id: str, body: hs.IncidentCloseIn, request: Request, session: SessionDep, p: IncWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hs.close_incident(session, p, request.app.state.ledger, incident_id, body)
        return res.model_dump()
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 环保排放监测 ----------------


@router.get("/env")
async def list_env(
    request: Request,
    session: SessionDep,
    p: EnvRead,
    kind: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await hs.env_board(session, p.tenant_id, cfg, kind=kind, mode=mode)


@router.post("/env", status_code=201)
async def create_env(
    body: hs.EnvReadingIn, request: Request, session: SessionDep, p: EnvWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await hs.record_env_reading(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), cfg, body
        )
    except (hs.EhsError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- EHS 合规总览与四表导出 ----------------


@router.get("/summary")
async def get_summary(
    request: Request, session: SessionDep, p: HzRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await hs.ehs_summary(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)


@router.get("/report.xlsx")
async def export_ehs_report(request: Request, session: SessionDep, p: HzRead) -> Response:
    """导出 EHS 四表合规报告 Excel（规则 4：正式报表仅使用真实数据）。"""
    cfg = _cfg(request, p.tenant_id)
    today = _today(request, p.tenant_id)
    tenant = request.app.state.registry.get(p.tenant_id)
    factory = tenant.name if tenant else p.tenant_id
    summary = await hs.ehs_summary(session, p.tenant_id, cfg, today, mode="real")
    hz = await hs.hazards_board(session, p.tenant_id, cfg, today, mode="real")
    pt = await hs.permits_board(session, p.tenant_id, mode="real")
    env = await hs.env_board(session, p.tenant_id, cfg, mode="real")
    xlsx = hx.ehs_report_xlsx(factory, summary, hz["items"], pt["items"], env["items"])
    fname = f"EHS合规报告_{today.isoformat()}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
