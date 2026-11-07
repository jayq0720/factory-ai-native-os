"""订单交付的写操作：工程报价 → 合同草稿、登记合同、拆订单行、发货、签收。

每一步都是人确认后以确认人身份 Ledger.append()（CLAUDE.md 规则 5、6）；这里只负责组 payload 和确定性校验。
金额由代码按报价行计算（规则 3），草稿可以改，入账前人核对。
"""

from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import EngDesign
from fos.engineering.design import money
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, ApprovalRequired, Evidence, Ledger
from fos.ontology import Ontology
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num
from fos.orders.shipping import SHIP_EVENT, check_shipment

CONTRACT_EVENT = "sales.contract_signed"
CONTRACT_FROM_QUOTE = "sales.contract_signed_from_quote"


class OrderError(Exception):
    status = 422


class OrderNotFound(OrderError):
    status = 404


class OrderConflict(OrderError):
    status = 409


def new_id(prefix: str, tz: ZoneInfo) -> str:
    return f"{prefix}-{datetime.now(tz).strftime('%y%m%d')}-{secrets.token_hex(2).upper()}"


# ---------------- 工程报价 → 合同草稿 ----------------


async def quote_contract_draft(
    session: AsyncSession, onto: Ontology, tenant: str, project_id: str, today: date
) -> dict[str, Any]:
    """按已发布的工程报价组合同草稿和订单行（L1，只是草稿）。

    订单行单价 = 报价单价 × (1 − 折扣%)，金额 = 数量 × 单价；运费、施工费、税只计入合同金额，不拆到行。
    """
    if CONTRACT_FROM_QUOTE not in onto.events:
        raise OrderError("当前行业包没有“按工程报价登记合同”")
    project = await get(session, tenant, "eng_project", project_id)
    if project is None:
        raise OrderNotFound("工程项目不存在")
    ps = project.state
    if not ps.get("quote_design"):
        raise OrderError("这个工程项目还没有发布报价")
    design = await session.get(EngDesign, ps["quote_design"])
    if design is None or design.tenant_id != tenant:
        raise OrderNotFound("报价方案不存在")
    quote = design.data.get("quote") or {}
    disc = Decimal(str((quote.get("options") or {}).get("discount_pct") or 0))
    problems: list[str] = []
    notes = ["运费、施工费和税计入合同金额，没有拆到订单行"]
    lines = []
    for ln in quote.get("lines") or []:
        if ln.get("unit_price") is None or ln.get("qty") is None:
            problems.append(f"报价第 {ln.get('line')} 行缺单价或数量")
            continue
        price = money(Decimal(str(ln["unit_price"])) * (1 - disc / 100))
        amount = money(Decimal(str(ln["qty"])) * price)
        lines.append(
            {
                "line": ln.get("line"),
                "product": ln["product_id"],
                "code": ln.get("code"),
                "name": ln.get("name"),
                "qty": ln["qty"],
                "unit": ln.get("unit"),
                "unit_price": float(price),
                "amount": float(amount),
                "formula": f"{ln['unit_price']:g} × (1 − {disc:g}%) = {float(price):g} 元；"
                f"{ln['qty']:g} × {float(price):g} = {float(amount):g} 元",
            }
        )
    if not ps.get("customer"):
        problems.append(
            f"工程项目没有关联客户（客户名称：{ps.get('customer_name') or '未填'}），请先选择或新建客户"
        )
    if ps.get("status") not in ("quoted", "won"):
        notes.append(f"工程项目当前状态是 {ps.get('status')}")
    if ps.get("delivery_period"):
        notes.append(f"项目文件写的交货期：{ps['delivery_period']}，请按合同填写交期日期")
    contract = {
        "contract_no": "",
        "customer": ps.get("customer"),
        "signed_date": today.isoformat(),
        "amount": quote.get("total"),
        "due_date": None,
        "payment_terms": ps.get("payment_terms"),
        "warranty": None,
        "delivery_address": ps.get("location"),
        "status": "active",
        "eng_project": project.object_id,
        "quote_no": ps.get("quote_no"),
    }
    return {
        "event": CONTRACT_FROM_QUOTE,
        "eng_project": {"id": project.object_id, "name": ps.get("name"), "status": ps.get("status")},
        "quote": {
            "quote_no": ps.get("quote_no"),
            "design_id": design.id,
            "total": quote.get("total"),
            "discount_pct": float(disc),
        },
        "contract": contract,
        "lines": lines,
        "problems": problems,
        "notes": notes,
        "data_mode": design.data_mode,
    }


# ---------------- 登记合同 ----------------


class ContractIn(BaseModel):
    contract_no: str = Field(min_length=1, max_length=64)
    customer: str = Field(min_length=1, max_length=64)
    signed_date: date
    amount: float = Field(ge=0)
    due_date: date | None = None
    payment_terms: str | None = None
    warranty: str | None = None
    delivery_address: str | None = None
    note: str | None = None
    eng_project: str | None = Field(default=None, max_length=64)
    quote_no: str | None = Field(default=None, max_length=64)
    evidence: list[Evidence] = Field(min_length=1)  # 合同扫描件
    data_mode: Literal["real", "mock"] = "real"


def contract_request(onto: Ontology, body: ContractIn, tz: ZoneInfo) -> AppendRequest:
    payload: dict[str, Any] = {
        "contract_no": body.contract_no,
        "customer": body.customer,
        "signed_date": body.signed_date.isoformat(),
        "amount": body.amount,
        "status": "active",
    }
    for k in ("payment_terms", "warranty", "delivery_address", "note"):
        if getattr(body, k):
            payload[k] = getattr(body, k)
    if body.due_date:
        payload["due_date"] = body.due_date.isoformat()
    event = CONTRACT_EVENT
    if body.eng_project:
        if CONTRACT_FROM_QUOTE not in onto.events:
            raise OrderError("当前行业包不支持关联工程项目")
        event = CONTRACT_FROM_QUOTE
        payload["eng_project"] = body.eng_project
        if body.quote_no:
            payload["quote_no"] = body.quote_no
    return AppendRequest(
        type=event,
        object_id=new_id("CT", tz),
        payload=payload,
        evidence=body.evidence,
        idempotency_key=f"contract:{body.customer}:{body.contract_no}"[:128],
        data_mode=body.data_mode,
    )


async def after_contract(
    session: AsyncSession, ledger: Ledger, principal: Principal, res: AppendResult, body: ContractIn
) -> list[str]:
    """合同入账后：有权限就把工程项目标为赢单，否则提示。"""
    notes: list[str] = []
    if not body.eng_project or res.duplicate:
        return notes
    project = await get(session, principal.tenant_id, "eng_project", body.eng_project)
    if project is None or project.state.get("status") != "quoted":
        return notes
    if not principal.can("eng.project:write"):
        return ["工程项目状态还是“已报价”，请工程技术改为赢单"]
    await ledger.append(
        session,
        principal,
        AppendRequest(
            type="eng.project_closed",
            object_id=project.object_id,
            payload={"status": "won"},
            causation_id=res.event_id,
            idempotency_key=f"won:{project.object_id}:{res.object_id}",
            data_mode=project.data_mode,
        ),
    )
    notes.append("工程项目已标为赢单")
    return notes


# ---------------- 合同拆订单行 ----------------


class LineIn(BaseModel):
    product: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=16)
    unit_price: float | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)
    due_date: date | None = None
    order_no: str | None = Field(default=None, max_length=64)
    note: str | None = None


class LinesIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1, max_length=50)


async def create_lines(
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    contract_id: str,
    body: LinesIn,
    tz: ZoneInfo,
) -> dict[str, Any]:
    """一次把合同拆成若干订单行，全有或全无（一行失败整批回滚，由调用方的事务负责）。"""
    contract = await get(session, principal.tenant_id, "contract", contract_id)
    if contract is None:
        raise OrderNotFound("合同不存在（如果在审批中，审批通过后再拆订单行）")
    cs = contract.state
    if cs.get("status") == "cancelled":
        raise OrderConflict("合同已取消")
    results, warnings = [], []
    total = Decimal(0)
    for i, ln in enumerate(body.lines, 1):
        amount = ln.amount
        if amount is None and ln.unit_price is not None:
            amount = float(money(Decimal(str(ln.qty)) * Decimal(str(ln.unit_price))))
        total += Decimal(str(amount or 0))
        order_no = ln.order_no or f"{cs['contract_no']}-{i}"
        payload: dict[str, Any] = {
            "customer": cs["customer"],
            "product": ln.product,
            "qty": ln.qty,
            "unit": ln.unit,
            "status": "open",
            "order_no": order_no,
            "contract": contract.object_id,
        }
        due = ln.due_date.isoformat() if ln.due_date else cs.get("due_date")
        if due:
            payload["due_date"] = due
        if amount is not None:
            payload["amount"] = amount
        if ln.unit_price is not None:
            payload["unit_price"] = ln.unit_price
        if ln.note:
            payload["note"] = ln.note
        req = AppendRequest(
            type="sales.order_created",
            object_id=new_id("SO", tz),
            payload=payload,
            evidence=[
                Evidence(
                    kind="manual",
                    ref=f"contract:{contract.object_id}",
                    note=f"按合同 {cs['contract_no']} 拆分",
                )
            ],
            idempotency_key=f"contract:{contract.object_id}:line:{order_no}"[:128],
            data_mode=contract.data_mode,
        )
        try:
            res = await ledger.append(session, principal, req)
        except ApprovalRequired as e:
            raise OrderConflict(f"订单行 {order_no} 命中审批规则（{e}），请单独提交该订单") from e
        results.append({**res.model_dump(), "order_no": order_no})
    if num(cs.get("amount")) and float(total) > num(cs.get("amount")) + 0.005:
        warnings.append(f"订单行金额合计 {float(total):g} 元，超过合同金额 {num(cs.get('amount')):g} 元")
    return {
        "contract_id": contract.object_id,
        "orders": results,
        "lines_total": float(total),
        "warnings": warnings,
    }


# ---------------- 发货、签收 ----------------


class ShipmentIn(BaseModel):
    order: str = Field(min_length=1, max_length=64)
    batches: list[str] = Field(default_factory=list, max_length=50)
    qty: float = Field(gt=0)
    shipped_at: datetime | None = None
    shipment_no: str | None = Field(default=None, max_length=64)
    carrier: str | None = Field(default=None, max_length=64)
    vehicle_no: str | None = Field(default=None, max_length=32)
    note: str | None = None
    evidence: list[Evidence] = Field(min_length=1)  # 发货单 / 装车照片或仓管确认


async def shipment_request(
    session: AsyncSession, cfg: OrdersConfig, tenant: str, body: ShipmentIn, tz: ZoneInfo
) -> tuple[AppendRequest, dict[str, Any]]:
    order = await get(session, tenant, "sales_order", body.order)
    if order is None:
        raise OrderNotFound("订单不存在")
    sid = new_id("SH", tz)
    payload: dict[str, Any] = {
        "shipment_no": body.shipment_no or sid,
        "order": order.object_id,
        "batches": body.batches,
        "qty": body.qty,
        "unit": order.state.get("unit"),
        "shipped_at": (body.shipped_at or datetime.now(tz)).isoformat(),
        "status": "in_transit",
    }
    for k in ("carrier", "vehicle_no", "note"):
        if getattr(body, k):
            payload[k] = getattr(body, k)
    check = await check_shipment(session, tenant, cfg, payload)
    req = AppendRequest(
        type=SHIP_EVENT,
        object_id=sid,
        payload=payload,
        evidence=body.evidence,
        data_mode=order.data_mode,
    )
    return req, check.out()


class ReceiptIn(BaseModel):
    status: Literal["delivered", "refused"]
    receiver: str | None = Field(default=None, max_length=64)
    received_at: datetime | None = None
    received_qty: float | None = Field(default=None, ge=0)
    note: str | None = None
    evidence: list[Evidence] = Field(min_length=1)  # 签收单照片


async def receipt_request(
    session: AsyncSession, tenant: str, shipment_id: str, body: ReceiptIn, tz: ZoneInfo
) -> AppendRequest:
    sh = await get(session, tenant, "shipment", shipment_id)
    if sh is None:
        raise OrderNotFound("发货记录不存在")
    if sh.state.get("status") != "in_transit":
        raise OrderConflict("这批发货已经登记过签收结果")
    if body.received_qty is not None and body.received_qty > num(sh.state.get("qty")) + 1e-9:
        raise OrderError(f"签收数量 {body.received_qty:g} 超过发货数量 {num(sh.state.get('qty')):g}")
    if body.status == "refused" and not body.note:
        raise OrderError("拒收请写明原因")
    payload: dict[str, Any] = {
        "status": body.status,
        "received_at": (body.received_at or datetime.now(tz)).isoformat(),
    }
    if body.receiver:
        payload["receiver"] = body.receiver
    if body.received_qty is not None:
        payload["received_qty"] = body.received_qty
    elif body.status == "delivered":
        payload["received_qty"] = num(sh.state.get("qty"))
    if body.note:
        payload["note"] = body.note
    return AppendRequest(
        type="sales.shipment_received",
        object_id=sh.object_id,
        payload=payload,
        evidence=body.evidence,
        expected_version=sh.version,
        data_mode=sh.data_mode,
    )
