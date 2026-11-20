"""财务集成与成本核算 API（W23）：应收应付、账龄、批次成本、订单毛利、凭证草稿与对账单导出。"""

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
from fos.finance import export as fe
from fos.finance import service as fs
from fos.finance.config import FinanceConfig
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, LedgerError
from fos.policy.approvals import send_notices

router = APIRouter(prefix="/v1/finance", tags=["finance"])

ArRead = Annotated[Principal, Depends(require("finance.ar:read"))]
ArWrite = Annotated[Principal, Depends(require("finance.ar:write"))]
ApRead = Annotated[Principal, Depends(require("finance.ap:read"))]
ApWrite = Annotated[Principal, Depends(require("finance.ap:write"))]
CostRead = Annotated[Principal, Depends(require("finance.cost:read"))]
VoucherRead = Annotated[Principal, Depends(require("finance.voucher:read"))]
VoucherWrite = Annotated[Principal, Depends(require("finance.voucher:write"))]


def _cfg(request: Request, tid: str) -> FinanceConfig:
    cfg = request.app.state.services.finance.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="当前租户未启用财务模块")
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
    if isinstance(e, fs.FinanceError):
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
        source="api:finance",
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


# ---------------- 应收账款（AR） ----------------


@router.get("/ar")
async def get_ar_board(
    request: Request,
    session: SessionDep,
    p: ArRead,
    status: str | None = None,
    customer: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await fs.ar_board(
        session, p.tenant_id, _today(request, p.tenant_id), status=status, customer=customer, mode=mode
    )


@router.post("/ar", status_code=201)
async def create_ar(body: fs.ArCreateIn, request: Request, session: SessionDep, p: ArWrite) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await fs.record_ar(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/ar/{ar_id}/invoice")
async def ar_invoice_ep(
    ar_id: str, body: fs.ArInvoiceIn, request: Request, session: SessionDep, p: ArWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await fs.invoice_ar(session, p, request.app.state.ledger, ar_id, body)
        return res.model_dump()
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/ar/{ar_id}/receive")
async def ar_receive_ep(
    ar_id: str, body: fs.ArReceiveIn, request: Request, session: SessionDep, p: ArWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await fs.receive_ar(session, p, request.app.state.ledger, ar_id, body)
        return res.model_dump()
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 应付账款（AP） ----------------


@router.get("/ap")
async def get_ap_board(
    request: Request,
    session: SessionDep,
    p: ApRead,
    status: str | None = None,
    supplier: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    return await fs.ap_board(
        session, p.tenant_id, _today(request, p.tenant_id), status=status, supplier=supplier, mode=mode
    )


@router.post("/ap", status_code=201)
async def create_ap(body: fs.ApCreateIn, request: Request, session: SessionDep, p: ApWrite) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await fs.record_ap(session, p, request.app.state.ledger, _tz(request, p.tenant_id), body)
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/ap/{ap_id}/pay")
async def ap_pay_ep(ap_id: str, body: fs.ApPayIn, request: Request, session: SessionDep, p: ApWrite):
    _cfg(request, p.tenant_id)
    try:
        req = await fs.pay_ap_request(session, p, ap_id, body)
        res = await request.app.state.ledger.append(session, p, req)
        return res.model_dump()
    except ApprovalRequired as err:
        return await _submit_approval(request, session, p, req, err)
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


# ---------------- 批次成本与订单毛利 ----------------


@router.get("/cost/batches/{batch_id}")
async def get_batch_cost(batch_id: str, request: Request, session: SessionDep, p: CostRead) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    try:
        return await fs.batch_cost_report(session, p.tenant_id, cfg, batch_id)
    except fs.FinanceError as e:
        raise _http_err(e) from e


@router.get("/cost/orders/{order_id}")
async def get_order_margin(
    order_id: str, request: Request, session: SessionDep, p: CostRead
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    try:
        return await fs.order_margin_report(session, p.tenant_id, cfg, order_id)
    except fs.FinanceError as e:
        raise _http_err(e) from e


# ---------------- 凭证草稿与导出（金蝶/用友/通用） ----------------


@router.get("/vouchers")
async def list_vouchers(
    request: Request,
    session: SessionDep,
    p: VoucherRead,
    status: str | None = None,
    mode: Literal["real", "mock"] = "real",
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    rows = await fs.vouchers_board(session, p.tenant_id, status=status, mode=mode)
    return {"items": rows, "total": len(rows)}


@router.post("/vouchers", status_code=201)
async def create_voucher(
    body: fs.VoucherDraftIn, request: Request, session: SessionDep, p: VoucherWrite
) -> dict[str, Any]:
    _check_mode(request, body.data_mode)
    _cfg(request, p.tenant_id)
    try:
        return await fs.draft_voucher_manual(
            session, p, request.app.state.ledger, _tz(request, p.tenant_id), body
        )
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/vouchers/from-source", status_code=201)
async def create_voucher_from_source(
    body: fs.VoucherFromSourceIn, request: Request, session: SessionDep, p: VoucherWrite
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    try:
        return await fs.draft_voucher_from_source(
            session, p, request.app.state.ledger, cfg, _tz(request, p.tenant_id), body
        )
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


@router.post("/vouchers/{voucher_id}/post")
async def post_voucher_ep(
    voucher_id: str, body: fs.VoucherPostIn, request: Request, session: SessionDep, p: VoucherWrite
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        res = await fs.post_voucher(session, p, request.app.state.ledger, voucher_id, body)
        return res.model_dump()
    except (fs.FinanceError, LedgerError) as e:
        raise _http_err(e) from e


@router.get("/vouchers/export")
async def export_vouchers(
    request: Request,
    session: SessionDep,
    p: VoucherWrite,
    target: Literal["generic", "kingdee", "yonyou"] = "kingdee",
    status: str | None = None,
    mark_exported: bool = True,
) -> Response:
    """导出正式凭证草稿 Excel（仅真实数据，禁止模拟数据），适配金蝶/用友导入。"""
    _cfg(request, p.tenant_id)
    rows = await fs.vouchers_board(session, p.tenant_id, status=status, mode="real")
    rows = [r for r in rows if r["status"] != "void"]
    if not rows:
        raise HTTPException(409, detail="没有可导出的真实凭证草稿")
    if mark_exported:
        await fs.mark_vouchers_exported(
            session,
            p,
            request.app.state.ledger,
            [r["id"] for r in rows if r["status"] == "draft"],
            external_system=target,
        )
    t = request.app.state.registry.get(p.tenant_id)
    factory = t.name if t else p.tenant_id
    data = fe.vouchers_export_xlsx(factory, rows, target=target)
    fname = quote(f"凭证导入_{target}_{_today(request, p.tenant_id).isoformat()}.xlsx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


# ---------------- 对账单与逾期提醒 ----------------


@router.get("/reconciliation/{party_type}/{party_id}")
async def get_reconciliation(
    party_type: Literal["customer", "supplier"],
    party_id: str,
    request: Request,
    session: SessionDep,
    p: ArRead,
) -> dict[str, Any]:
    _cfg(request, p.tenant_id)
    try:
        return await fs.reconciliation_data(
            session, p.tenant_id, _today(request, p.tenant_id), party_type=party_type, party_id=party_id
        )
    except fs.FinanceError as e:
        raise _http_err(e) from e


@router.get("/reconciliation/{party_type}/{party_id}/export")
async def export_reconciliation(
    party_type: Literal["customer", "supplier"],
    party_id: str,
    request: Request,
    session: SessionDep,
    p: ArRead,
) -> Response:
    _cfg(request, p.tenant_id)
    try:
        stmt = await fs.reconciliation_data(
            session, p.tenant_id, _today(request, p.tenant_id), party_type=party_type, party_id=party_id
        )
    except fs.FinanceError as e:
        raise _http_err(e) from e
    t = request.app.state.registry.get(p.tenant_id)
    factory = t.name if t else p.tenant_id
    data = fe.reconciliation_xlsx(factory, stmt)
    fname = quote(f"对账单_{stmt['party_name']}_{stmt['date']}.xlsx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


@router.post("/remind")
async def trigger_overdue_remind(
    request: Request, session: SessionDep, p: ArWrite, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    cfg = _cfg(request, p.tenant_id)
    return await fs.remind_overdue_ar_ap(session, p.tenant_id, cfg, _today(request, p.tenant_id), mode=mode)
