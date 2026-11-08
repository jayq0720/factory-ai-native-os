"""采购与仓储接口（W17，说明见 docs/supply.md）。

库存（仓管）：
- GET  /v1/stock                         实时库存（每种物料：可用 / 待检 / 不可用、安全库存、批次）
- POST /v1/stock/issues                  领料：指定批次，或只给物料按先进先出分配；
                                         领后低于安全库存推给仓管和采购
- POST /v1/stock/returns                 退料
- POST /v1/stock/counts/preview          盘点预览：盘点数 − 账面数（不入账）
- POST /v1/stock/counts                  盘点确认：差异记盘盈 / 盘亏（附盘点照片），全有或全无
采购：
- GET  /v1/purchase/suggestions          请购建议（排窑计划缺口 + 安全库存 − 未下单请购；不入账）
- GET  /v1/purchase/requisitions         请购单（待下单 / 审批中 / 已下单 / 已取消）
- POST /v1/purchase/requisitions         生成请购单（采购员确认，全有或全无）
- POST /v1/purchase/requisitions/{id}/cancel
- GET  /v1/purchase/compare              比价
- POST /v1/purchase/orders               下采购单（金额由代码算；超阈值走审批，返回 202）
- GET  /v1/purchase/orders               采购单和到货进度
- POST /v1/purchase/orders/{id}/receipts 按采购单登记到货（仓管；批次状态为待检）
- GET  /v1/purchase/suppliers            供应商评分

确认都以调用人身份 Ledger.append()；跨对象规则在账本守卫里（fos/supply/guards.py）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fos.api.deps import SessionDep, require
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, ApprovalRequired, Conflict, Forbidden, Invalid, NotFound
from fos.orders.data import get
from fos.supply.moves import (
    CountIn,
    IssueIn,
    ReturnIn,
    count_preview,
    count_requests,
    issue_requests,
    return_request,
)
from fos.supply.purchase import (
    PurchaseOrderIn,
    ReceiptIn,
    RequisitionsIn,
    SupplyError,
    after_receipt,
    compare_quotes,
    po_request,
    po_row,
    po_rows,
    receipt_request,
    requisition_requests,
    requisition_rows,
    suggestions,
)
from fos.supply.stock import material_stock
from fos.supply.suppliers import FORMULA, score_suppliers

router = APIRouter()
_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}
Mode = Annotated[Literal["real", "mock"], Query()]

StockReader = Annotated[Principal, Depends(require("warehouse.material_lot:read"))]
StockWriter = Annotated[Principal, Depends(require("warehouse.stock:write"))]
ReqReader = Annotated[Principal, Depends(require("purchase.purchase_requisition:read"))]
ReqWriter = Annotated[Principal, Depends(require("purchase.requisition:write"))]
POReader = Annotated[Principal, Depends(require("purchase.purchase_order:read"))]
POWriter = Annotated[Principal, Depends(require("purchase.order:write"))]
QuoteReader = Annotated[Principal, Depends(require("purchase.supplier_quote:read"))]
SupplierReader = Annotated[Principal, Depends(require("purchase.supplier:read"))]


def _env(request: Request, principal: Principal):
    st = request.app.state
    tid = principal.tenant_id
    cfg = st.supply.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="这个租户没有开采购仓储模块")
    return cfg, st.orders[tid], ZoneInfo(st.registry.get(tid).timezone)


def _err(e: Exception) -> HTTPException:
    if isinstance(e, SupplyError):
        return HTTPException(e.status, detail=str(e))
    return HTTPException(_STATUS[type(e)], detail=str(e))


async def _append_all(
    session: AsyncSession, request: Request, principal: Principal, reqs: list[AppendRequest]
):
    """逐条入账，任何一条失败整批回滚（由请求事务负责）。"""
    out = []
    for r in reqs:
        try:
            out.append((await request.app.state.ledger.append(session, principal, r)).model_dump())
        except ApprovalRequired as e:
            raise HTTPException(409, detail=f"{r.type} 命中审批规则（{e}），请单独提交") from e
        except (Forbidden, NotFound, Conflict, Invalid) as e:
            raise _err(e) from e
    return out


# ---------------- 库存 ----------------


@router.get("/v1/stock")
async def stock(
    request: Request,
    session: SessionDep,
    principal: StockReader,
    mode: Mode = "real",
    material: Annotated[str | None, Query(max_length=64)] = None,
) -> Any:
    cfg, ocfg, _ = _env(request, principal)
    return await material_stock(
        session, principal.tenant_id, cfg, ocfg, mode=mode, material_ids={material} if material else None
    )


async def _low_stock_texts(session, request, principal, cfg, ocfg, material_ids: set[str], mode: str):
    from fos.planning.notify import dm, role_identities

    if mode != "real" or not material_ids:
        return []
    rows = (await material_stock(session, principal.tenant_id, cfg, ocfg, material_ids=material_ids))[
        "materials"
    ]
    low = [r for r in rows if r["below_safety"]]
    if not low:
        return []
    msg = "以下物料领用后低于安全库存：\n" + "\n".join(
        f"- {r['name']}：可用 {r['usable']:g} {r['unit']}，安全库存 {r['safety_stock']:g}"
        + (f"，待检 {r['incoming']:g}" if r["incoming"] else "")
        for r in low
    )
    idents = await role_identities(session, principal.tenant_id, cfg.stock.low_stock_notify_roles)
    return [(dm(i), msg) for _, i in idents]


async def _send(request: Request, tenant: str, texts) -> None:
    if texts:
        from fos.api.channel_routes import lookup_for
        from fos.planning.notify import send_texts

        await send_texts(lookup_for(request), tenant, texts)


@router.post("/v1/stock/issues", status_code=201)
async def issue(
    body: IssueIn, request: Request, session: SessionDep, principal: StockWriter
) -> dict[str, Any]:
    cfg, ocfg, tz = _env(request, principal)
    try:
        reqs, alloc = await issue_requests(session, principal.tenant_id, cfg, ocfg, body, tz)
    except SupplyError as e:
        raise _err(e) from e
    events = await _append_all(session, request, principal, reqs)
    mats = {r.payload["material"] for r in reqs}
    texts = await _low_stock_texts(session, request, principal, cfg, ocfg, mats, reqs[0].data_mode)
    after = await material_stock(
        session, principal.tenant_id, cfg, ocfg, mode=reqs[0].data_mode, material_ids=mats
    )
    await session.commit()  # 先提交再推送
    await _send(request, principal.tenant_id, texts)
    return {"events": events, "allocation": alloc, "stock": after["materials"], "notified": len(texts)}


@router.post("/v1/stock/returns", status_code=201)
async def return_(
    body: ReturnIn, request: Request, session: SessionDep, principal: StockWriter
) -> dict[str, Any]:
    _, _, tz = _env(request, principal)
    try:
        req = await return_request(session, principal.tenant_id, body, tz)
    except SupplyError as e:
        raise _err(e) from e
    return {"events": await _append_all(session, request, principal, [req])}


@router.post("/v1/stock/counts/preview")
async def count_preview_route(
    body: CountIn, request: Request, session: SessionDep, principal: StockReader
) -> dict[str, Any]:
    _env(request, principal)
    try:
        return await count_preview(session, principal.tenant_id, body)
    except SupplyError as e:
        raise _err(e) from e


@router.post("/v1/stock/counts", status_code=201)
async def count(
    body: CountIn, request: Request, session: SessionDep, principal: StockWriter
) -> dict[str, Any]:
    _, _, tz = _env(request, principal)
    try:
        reqs, pv = await count_requests(session, principal.tenant_id, body, tz)
    except SupplyError as e:
        raise _err(e) from e
    return {**pv, "events": await _append_all(session, request, principal, reqs)}


# ---------------- 请购 ----------------


@router.get("/v1/purchase/suggestions")
async def suggestions_route(
    request: Request, session: SessionDep, principal: ReqReader, mode: Mode = "real"
) -> Any:
    cfg, ocfg, tz = _env(request, principal)
    return await suggestions(
        session,
        principal.tenant_id,
        cfg,
        ocfg,
        planning=request.app.state.planning.get(principal.tenant_id),
        tz=tz,
        now=utcnow(),
        mode=mode,
    )


@router.get("/v1/purchase/requisitions")
async def requisitions(
    request: Request, session: SessionDep, principal: ReqReader, mode: Mode = "real", open_only: bool = False
) -> Any:
    _env(request, principal)
    return {
        "requisitions": await requisition_rows(session, principal.tenant_id, mode=mode, only_open=open_only)
    }


@router.post("/v1/purchase/requisitions", status_code=201)
async def raise_requisitions(
    body: RequisitionsIn, request: Request, session: SessionDep, principal: ReqWriter
) -> dict[str, Any]:
    _, _, tz = _env(request, principal)
    try:
        reqs = await requisition_requests(session, principal.tenant_id, body, tz)
    except SupplyError as e:
        raise _err(e) from e
    return {"events": await _append_all(session, request, principal, reqs)}


class CancelIn(BaseModel):
    note: str = Field(min_length=1, max_length=500)


@router.post("/v1/purchase/requisitions/{req_id}/cancel")
async def cancel_requisition(
    req_id: str, body: CancelIn, request: Request, session: SessionDep, principal: ReqWriter
) -> dict[str, Any]:
    _env(request, principal)
    rows = {
        r["id"]: r
        for mode in ("real", "mock")
        for r in await requisition_rows(session, principal.tenant_id, mode=mode)
    }
    r = rows.get(req_id)
    if r is None:
        raise HTTPException(404, detail="请购单不存在")
    if r["stage"] != "open":
        raise HTTPException(409, detail=f"请购单{r['stage_text']}，不能取消")
    obj = await get(session, principal.tenant_id, "purchase_requisition", req_id)
    assert obj is not None
    req = AppendRequest(
        type="purchase.requisition_cancelled",
        object_id=req_id,
        payload={"status": "cancelled", "note": body.note},
        expected_version=obj.version,
        data_mode=obj.data_mode,
    )
    return {"events": await _append_all(session, request, principal, [req])}


@router.get("/v1/purchase/compare")
async def compare(
    request: Request,
    session: SessionDep,
    principal: QuoteReader,
    material: Annotated[str, Query(min_length=1, max_length=64)],
    qty: Annotated[float | None, Query(gt=0)] = None,
    need_date: date | None = None,
    mode: Mode = "real",
) -> Any:
    cfg, ocfg, tz = _env(request, principal)
    try:
        return await compare_quotes(
            session,
            principal.tenant_id,
            cfg,
            ocfg,
            material_id=material,
            qty=qty,
            need_date=need_date,
            today=utcnow().astimezone(tz).date(),
            tz=tz,
            mode=mode,
        )
    except SupplyError as e:
        raise _err(e) from e


# ---------------- 采购单、到货 ----------------


@router.post("/v1/purchase/orders", status_code=201)
async def create_po(body: PurchaseOrderIn, request: Request, session: SessionDep, principal: POWriter) -> Any:
    _, _, tz = _env(request, principal)
    today = utcnow().astimezone(tz).date()
    try:
        req, check = await po_request(session, principal.tenant_id, body, today, tz)
    except SupplyError as e:
        raise _err(e) from e
    try:
        res = await request.app.state.ledger.append(session, principal, req)
    except ApprovalRequired as e:
        from fos.api.channel_routes import lookup_for
        from fos.api.inbox_routes import approval_out
        from fos.policy.approvals import send_notices

        lookup = lookup_for(request)
        sub = await request.app.state.approvals.submit(
            session, principal, req, e.decision, source="api", lookup=lookup
        )
        await session.commit()
        await send_notices(lookup, principal.tenant_id, sub.notices)
        return JSONResponse(
            {
                "status": "pending_approval",
                "message": sub.message(),
                "approval": approval_out(sub.approval),
                **check,
            },
            status_code=202,
        )
    except (Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    return {"status": "created", **res.model_dump(), **check}


@router.get("/v1/purchase/orders")
async def list_pos(
    request: Request,
    session: SessionDep,
    principal: POReader,
    mode: Mode = "real",
    status: Annotated[str | None, Query(max_length=16)] = None,
    material: Annotated[str | None, Query(max_length=64)] = None,
) -> Any:
    _, ocfg, tz = _env(request, principal)
    today = utcnow().astimezone(tz).date()
    rows = await po_rows(
        session, principal.tenant_id, ocfg, today, mode=mode, status=status, material=material
    )
    return {"orders": rows}


@router.post("/v1/purchase/orders/{po_id}/receipts", status_code=201)
async def receive(
    po_id: str, body: ReceiptIn, request: Request, session: SessionDep, principal: StockWriter
) -> dict[str, Any]:
    cfg, ocfg, tz = _env(request, principal)
    try:
        req, _info = await receipt_request(session, principal.tenant_id, cfg, ocfg, po_id, body, tz)
    except SupplyError as e:
        raise _err(e) from e
    (ev,) = await _append_all(session, request, principal, [req])
    from fos.ledger import AppendResult

    today = utcnow().astimezone(tz).date()
    notes = await after_receipt(
        session, request.app.state.ledger, principal, ocfg, po_id, AppendResult(**ev), today
    )
    po = await get(session, principal.tenant_id, "purchase_order", po_id)
    assert po is not None
    return {**ev, "notes": notes, "order": await po_row(session, principal.tenant_id, ocfg, po, today)}


@router.get("/v1/purchase/suppliers")
async def suppliers(
    request: Request, session: SessionDep, principal: SupplierReader, mode: Mode = "real"
) -> Any:
    cfg, ocfg, tz = _env(request, principal)
    rows = await score_suppliers(
        session, principal.tenant_id, cfg, ocfg, today=utcnow().astimezone(tz).date(), tz=tz, mode=mode
    )
    return {"suppliers": rows, "window_days": cfg.suppliers.window_days, "formula": FORMULA}
