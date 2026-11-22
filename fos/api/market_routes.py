"""市场商机线索与经营决策驾驶舱 API（W27c）：商机漏斗、大额丢单复盘审批、竞品情报、经营总览与三表导出。"""

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
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.market import export as mx
from fos.market import service as ms
from fos.market.config import MarketConfig
from fos.policy.approvals import send_notices

router = APIRouter(prefix="/v1/market", tags=["market"])

LeadRead = Annotated[Principal, Depends(require("market.lead:read"))]
LeadWrite = Annotated[Principal, Depends(require("market.lead:write"))]
IntelRead = Annotated[Principal, Depends(require("market.intel:read"))]
IntelWrite = Annotated[Principal, Depends(require("market.intel:write"))]


def _cfg(request: Request, tid: str) -> MarketConfig:
    cfg = request.app.state.services.market.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用市场商机与经营决策模块")
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
    if isinstance(e, ms.MarketError):
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
        source="api:market",
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


# ---------------- 市场商机漏斗 ----------------


@router.get("/leads")
async def list_leads(
    request: Request,
    session: SessionDep,
    p: LeadRead,
    stage: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await ms.funnel_board(session, p.tenant_id, stage=stage, mode=mode)


@router.post("/leads", status_code=201)
async def create_lead_ep(
    body: ms.LeadIn, request: Request, session: SessionDep, p: LeadWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await ms.create_lead(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), cfg, body
        )
    except (ms.MarketError, LedgerError) as e:
        raise _http_err(e) from e


@router.patch("/leads/{lead_id}")
async def patch_lead_ep(
    lead_id: str, body: ms.LeadUpdateIn, request: Request, session: SessionDep, p: LeadWrite
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    try:
        res = await ms.update_lead(session, p, request.app.state.ledger, cfg, lead_id, body)
        return res.model_dump()
    except (ms.MarketError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/leads/{lead_id}/close")
async def close_lead_ep(
    lead_id: str, body: ms.LeadCloseIn, request: Request, session: SessionDep, p: LeadWrite
):
    _cfg(request, p.tenant_id)
    try:
        req = await ms.close_lead_request(session, p, lead_id, body)
        res = await request.app.state.ledger.append(session, p, req)
        return res.model_dump()
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (ms.MarketError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 竞品行情情报 ----------------


@router.get("/intel")
async def list_intel(
    request: Request, session: SessionDep, p: IntelRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await ms.intel_board(session, p.tenant_id, mode=mode)


@router.post("/intel", status_code=201)
async def create_intel_ep(
    body: ms.IntelIn, request: Request, session: SessionDep, p: IntelWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await ms.log_intel(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (ms.MarketError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 经营决策驾驶舱与三表导出 ----------------


@router.get("/cockpit")
async def get_cockpit(
    request: Request, session: SessionDep, p: LeadRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await ms.executive_cockpit(session, p.tenant_id, _today(request, p.tenant_id), mode=mode)


@router.get("/report.xlsx")
async def export_market_report(request: Request, session: SessionDep, p: LeadRead) -> Response:
    """导出经营决策驾驶舱、商机漏斗与竞品情报三表 Excel（规则 4：正式报表仅使用真实数据）。"""
    _cfg(request, p.tenant_id)
    today = _today(request, p.tenant_id)
    tenant = request.app.state.registry.get(p.tenant_id)
    factory = tenant.name if tenant else p.tenant_id
    cockpit = await ms.executive_cockpit(session, p.tenant_id, today, mode="real")
    leads = (await ms.funnel_board(session, p.tenant_id, mode="real"))["items"]
    intels = (await ms.intel_board(session, p.tenant_id, mode="real"))["items"]
    xlsx = mx.market_report_xlsx(factory, cockpit, leads, intels)
    fname = f"经营决策与市场商机_{today.isoformat()}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
