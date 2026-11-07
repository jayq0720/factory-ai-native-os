"""订单交付接口（W14–W15，说明见 docs/orders.md）。

- GET  /v1/contracts/draft?eng_project=ID   工程报价 → 合同草稿和订单行（只是草稿）
- POST /v1/contracts                        登记合同（命中审批规则返回 202）
- POST /v1/contracts/{id}/orders            合同拆订单行（全有或全无）
- GET  /v1/orders                           订单看板（交付进度 + 交期风险，有风险的排前面）
- GET  /v1/orders/{id}                      单个订单的交付进度
- POST /v1/orders/{id}/shipments/check      发货预检（批次、出厂检验、超发）
- POST /v1/shipments                        发货出库（账本守卫再查一次）
- POST /v1/shipments/{id}/receipt           客户签收 / 拒收
- GET  /v1/shipments/{id}/export            发货单 Excel（模拟数据不导出）

写操作都以调用人身份 Ledger.append()；网页 / API 上提交本身就是人的确认（L2）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fos.api.deps import SessionDep, require
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, ApprovalRequired, Conflict, Forbidden, Invalid, NotFound
from fos.orders.data import get
from fos.orders.export import shipment_xlsx
from fos.orders.progress import order_board, order_progress
from fos.orders.service import (
    ContractIn,
    LinesIn,
    OrderError,
    ReceiptIn,
    ShipmentIn,
    after_contract,
    contract_request,
    create_lines,
    quote_contract_draft,
    receipt_request,
    shipment_request,
)
from fos.orders.shipping import check_shipment

router = APIRouter(prefix="/v1")
_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}

OrderReader = Annotated[Principal, Depends(require("sales.sales_order:read"))]
ContractWriter = Annotated[Principal, Depends(require("sales.contract:write"))]
OrderWriter = Annotated[Principal, Depends(require("sales.order:write"))]
ShipWriter = Annotated[Principal, Depends(require("warehouse.shipment:write"))]
ShipReader = Annotated[Principal, Depends(require("warehouse.shipment:read"))]
DeliveryWriter = Annotated[Principal, Depends(require("sales.delivery:write"))]


def _tz(request: Request, principal: Principal) -> ZoneInfo:
    return ZoneInfo(request.app.state.registry.get(principal.tenant_id).timezone)


def _today(request: Request, principal: Principal) -> date:
    return datetime.now(_tz(request, principal)).date()


def _ctx(request: Request, principal: Principal):
    st = request.app.state
    return st.ontologies[principal.tenant_id], st.orders[principal.tenant_id]


def _err(e: Exception) -> HTTPException:
    if isinstance(e, OrderError):
        return HTTPException(e.status, detail=str(e))
    return HTTPException(_STATUS[type(e)], detail=str(e))


async def _append(
    request: Request, session, principal: Principal, req: AppendRequest, extra: dict | None = None
):
    """追加一条事件；命中审批规则就提交审批单并返回 202。"""
    st = request.app.state
    try:
        res = await st.ledger.append(session, principal, req)
    except ApprovalRequired as e:
        from fos.api.channel_routes import lookup_for
        from fos.api.inbox_routes import approval_out
        from fos.policy.approvals import send_notices

        lookup = lookup_for(request)
        sub = await st.approvals.submit(session, principal, req, e.decision, source="api", lookup=lookup)
        await session.commit()
        await send_notices(lookup, principal.tenant_id, sub.notices)
        body = {
            "status": "pending_approval",
            "message": sub.message(),
            "approval": approval_out(sub.approval),
        }
        return None, JSONResponse({**body, **(extra or {})}, status_code=202)
    except (Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    return res, None


# ---------------- 合同 ----------------


@router.get("/contracts/draft")
async def contract_draft(
    request: Request,
    session: SessionDep,
    principal: ContractWriter,
    eng_project: Annotated[str, Query(min_length=1, max_length=64)],
) -> dict[str, Any]:
    onto, _ = _ctx(request, principal)
    try:
        return await quote_contract_draft(
            session, onto, principal.tenant_id, eng_project, _today(request, principal)
        )
    except OrderError as e:
        raise _err(e) from e


@router.post("/contracts", status_code=201)
async def create_contract(
    body: ContractIn, request: Request, response: Response, session: SessionDep, principal: ContractWriter
) -> Any:
    onto, _ = _ctx(request, principal)
    try:
        req = contract_request(onto, body, _tz(request, principal))
    except OrderError as e:
        raise _err(e) from e
    res, pending = await _append(request, session, principal, req)
    if pending is not None:
        return pending
    try:
        notes = await after_contract(session, request.app.state.ledger, principal, res, body)
    except (Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    if res.duplicate:
        response.status_code = 200
    return {**res.model_dump(), "notes": notes}


@router.post("/contracts/{contract_id}/orders", status_code=201)
async def create_contract_orders(
    contract_id: str, body: LinesIn, request: Request, session: SessionDep, principal: OrderWriter
) -> dict[str, Any]:
    try:
        return await create_lines(
            session, request.app.state.ledger, principal, contract_id, body, _tz(request, principal)
        )
    except (OrderError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e


# ---------------- 订单进度 ----------------


@router.get("/orders")
async def list_orders(
    request: Request,
    session: SessionDep,
    principal: OrderReader,
    risky: bool = False,
    include_done: bool = False,
    customer: str | None = None,
    contract: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    onto, cfg = _ctx(request, principal)
    today = _today(request, principal)
    rows = await order_board(
        session,
        onto,
        cfg,
        principal,
        today,
        include_done=include_done,
        only_risky=risky,
        customer=customer,
        contract=contract,
    )
    return {"date": today.isoformat(), "count": len(rows), "orders": rows[:limit]}


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str, request: Request, session: SessionDep, principal: OrderReader
) -> dict[str, Any]:
    onto, cfg = _ctx(request, principal)
    o = await get(session, principal.tenant_id, "sales_order", order_id)
    if o is None:
        raise HTTPException(404, detail="订单不存在")
    return await order_progress(session, onto, cfg, principal, o, _today(request, principal))


class ShipCheckIn(BaseModel):
    batches: list[str] = Field(default_factory=list, max_length=50)
    qty: float = Field(gt=0)


@router.post("/orders/{order_id}/shipments/check")
async def precheck_shipment(
    order_id: str, body: ShipCheckIn, request: Request, session: SessionDep, principal: ShipWriter
) -> dict[str, Any]:
    _, cfg = _ctx(request, principal)
    o = await get(session, principal.tenant_id, "sales_order", order_id)
    if o is None:
        raise HTTPException(404, detail="订单不存在")
    payload = {"order": order_id, "batches": body.batches, "qty": body.qty, "unit": o.state.get("unit")}
    return (await check_shipment(session, principal.tenant_id, cfg, payload)).out()


# ---------------- 发货、签收 ----------------


@router.post("/shipments", status_code=201)
async def create_shipment(
    body: ShipmentIn, request: Request, session: SessionDep, principal: ShipWriter
) -> Any:
    _, cfg = _ctx(request, principal)
    try:
        req, check = await shipment_request(session, cfg, principal.tenant_id, body, _tz(request, principal))
    except OrderError as e:
        raise _err(e) from e
    if not check["ok"]:
        raise HTTPException(
            422, detail={"message": "不能发货：" + "；".join(check["problems"]), "check": check}
        )
    res, pending = await _append(request, session, principal, req, {"check": check})
    if pending is not None:
        return pending
    return {**res.model_dump(), "shipment_no": req.payload["shipment_no"], "warnings": check["warnings"]}


@router.post("/shipments/{shipment_id}/receipt")
async def shipment_receipt(
    shipment_id: str, body: ReceiptIn, request: Request, session: SessionDep, principal: DeliveryWriter
) -> Any:
    try:
        req = await receipt_request(session, principal.tenant_id, shipment_id, body, _tz(request, principal))
    except OrderError as e:
        raise _err(e) from e
    res, pending = await _append(request, session, principal, req)
    if pending is not None:
        return pending
    return res.model_dump()


@router.get("/shipments/{shipment_id}/export")
async def export_shipment(
    shipment_id: str, request: Request, session: SessionDep, principal: ShipReader
) -> Response:
    tenant = principal.tenant_id
    _, cfg = _ctx(request, principal)
    sh = await get(session, tenant, "shipment", shipment_id)
    if sh is None:
        raise HTTPException(404, detail="发货记录不存在")
    if sh.data_mode == "mock":
        raise HTTPException(409, detail="模拟数据不能导出正式发货单")
    s = sh.state
    order = await get(session, tenant, "sales_order", s["order"])
    assert order is not None
    customer = await get(session, tenant, "customer", order.state.get("customer"))
    product = await get(session, tenant, "product", order.state.get("product"))
    contract = await get(session, tenant, "contract", order.state.get("contract"))
    check = await check_shipment(session, tenant, cfg, {**s, "qty": 0})  # 只用它整理批次与检验信息
    info = {
        **s,
        "customer": customer.state.get("name") if customer else order.state.get("customer"),
        "delivery_address": contract.state.get("delivery_address") if contract else None,
        "contract_no": contract.state.get("contract_no") if contract else None,
        "order_no": order.state.get("order_no") or order.object_id,
        "product_code": product.state.get("code") if product else "",
        "product_name": product.state.get("name") if product else "",
        "product_spec": product.state.get("spec") if product else "",
        "batches": check.info.get("batches", []),
    }
    factory = request.app.state.registry.get(tenant).name
    data = shipment_xlsx(factory, info)
    fname = f"发货单-{s['shipment_no']}.xlsx"
    from urllib.parse import quote

    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
