"""质量体系 API（W22）：待检任务、理化检验判定、NCR / 8D、质保书、计量器具校准、ISO 审核包。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fos.api.channel_routes import lookup_for
from fos.api.deps import PrincipalDep, SessionDep, require
from fos.api.inbox_routes import approval_out
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices
from fos.quality import export as qe
from fos.quality import service as qs
from fos.quality.config import QualityConfig

router = APIRouter(prefix="/v1/quality", tags=["quality"])

InsRead = Annotated[Principal, Depends(require("quality.inspection:read"))]
InsWrite = Annotated[Principal, Depends(require("quality.inspection:write"))]
NcrRead = Annotated[Principal, Depends(require("quality.ncr:read"))]
NcrWrite = Annotated[Principal, Depends(require("quality.ncr:write"))]
InstRead = Annotated[Principal, Depends(require("quality.measuring_instrument:read"))]
InstWrite = Annotated[Principal, Depends(require("quality.instrument:write"))]


def _cfg(request: Request, tid: str) -> QualityConfig:
    cfg = request.app.state.services.quality.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用质量体系模块")
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
    if isinstance(e, qs.QualityError):
        return HTTPException(e.status, detail=str(e))
    if isinstance(e, Forbidden):
        return HTTPException(403, detail=str(e))
    if isinstance(e, Conflict):
        return HTTPException(409, detail=str(e))
    if isinstance(e, Invalid | LedgerError):
        return HTTPException(422, detail=str(e))
    raise e


async def _submit_approval(request: Request, session: SessionDep, p: Principal, err: ApprovalRequired):
    ap, notices = await request.app.state.approvals.submit(
        session,
        p,
        err.request,
        err.decision,
        source="api:quality",
        lookup=lookup_for(request),
    )
    await session.commit()
    await send_notices(notices)
    return JSONResponse(
        {"approval": approval_out(ap), "message": f"{err}，已提交审批单 {ap.id[:8]}"},
        status_code=202,
    )


# ---------------- 待检任务与检验入账 ----------------


@router.get("/pending")
async def get_pending(
    request: Request,
    session: SessionDep,
    p: InsRead,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await qs.pending_tasks(session, p.tenant_id, mode=mode)


@router.post("/inspections", status_code=201)
async def create_inspection(
    body: qs.InspectionIn,
    request: Request,
    session: SessionDep,
    p: InsWrite,
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await qs.record_inspection(
            session, p, request.app.state.ledger, cfg, _tz(request, p.tenant_id), body
        )
    except (qs.QualityError, LedgerError, ValueError) as e:
        if isinstance(e, ValueError) and not isinstance(e, qs.QualityError | LedgerError):
            raise HTTPException(422, detail=str(e)) from e
        raise _http_err(e) from e


# ---------------- NCR 与 8D 闭环 ----------------


@router.get("/ncrs")
async def list_ncrs(
    request: Request,
    session: SessionDep,
    p: NcrRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    items = await qs.ncr_board(session, p.tenant_id, status=status, mode=mode)
    return {"items": items, "total": len(items), "data_mode": mode}


@router.post("/ncrs", status_code=201)
async def create_ncr(
    body: qs.NcrOpenIn,
    request: Request,
    session: SessionDep,
    p: NcrWrite,
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await qs.open_ncr(session, p, request.app.state.ledger, cfg, _tz(request, p.tenant_id), body)
    except (qs.QualityError, LedgerError) as e:
        raise _http_err(e) from e


@router.get("/ncrs/{ncr_id}/8d-draft")
async def get_8d_draft(
    ncr_id: str, request: Request, session: SessionDep, p: NcrRead
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    st = request.app.state
    try:
        return await qs.draft_8d(session, st.ontologies[p.tenant_id], st.traces.get(p.tenant_id), p, ncr_id)
    except qs.QualityError as e:
        raise _http_err(e) from e


@router.post("/ncrs/{ncr_id}/disposition")
async def dispose_ncr(
    ncr_id: str,
    body: qs.NcrDispositionIn,
    request: Request,
    session: SessionDep,
    p: NcrWrite,
):
    _cfg(request, p.tenant_id)
    try:
        res = await qs.disposition_ncr(session, p, request.app.state.ledger, ncr_id, body)
        return res.model_dump()
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, err)
    except (qs.QualityError, LedgerError) as e:
        raise _http_err(e) from e


@router.patch("/ncrs/{ncr_id}/8d")
async def patch_8d(
    ncr_id: str,
    body: qs.Ncr8dIn,
    request: Request,
    session: SessionDep,
    p: NcrWrite,
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await qs.update_8d(session, p, request.app.state.ledger, ncr_id, body)
        return res.model_dump()
    except (qs.QualityError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/ncrs/{ncr_id}/close")
async def close_ncr_ep(
    ncr_id: str,
    body: qs.NcrCloseIn,
    request: Request,
    session: SessionDep,
    p: NcrWrite,
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await qs.close_ncr(session, p, request.app.state.ledger, ncr_id, body)
        return res.model_dump()
    except (qs.QualityError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 质保书（CoA） ----------------


@router.get("/batches/{batch_id}/certificate")
async def get_certificate(
    batch_id: str, request: Request, session: SessionDep, p: InsRead
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    try:
        return await qs.certificate_data(session, p.tenant_id, cfg, batch_id)
    except qs.QualityError as e:
        raise _http_err(e) from e


@router.get("/batches/{batch_id}/certificate/export")
async def export_certificate(
    batch_id: str,
    request: Request,
    session: SessionDep,
    p: InsRead,
    format: Literal["docx", "xlsx"] = "docx",
) -> Response:
    cfg = _cfg(request, p.tenant_id)
    t = request.app.state.registry.get(p.tenant_id)
    factory = t.name if t else p.tenant_id
    try:
        coa = await qs.certificate_data(session, p.tenant_id, cfg, batch_id)
    except qs.QualityError as e:
        raise _http_err(e) from e
    if format == "xlsx":
        data = qe.certificate_xlsx(factory, coa)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        data = qe.certificate_docx(factory, coa)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    fname = quote(f"质保书-{batch_id}.{format}")
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


# ---------------- 计量器具校准 ----------------


@router.get("/instruments")
async def list_instruments(
    request: Request,
    session: SessionDep,
    p: InstRead,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await qs.instruments_board(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)


@router.post("/instruments", status_code=201)
async def create_instrument(
    body: qs.InstrumentIn,
    request: Request,
    session: SessionDep,
    p: InstWrite,
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        res = await qs.register_instrument(session, p, request.app.state.ledger, body)
        return res.model_dump()
    except (qs.QualityError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/instruments/{inst_id}/calibrate")
async def calibrate_instrument(
    inst_id: str,
    body: qs.CalibrateIn,
    request: Request,
    session: SessionDep,
    p: InstWrite,
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await qs.record_calibration(session, p, request.app.state.ledger, inst_id, body)
        return res.model_dump()
    except (qs.QualityError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/instruments/remind")
async def trigger_calibration_remind(
    request: Request,
    session: SessionDep,
    p: InstWrite,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await qs.remind_calibration_due(
        session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode
    )


# ---------------- ISO 9001 审核证据包 ----------------


@router.get("/iso-audit")
async def get_iso_audit(
    request: Request,
    session: SessionDep,
    p: InsRead,
    start: date | None = None,
    end: date | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    today = _today(request, p.tenant_id)
    end_d = end or today
    start_d = start or (end_d - timedelta(days=29))
    try:
        return await qs.iso_audit_pack(
            session, p.tenant_id, cfg, start_d, end_d, _tz(request, p.tenant_id), mode=mode
        )
    except qs.QualityError as e:
        raise _http_err(e) from e


@router.get("/iso-audit/export")
async def export_iso_audit(
    request: Request,
    session: SessionDep,
    p: InsRead,
    start: date | None = None,
    end: date | None = None,
) -> Response:
    cfg = _cfg(request, p.tenant_id)
    today = _today(request, p.tenant_id)
    end_d = end or today
    start_d = start or (end_d - timedelta(days=29))
    t = request.app.state.registry.get(p.tenant_id)
    factory = t.name if t else p.tenant_id
    try:
        pack = await qs.iso_audit_pack(
            session, p.tenant_id, cfg, start_d, end_d, _tz(request, p.tenant_id), mode="real"
        )
    except qs.QualityError as e:
        raise _http_err(e) from e
    data = qe.iso_audit_xlsx(factory, pack)
    fname = quote(f"ISO审核证据包-{start_d.isoformat()}_{end_d.isoformat()}.xlsx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


__all__ = ["PrincipalDep", "router"]
