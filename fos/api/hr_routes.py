"""人力、排班、计件与特种资质 API（W26）：花名册、证照预警、考勤计件、请假审批、月度薪酬与四表导出。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from fos.api.channel_routes import lookup_for
from fos.api.deps import SessionDep, require
from fos.api.inbox_routes import approval_out
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.hr import export as hrx
from fos.hr import service as hrs
from fos.hr.config import HrConfig
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices

router = APIRouter(prefix="/v1/hr", tags=["hr"])

EmpRead = Annotated[Principal, Depends(require("hr.employee:read"))]
EmpWrite = Annotated[Principal, Depends(require("hr.employee:write"))]
CertRead = Annotated[Principal, Depends(require("hr.cert:read"))]
CertWrite = Annotated[Principal, Depends(require("hr.cert:write"))]
RstRead = Annotated[Principal, Depends(require("hr.roster:read"))]
RstWrite = Annotated[Principal, Depends(require("hr.roster:write"))]
LvRead = Annotated[Principal, Depends(require("hr.leave:read"))]
LvWrite = Annotated[Principal, Depends(require("hr.leave:write"))]


def _cfg(request: Request, tid: str) -> HrConfig:
    cfg = request.app.state.services.hr.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用人力排班与薪酬模块")
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
    if isinstance(e, hrs.HrError):
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
        source="api:hr",
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


# ---------------- 员工花名册 ----------------


@router.get("/employees")
async def list_employees(
    request: Request,
    session: SessionDep,
    p: EmpRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await hrs.employees_board(session, p.tenant_id, status=status, mode=mode)


@router.post("/employees", status_code=201)
async def create_employee(
    body: hrs.EmployeeIn, request: Request, session: SessionDep, p: EmpWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await hrs.register_employee(session, p, request.app.state.ledger, body)
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


@router.patch("/employees/{emp_id}")
async def patch_employee(
    emp_id: str, body: hrs.EmployeeUpdateIn, request: Request, session: SessionDep, p: EmpWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hrs.update_employee(session, p, request.app.state.ledger, emp_id, body)
        return res.model_dump()
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 特种作业资质 ----------------


@router.get("/certs")
async def list_certs(
    request: Request, session: SessionDep, p: CertRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await hrs.certs_board(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)


@router.post("/certs", status_code=201)
async def create_cert(
    body: hrs.WorkCertIn, request: Request, session: SessionDep, p: CertWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await hrs.record_cert(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/certs/{cert_id}/renew")
async def renew_cert_ep(
    cert_id: str, body: hrs.WorkCertRenewIn, request: Request, session: SessionDep, p: CertWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hrs.renew_cert(session, p, request.app.state.ledger, cert_id, body)
        return res.model_dump()
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/certs/remind")
async def trigger_cert_remind(
    request: Request, session: SessionDep, p: CertWrite, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await hrs.remind_expiring_certs(
        session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode
    )


# ---------------- 排班与计件考勤 ----------------


@router.get("/rosters")
async def list_rosters(
    request: Request,
    session: SessionDep,
    p: RstRead,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    employee: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await hrs.roster_board(session, p.tenant_id, month=month, employee=employee, mode=mode)


@router.post("/rosters", status_code=201)
async def create_roster(
    body: hrs.RosterIn, request: Request, session: SessionDep, p: RstWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    cfg = _cfg(request, p.tenant_id)
    try:
        return await hrs.log_roster(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), cfg, body
        )
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 请假单 ----------------


@router.get("/leaves")
async def list_leaves(
    request: Request, session: SessionDep, p: LvRead, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await hrs.leaves_board(session, p.tenant_id, mode=mode)


@router.post("/leaves", status_code=201)
async def create_leave(body: hrs.LeaveIn, request: Request, session: SessionDep, p: LvWrite):
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    lid, req = hrs.submit_leave_request(_tz(request, p.tenant_id), body)
    try:
        res = await request.app.state.ledger.append(session, p, req)
        return {"id": lid, "event": res.model_dump()}
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/leaves/{leave_id}/cancel")
async def cancel_leave_ep(
    leave_id: str, request: Request, session: SessionDep, p: LvWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await hrs.cancel_leave(session, p, request.app.state.ledger, leave_id)
        return res.model_dump()
    except (hrs.HrError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 月度薪酬核算与四表导出 ----------------


@router.get("/payroll")
async def get_payroll(
    request: Request,
    session: SessionDep,
    p: RstRead,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    m = month or _today(request, p.tenant_id).strftime("%Y-%m")
    return await hrs.payroll_report(session, p.tenant_id, cfg, m, mode=mode)


@router.get("/report.xlsx")
async def export_hr_report(
    request: Request,
    session: SessionDep,
    p: RstRead,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> Response:
    """导出人力与月度计件薪酬四表 Excel（规则 4：正式报表仅使用真实数据）。"""
    cfg = _cfg(request, p.tenant_id)
    today = _today(request, p.tenant_id)
    m = month or today.strftime("%Y-%m")
    tenant = request.app.state.registry.get(p.tenant_id)
    factory = tenant.name if tenant else p.tenant_id
    emps = (await hrs.employees_board(session, p.tenant_id, mode="real"))["items"]
    certs = (await hrs.certs_board(session, p.tenant_id, cfg, today, mode="real"))["items"]
    rosters = (await hrs.roster_board(session, p.tenant_id, month=m, mode="real"))["items"]
    pay = await hrs.payroll_report(session, p.tenant_id, cfg, m, mode="real")
    xlsx = hrx.hr_report_xlsx(factory, m, emps, certs, rosters, pay)
    fname = f"人力排班与薪酬_{m}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
