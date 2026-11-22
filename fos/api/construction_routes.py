"""筑炉施工与烘炉验收 API（W27b）：施工立项、日报进度、烘炉曲线监测、竣工验收审批与三表导出。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fos.api.channel_routes import lookup_for
from fos.api.deps import SessionDep, require
from fos.api.inbox_routes import approval_out
from fos.construction import export as cx
from fos.construction import service as cs
from fos.construction.config import ConstructionConfig
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices

router = APIRouter(prefix="/v1/construction", tags=["construction"])

SiteRead = Annotated[Principal, Depends(require("construction.site:read"))]
SiteWrite = Annotated[Principal, Depends(require("construction.site:write"))]
LogRead = Annotated[Principal, Depends(require("construction.log:read"))]
LogWrite = Annotated[Principal, Depends(require("construction.log:write"))]
DryRead = Annotated[Principal, Depends(require("construction.dryout:read"))]
DryWrite = Annotated[Principal, Depends(require("construction.dryout:write"))]


def _cfg(request: Request, tid: str) -> ConstructionConfig:
    cfg = request.app.state.services.construction.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用筑炉施工管理模块")
    return cfg


def _tz(request: Request, tid: str) -> ZoneInfo:
    t = request.app.state.registry.get(tid)
    return ZoneInfo(t.timezone if t else "Asia/Shanghai")


def _check_mode(request: Request, mode: str) -> None:
    if mode == "mock" and request.app.state.settings.env == "prod":
        raise HTTPException(403, detail="生产环境不允许写入模拟数据")


def _http_err(e: Exception) -> HTTPException:
    if isinstance(e, cs.ConstructionError):
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
        source="api:construction",
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


# ---------------- 筑炉施工项目 ----------------


@router.get("/sites")
async def list_sites(
    request: Request,
    session: SessionDep,
    p: SiteRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await cs.sites_board(session, p.tenant_id, status=status, mode=mode)


@router.post("/sites", status_code=201)
async def create_site_ep(
    body: cs.SiteIn, request: Request, session: SessionDep, p: SiteWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await cs.create_site(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (cs.ConstructionError, LedgerError) as e:
        raise _http_err(e) from e


@router.patch("/sites/{site_id}")
async def patch_site_ep(
    site_id: str, body: cs.SiteUpdateIn, request: Request, session: SessionDep, p: SiteWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await cs.update_site(session, p, request.app.state.ledger, site_id, body)
        return res.model_dump()
    except (cs.ConstructionError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/sites/{site_id}/accept")
async def accept_site_ep(
    site_id: str, body: cs.SiteAcceptIn, request: Request, session: SessionDep, p: SiteWrite
):
    _cfg(request, p.tenant_id)
    try:
        req = await cs.accept_site_request(session, p, site_id, body)
        res = await request.app.state.ledger.append(session, p, req)
        return res.model_dump()
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (cs.ConstructionError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 施工日报 ----------------


@router.get("/logs")
async def list_logs(
    request: Request,
    session: SessionDep,
    p: LogRead,
    site: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await cs.logs_board(session, p.tenant_id, site=site, mode=mode)


@router.post("/logs", status_code=201)
async def create_log_ep(
    body: cs.SiteLogIn, request: Request, session: SessionDep, p: LogWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await cs.record_log(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (cs.ConstructionError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 烘炉升温记录 ----------------


@router.get("/dryout")
async def list_dryout(
    request: Request,
    session: SessionDep,
    p: DryRead,
    site: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await cs.dryout_board(session, p.tenant_id, site=site, mode=mode)


@router.post("/dryout", status_code=201)
async def create_dryout_ep(
    body: cs.DryoutIn, request: Request, session: SessionDep, p: DryWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await cs.record_dryout(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), cfg, body
        )
    except (cs.ConstructionError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 三表导出 ----------------


@router.get("/report.xlsx")
async def export_construction_report(request: Request, session: SessionDep, p: SiteRead) -> Response:
    """导出筑炉施工项目、施工日报与烘炉升温三表 Excel（规则 4：正式报表仅使用真实数据）。"""
    _cfg(request, p.tenant_id)
    today = utcnow().astimezone(_tz(request, p.tenant_id)).date().isoformat()
    tenant = request.app.state.registry.get(p.tenant_id)
    factory = tenant.name if tenant else p.tenant_id
    sites = (await cs.sites_board(session, p.tenant_id, mode="real"))["items"]
    logs = (await cs.logs_board(session, p.tenant_id, mode="real"))["items"]
    dryouts = (await cs.dryout_board(session, p.tenant_id, mode="real"))["items"]
    xlsx = cx.construction_report_xlsx(factory, sites, logs, dryouts)
    fname = f"筑炉施工与烘炉验收_{today}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
