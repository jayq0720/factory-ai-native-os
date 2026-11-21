"""设备管理（EAM）API（W24）：设备台账、保养计划、维修工单、日常点检、备件库存与可靠性报表。"""

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
from fos.eam import export as ee
from fos.eam import service as es
from fos.eam.config import EamConfig
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices

router = APIRouter(prefix="/v1/eam", tags=["eam"])

EqRead = Annotated[Principal, Depends(require("eam.equipment:read"))]
EqWrite = Annotated[Principal, Depends(require("eam.equipment:write"))]
PmRead = Annotated[Principal, Depends(require("eam.maintenance:read"))]
PmWrite = Annotated[Principal, Depends(require("eam.maintenance:write"))]
WoRead = Annotated[Principal, Depends(require("eam.work_order:read"))]
WoWrite = Annotated[Principal, Depends(require("eam.work_order:write"))]
InspWrite = Annotated[Principal, Depends(require("eam.inspection:write"))]


def _cfg(request: Request, tid: str) -> EamConfig:
    cfg = request.app.state.services.eam.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用设备管理模块")
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
    if isinstance(e, es.EamError):
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
        source="api:eam",
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


# ---------------- 设备台账 ----------------


@router.get("/equipment")
async def list_equipment(
    request: Request,
    session: SessionDep,
    p: EqRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await es.equipment_board(session, p.tenant_id, status=status, mode=mode)


@router.post("/equipment", status_code=201)
async def create_equipment(
    body: es.EquipmentIn, request: Request, session: SessionDep, p: EqWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await es.register_equipment(session, p, request.app.state.ledger, body)
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/equipment/{equip_id}/retire")
async def retire_equipment(
    equip_id: str, body: es.EquipmentRetireIn, request: Request, session: SessionDep, p: EqWrite
):
    _cfg(request, p.tenant_id)
    try:
        req = await es.retire_equipment_request(session, p, equip_id, body)
        res = await request.app.state.ledger.append(session, p, req)
        return res.model_dump()
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 保养计划（PM） ----------------


@router.get("/pm")
async def list_pm_plans(
    request: Request, session: SessionDep, p: PmRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await es.pm_board(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)


@router.post("/pm", status_code=201)
async def create_pm(body: es.PmPlanIn, request: Request, session: SessionDep, p: PmWrite) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await es.create_pm_plan(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/pm/{plan_id}/complete")
async def complete_pm_ep(
    plan_id: str, body: es.PmCompleteIn, request: Request, session: SessionDep, p: PmWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await es.complete_pm(session, p, request.app.state.ledger, plan_id, body)
        return res.model_dump()
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/pm/remind")
async def trigger_pm_remind(
    request: Request, session: SessionDep, p: PmWrite, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await es.remind_pm_due(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)


# ---------------- 维修工单与日常点检 ----------------


@router.get("/work-orders")
async def list_work_orders(
    request: Request,
    session: SessionDep,
    p: WoRead,
    status: str | None = None,
    equipment: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    rows = await es.work_orders_board(session, p.tenant_id, status=status, equipment=equipment, mode=mode)
    return {"items": rows, "total": len(rows)}


@router.post("/work-orders", status_code=201)
async def create_work_order(
    body: es.WorkOrderOpenIn, request: Request, session: SessionDep, p: WoWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await es.open_work_order(
            session, p, request.app.state.ledger, cfg, _tz(request, p.tenant_id), body
        )
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/work-orders/{wo_id}/start")
async def start_wo_ep(
    wo_id: str, body: es.WorkOrderStartIn, request: Request, session: SessionDep, p: WoWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await es.start_work_order(session, p, request.app.state.ledger, wo_id, body)
        return res.model_dump()
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/work-orders/{wo_id}/complete")
async def complete_wo_ep(
    wo_id: str, body: es.WorkOrderCompleteIn, request: Request, session: SessionDep, p: WoWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await es.complete_work_order(session, p, request.app.state.ledger, wo_id, body)
        return res.model_dump()
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/inspections", status_code=201)
async def create_inspection_log(
    body: es.InspectionLogIn, request: Request, session: SessionDep, p: InspWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await es.log_inspection(
            session, p, request.app.state.ledger, cfg, _tz(request, p.tenant_id), body
        )
    except (es.EamError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 备件库存、可靠性报表与导出 ----------------


@router.get("/spare-parts")
async def get_spare_parts(
    request: Request, session: SessionDep, p: EqRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await es.spare_parts_board(session, p.tenant_id, mode=mode)


@router.get("/reliability")
async def get_reliability(
    request: Request,
    session: SessionDep,
    p: WoRead,
    window_days: int | None = None,
    equipment: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await es.reliability_report(
        session, p.tenant_id, cfg, window_days=window_days, equipment=equipment, mode=mode
    )


@router.get("/report/export")
async def export_eam_report(
    request: Request,
    session: SessionDep,
    p: EqRead,
    window_days: int | None = None,
) -> Response:
    """导出正式设备管理三表 Excel（仅真实数据）。"""
    cfg = _cfg(request, p.tenant_id)
    eq = await es.equipment_board(session, p.tenant_id, mode="real")
    wos = await es.work_orders_board(session, p.tenant_id, mode="real")
    rel = await es.reliability_report(session, p.tenant_id, cfg, window_days=window_days, mode="real")
    t = request.app.state.registry.get(p.tenant_id)
    factory = t.name if t else p.tenant_id
    data = ee.eam_report_xlsx(factory, eq, wos, rel)
    fname = quote(f"设备管理报表_{_today(request, p.tenant_id).isoformat()}.xlsx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )
