"""财务集成与成本核算核心服务（W23）。

- 应收账款（AR）与应付账款（AP）：挂账、开票、回款/付款（大额付款自动命中审批）、账龄与逾期统计；
- 生产批次成本与订单毛利：从领料、采购单价、窑次能耗与制造费用确定性核算；
- 凭证草稿：从应收/回款/应付/付款确定性生成借贷平衡的复式分录，支持导出至金蝶/用友/畅捷通；
- 客户/供应商对账单与逾期去重提醒。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState, Todo
from fos.finance.config import FinanceConfig
from fos.finance.costing import compute_batch_cost, split_vat
from fos.finance.guards import (
    AP_PAY_EVENT,
    AP_RECORD_EVENT,
    AR_INVOICE_EVENT,
    AR_RECEIVE_EVENT,
    AR_RECORD_EVENT,
    VOUCHER_DRAFT_EVENT,
    VOUCHER_EXPORT_EVENT,
    VOUCHER_POST_EVENT,
)
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.data import get
from fos.orders.service import new_id

STATUS_TEXT = {"open": "未结清", "partial": "部分结清", "paid": "已结清", "void": "已作废"}
VOUCHER_STATUS_TEXT = {"draft": "草稿", "exported": "已导出", "posted": "已过账", "void": "已作废"}
SOURCE_TYPE_TEXT = {
    "ar": "销售应收",
    "ar_receipt": "客户回款",
    "ap": "采购应付",
    "ap_payment": "供应商付款",
    "cost": "成本结转",
    "manual": "手工凭证",
}


class FinanceError(ValueError):
    """财务业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class FinanceNotFound(FinanceError):
    def __init__(self, msg: str):
        super().__init__(msg, 404)


class FinanceConflict(FinanceError):
    def __init__(self, msg: str):
        super().__init__(msg, 409)


async def _scan(session: AsyncSession, tenant: str, obj_type: str, mode: str = "real") -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == tenant,
            ObjectState.object_type == obj_type,
            ObjectState.data_mode == mode,
        )
    )
    return sorted(rows, key=lambda o: o.created_seq)


def _aging_bucket(overdue_days: int) -> str:
    if overdue_days <= 0:
        return "current"
    if overdue_days <= 30:
        return "1_30d"
    if overdue_days <= 60:
        return "31_60d"
    if overdue_days <= 90:
        return "61_90d"
    return "over_90d"


# ---------------- 应收账款（AR） ----------------


class ArCreateIn(BaseModel):
    customer: str | None = Field(default=None, max_length=64)
    order: str | None = Field(default=None, max_length=64)
    contract: str | None = Field(default=None, max_length=64)
    shipment: str | None = Field(default=None, max_length=64)
    amount: FiniteFloat | None = Field(default=None, gt=0)
    due_date: date
    invoice_no: str | None = Field(default=None, max_length=64)
    invoiced_at: date | None = None
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class ArInvoiceIn(BaseModel):
    invoice_no: str = Field(min_length=1, max_length=64)
    invoiced_at: date
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class ArReceiveIn(BaseModel):
    amount: FiniteFloat = Field(gt=0)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _ar_out(o: ObjectState, today: date, cust_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    amt = round(float(s.get("amount") or 0.0), 2)
    rec = round(float(s.get("received_amount") or 0.0), 2)
    bal = round(max(amt - rec, 0.0), 2)
    due_raw = s.get("due_date")
    due = date.fromisoformat(due_raw) if due_raw else None
    st = s.get("status") or "open"
    overdue_days = (today - due).days if (due and bal > 0 and st != "void") else 0
    overdue_days = max(overdue_days, 0)
    cid = s.get("customer") or ""
    return {
        "id": o.object_id,
        "ar_no": s.get("ar_no") or o.object_id,
        "customer": cid,
        "customer_name": cust_names.get(cid, cid),
        "order": s.get("order"),
        "contract": s.get("contract"),
        "shipment": s.get("shipment"),
        "amount": amt,
        "received_amount": rec,
        "balance": bal,
        "due_date": due_raw,
        "overdue_days": overdue_days,
        "aging_bucket": _aging_bucket(overdue_days) if bal > 0 else "settled",
        "invoice_no": s.get("invoice_no"),
        "invoiced_at": s.get("invoiced_at"),
        "status": st,
        "status_text": STATUS_TEXT.get(st, st),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def ar_board(
    session: AsyncSession,
    tenant: str,
    today: date,
    *,
    status: str | None = None,
    customer: str | None = None,
    mode: str = "real",
) -> dict[str, Any]:
    custs = {c.object_id: c.state.get("name") or c.object_id for c in await _scan(session, tenant, "customer", mode)}
    items = [_ar_out(o, today, custs) for o in await _scan(session, tenant, "receivable", mode)]
    if status:
        items = [x for x in items if x["status"] == status]
    if customer:
        items = [x for x in items if x["customer"] == customer]
    items.sort(key=lambda x: (x["status"] == "paid", -x["overdue_days"], x["due_date"] or "9999-12-31"))
    aging = {"current": 0.0, "1_30d": 0.0, "31_60d": 0.0, "61_90d": 0.0, "over_90d": 0.0}
    for x in items:
        if x["balance"] > 0 and x["aging_bucket"] in aging:
            aging[x["aging_bucket"]] = round(aging[x["aging_bucket"]] + x["balance"], 2)
    return {
        "date": today.isoformat(),
        "data_mode": mode,
        "items": items,
        "summary": {
            "count": len(items),
            "total_amount": round(sum(x["amount"] for x in items if x["status"] != "void"), 2),
            "total_received": round(sum(x["received_amount"] for x in items if x["status"] != "void"), 2),
            "total_balance": round(sum(x["balance"] for x in items if x["status"] != "void"), 2),
            "overdue_count": sum(1 for x in items if x["overdue_days"] > 0),
            "overdue_balance": round(sum(x["balance"] for x in items if x["overdue_days"] > 0), 2),
            "aging": aging,
        },
    }


async def record_ar(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: ArCreateIn
) -> dict[str, Any]:
    tid = principal.tenant_id
    customer = body.customer
    amount = body.amount
    contract = body.contract
    if body.order:
        order = await get(session, tid, "sales_order", body.order)
        if order is None:
            raise FinanceNotFound(f"销售订单 {body.order} 不存在")
        customer = customer or order.state.get("customer")
        contract = contract or order.state.get("contract")
        if amount is None and order.state.get("amount") is not None:
            amount = float(order.state["amount"])
    if not customer:
        raise FinanceError("登记应收单必须指定客户（或关联已有销售订单）")
    if amount is None or amount <= 0:
        raise FinanceError("应收金额必须大于 0")
    ar_id = new_id("AR", tz)
    payload: dict[str, Any] = {
        "ar_no": ar_id,
        "customer": customer,
        "amount": round(float(amount), 2),
        "received_amount": 0.0,
        "due_date": body.due_date.isoformat(),
        "status": "open",
    }
    if body.order:
        payload["order"] = body.order
    if contract:
        payload["contract"] = contract
    if body.shipment:
        payload["shipment"] = body.shipment
    if body.invoice_no:
        payload["invoice_no"] = body.invoice_no
    if body.invoiced_at:
        payload["invoiced_at"] = body.invoiced_at.isoformat()
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="manual", ref=f"ar:{ar_id}", note="登记应收单")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=AR_RECORD_EVENT, object_id=ar_id, payload=payload, evidence=ev, data_mode=body.data_mode
        ),
    )
    return {"id": ar_id, "event_id": res.event_id, "seq": res.seq}


async def invoice_ar(
    session: AsyncSession, principal: Principal, ledger: Ledger, ar_id: str, body: ArInvoiceIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "receivable", ar_id)
    if o is None:
        raise FinanceNotFound(f"应收单 {ar_id} 不存在")
    if o.state.get("status") == "void":
        raise FinanceConflict("已作废应收单不能开票")
    payload: dict[str, Any] = {"invoice_no": body.invoice_no, "invoiced_at": body.invoiced_at.isoformat()}
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="document", ref=f"inv:{body.invoice_no}", note="销项发票登记")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=AR_INVOICE_EVENT,
            object_id=ar_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def receive_ar(
    session: AsyncSession, principal: Principal, ledger: Ledger, ar_id: str, body: ArReceiveIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "receivable", ar_id)
    if o is None:
        raise FinanceNotFound(f"应收单 {ar_id} 不存在")
    amt = float(o.state.get("amount") or 0.0)
    prev = float(o.state.get("received_amount") or 0.0)
    delta = round(float(body.amount), 2)
    new_rec = round(prev + delta, 2)
    new_status = "paid" if new_rec >= amt - 0.005 else "partial"
    payload: dict[str, Any] = {
        "received_amount": new_rec,
        "last_receipt_amount": delta,
        "status": new_status,
    }
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="document", ref=f"ar:{ar_id}:recv", note=f"回款 {delta:.2f} 元")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=AR_RECEIVE_EVENT,
            object_id=ar_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 应付账款（AP） ----------------


class ApCreateIn(BaseModel):
    supplier: str | None = Field(default=None, max_length=64)
    purchase_order: str | None = Field(default=None, max_length=64)
    amount: FiniteFloat | None = Field(default=None, gt=0)
    due_date: date
    invoice_no: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class ApPayIn(BaseModel):
    amount: FiniteFloat = Field(gt=0)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _ap_out(o: ObjectState, today: date, sup_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    amt = round(float(s.get("amount") or 0.0), 2)
    paid = round(float(s.get("paid_amount") or 0.0), 2)
    bal = round(max(amt - paid, 0.0), 2)
    due_raw = s.get("due_date")
    due = date.fromisoformat(due_raw) if due_raw else None
    st = s.get("status") or "open"
    overdue_days = max((today - due).days, 0) if (due and bal > 0 and st != "void") else 0
    sid = s.get("supplier") or ""
    return {
        "id": o.object_id,
        "ap_no": s.get("ap_no") or o.object_id,
        "supplier": sid,
        "supplier_name": sup_names.get(sid, sid),
        "purchase_order": s.get("purchase_order"),
        "amount": amt,
        "paid_amount": paid,
        "balance": bal,
        "due_date": due_raw,
        "overdue_days": overdue_days,
        "aging_bucket": _aging_bucket(overdue_days) if bal > 0 else "settled",
        "invoice_no": s.get("invoice_no"),
        "status": st,
        "status_text": STATUS_TEXT.get(st, st),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def ap_board(
    session: AsyncSession,
    tenant: str,
    today: date,
    *,
    status: str | None = None,
    supplier: str | None = None,
    mode: str = "real",
) -> dict[str, Any]:
    sups = {s.object_id: s.state.get("name") or s.object_id for s in await _scan(session, tenant, "supplier", mode)}
    items = [_ap_out(o, today, sups) for o in await _scan(session, tenant, "payable", mode)]
    if status:
        items = [x for x in items if x["status"] == status]
    if supplier:
        items = [x for x in items if x["supplier"] == supplier]
    items.sort(key=lambda x: (x["status"] == "paid", -x["overdue_days"], x["due_date"] or "9999-12-31"))
    aging = {"current": 0.0, "1_30d": 0.0, "31_60d": 0.0, "61_90d": 0.0, "over_90d": 0.0}
    for x in items:
        if x["balance"] > 0 and x["aging_bucket"] in aging:
            aging[x["aging_bucket"]] = round(aging[x["aging_bucket"]] + x["balance"], 2)
    return {
        "date": today.isoformat(),
        "data_mode": mode,
        "items": items,
        "summary": {
            "count": len(items),
            "total_amount": round(sum(x["amount"] for x in items if x["status"] != "void"), 2),
            "total_paid": round(sum(x["paid_amount"] for x in items if x["status"] != "void"), 2),
            "total_balance": round(sum(x["balance"] for x in items if x["status"] != "void"), 2),
            "overdue_count": sum(1 for x in items if x["overdue_days"] > 0),
            "overdue_balance": round(sum(x["balance"] for x in items if x["overdue_days"] > 0), 2),
            "aging": aging,
        },
    }


async def record_ap(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: ApCreateIn
) -> dict[str, Any]:
    tid = principal.tenant_id
    supplier = body.supplier
    amount = body.amount
    if body.purchase_order:
        po = await get(session, tid, "purchase_order", body.purchase_order)
        if po is None:
            raise FinanceNotFound(f"采购单 {body.purchase_order} 不存在")
        supplier = supplier or po.state.get("supplier")
        if amount is None and po.state.get("amount") is not None:
            amount = float(po.state["amount"])
    if not supplier:
        raise FinanceError("登记应付单必须指定供应商（或关联已有采购单）")
    if amount is None or amount <= 0:
        raise FinanceError("应付金额必须大于 0")
    ap_id = new_id("AP", tz)
    payload: dict[str, Any] = {
        "ap_no": ap_id,
        "supplier": supplier,
        "amount": round(float(amount), 2),
        "paid_amount": 0.0,
        "due_date": body.due_date.isoformat(),
        "status": "open",
    }
    if body.purchase_order:
        payload["purchase_order"] = body.purchase_order
    if body.invoice_no:
        payload["invoice_no"] = body.invoice_no
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="manual", ref=f"ap:{ap_id}", note="登记应付单")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=AP_RECORD_EVENT, object_id=ap_id, payload=payload, evidence=ev, data_mode=body.data_mode
        ),
    )
    return {"id": ap_id, "event_id": res.event_id, "seq": res.seq}


async def pay_ap_request(
    session: AsyncSession, principal: Principal, ap_id: str, body: ApPayIn
) -> AppendRequest:
    o = await get(session, principal.tenant_id, "payable", ap_id)
    if o is None:
        raise FinanceNotFound(f"应付单 {ap_id} 不存在")
    amt = float(o.state.get("amount") or 0.0)
    prev = float(o.state.get("paid_amount") or 0.0)
    delta = round(float(body.amount), 2)
    new_paid = round(prev + delta, 2)
    new_status = "paid" if new_paid >= amt - 0.005 else "partial"
    payload: dict[str, Any] = {
        "paid_amount": new_paid,
        "last_payment_amount": delta,
        "status": new_status,
    }
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="document", ref=f"ap:{ap_id}:pay", note=f"付款 {delta:.2f} 元")]
    return AppendRequest(
        type=AP_PAY_EVENT,
        object_id=ap_id,
        payload=payload,
        evidence=ev,
        expected_version=o.version,
        data_mode=o.data_mode,
    )


# ---------------- 批次成本与订单毛利 ----------------


async def _material_price_map(session: AsyncSession, tenant: str, mode: str) -> dict[str, float]:
    """按最新采购单提取各物料的单位采购价（用于批次直接材料成本核算）。"""
    prices: dict[str, float] = {}
    for po in await _scan(session, tenant, "purchase_order", mode):
        s = po.state
        if s.get("status") == "cancelled":
            continue
        mid = s.get("material")
        if not mid:
            continue
        if s.get("unit_price") is not None:
            prices[mid] = float(s["unit_price"])
        elif s.get("amount") and s.get("qty") and float(s["qty"]) > 0:
            prices[mid] = float(s["amount"]) / float(s["qty"])
    return prices


async def batch_cost_report(
    session: AsyncSession, tenant: str, cfg: FinanceConfig, batch_id: str
) -> dict[str, Any]:
    b = await get(session, tenant, "production_batch", batch_id)
    if b is None:
        raise FinanceNotFound(f"生产批次 {batch_id} 不存在")
    mode = b.data_mode
    bs = b.state
    prod = await get(session, tenant, "product", bs.get("product") or "") if bs.get("product") else None
    moves = [
        m.state for m in await _scan(session, tenant, "stock_move", mode) if m.state.get("batch") == batch_id
    ]
    mat_names = {
        m.object_id: m.state.get("name") or m.object_id for m in await _scan(session, tenant, "material", mode)
    }
    mat_prices = await _material_price_map(session, tenant, mode)

    kr_state: dict[str, Any] | None = None
    share_ratio = 1.0
    if bs.get("kiln_run"):
        kr = await get(session, tenant, "kiln_run", bs["kiln_run"])
        if kr is not None:
            kr_state = kr.state
            peers = [
                x
                for x in await _scan(session, tenant, "production_batch", mode)
                if x.state.get("kiln_run") == bs["kiln_run"]
            ]
            if len(peers) > 1:
                my_q = float(bs.get("good_qty") or bs.get("planned_qty") or 0)
                tot_q = sum(float(x.state.get("good_qty") or x.state.get("planned_qty") or 0) for x in peers)
                share_ratio = (my_q / tot_q) if tot_q > 0 else (1.0 / len(peers))

    res = compute_batch_cost(
        cfg,
        batch_id=batch_id,
        batch_state=bs,
        product_state=prod.state if prod else None,
        stock_moves=moves,
        material_names=mat_names,
        material_prices=mat_prices,
        kiln_run_state=kr_state,
        kiln_share_ratio=share_ratio,
    )
    return {**res.out(), "data_mode": mode}


async def order_margin_report(
    session: AsyncSession, tenant: str, cfg: FinanceConfig, order_id: str
) -> dict[str, Any]:
    order = await get(session, tenant, "sales_order", order_id)
    if order is None:
        raise FinanceNotFound(f"销售订单 {order_id} 不存在")
    mode = order.data_mode
    os_ = order.state
    batches = [
        b for b in await _scan(session, tenant, "production_batch", mode) if b.state.get("order") == order_id
    ]
    batch_results = []
    for b in batches:
        # 复用确定性成本计算
        rep = await batch_cost_report(session, tenant, cfg, b.object_id)
        batch_results.append(rep)
    rev = round(float(os_.get("amount") or 0.0), 2)
    total_cost = round(sum(float(x["total_cost"]) for x in batch_results), 2)
    gp = round(rev - total_cost, 2)
    margin_pct = round(100.0 * gp / rev, 2) if rev > 0 else None
    return {
        "order_id": order_id,
        "order_no": os_.get("order_no") or order_id,
        "customer": os_.get("customer"),
        "revenue": rev,
        "total_cost": total_cost,
        "gross_profit": gp,
        "gross_margin_pct": margin_pct,
        "batches": batch_results,
        "data_mode": mode,
    }


# ---------------- 凭证草稿（对接金蝶/用友/畅捷通） ----------------


class VoucherLineIn(BaseModel):
    account_code: str = Field(min_length=1, max_length=32)
    account_name: str = Field(min_length=1, max_length=64)
    debit: FiniteFloat = Field(default=0.0, ge=0)
    credit: FiniteFloat = Field(default=0.0, ge=0)
    memo: str = Field(default="", max_length=200)


class VoucherDraftIn(BaseModel):
    voucher_date: date
    summary: str = Field(min_length=1, max_length=200)
    source_type: Literal["ar", "ar_receipt", "ap", "ap_payment", "cost", "manual"] = "manual"
    source_id: str | None = Field(default=None, max_length=64)
    lines: list[VoucherLineIn] = Field(min_length=2, max_length=30)
    external_system: Literal["generic", "kingdee", "yonyou", "chanjet"] = "generic"
    note: str | None = Field(default=None, max_length=500)
    data_mode: Literal["real", "mock"] = "real"


class VoucherFromSourceIn(BaseModel):
    source_type: Literal["ar", "ar_receipt", "ap", "ap_payment"]
    source_id: str = Field(min_length=1, max_length=64)
    voucher_date: date
    amount: FiniteFloat | None = Field(default=None, gt=0)
    external_system: Literal["generic", "kingdee", "yonyou", "chanjet"] = "generic"
    note: str | None = Field(default=None, max_length=500)


class VoucherPostIn(BaseModel):
    external_system: Literal["generic", "kingdee", "yonyou", "chanjet"] = "kingdee"
    external_ref: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _voucher_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "draft"
    stype = s.get("source_type") or "manual"
    return {
        "id": o.object_id,
        "voucher_no": s.get("voucher_no") or o.object_id,
        "voucher_date": s.get("voucher_date"),
        "summary": s.get("summary"),
        "source_type": stype,
        "source_type_text": SOURCE_TYPE_TEXT.get(stype, stype),
        "source_id": s.get("source_id"),
        "lines": s.get("lines") or [],
        "total_debit": round(float(s.get("total_debit") or 0.0), 2),
        "total_credit": round(float(s.get("total_credit") or 0.0), 2),
        "status": st,
        "status_text": VOUCHER_STATUS_TEXT.get(st, st),
        "external_system": s.get("external_system") or "generic",
        "external_ref": s.get("external_ref"),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def vouchers_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> list[dict[str, Any]]:
    rows = [_voucher_out(o) for o in await _scan(session, tenant, "voucher_draft", mode)]
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=lambda r: (r["voucher_date"] or "", r["id"]), reverse=True)
    return rows


async def draft_voucher_manual(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: VoucherDraftIn
) -> dict[str, Any]:
    vid = new_id("PZ", tz)
    lines = [x.model_dump() for x in body.lines]
    td = round(sum(float(x["debit"]) for x in lines), 2)
    tc = round(sum(float(x["credit"]) for x in lines), 2)
    payload: dict[str, Any] = {
        "voucher_no": vid,
        "voucher_date": body.voucher_date.isoformat(),
        "summary": body.summary,
        "source_type": body.source_type,
        "lines": lines,
        "total_debit": td,
        "total_credit": tc,
        "status": "draft",
        "external_system": body.external_system,
    }
    if body.source_id:
        payload["source_id"] = body.source_id
    if body.note:
        payload["note"] = body.note
    ev = [Evidence(kind="calculation", ref=f"voucher:{vid}", note=body.summary[:200])]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=VOUCHER_DRAFT_EVENT, object_id=vid, payload=payload, evidence=ev, data_mode=body.data_mode
        ),
    )
    return {"id": vid, "event_id": res.event_id, "seq": res.seq, "total_debit": td, "total_credit": tc}


async def draft_voucher_from_source(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    cfg: FinanceConfig,
    tz: ZoneInfo,
    body: VoucherFromSourceIn,
) -> dict[str, Any]:
    """根据应收/回款/应付/付款单据确定性生成借贷平衡凭证草稿（含价税分离）。"""
    tid = principal.tenant_id
    acc = cfg.accounts
    if body.source_type in ("ar", "ar_receipt"):
        src = await get(session, tid, "receivable", body.source_id)
        if src is None:
            raise FinanceNotFound(f"应收单 {body.source_id} 不存在")
        s = src.state
        gross = round(
            float(
                body.amount
                if body.amount is not None
                else (s.get("last_receipt_amount") or s.get("received_amount") or s.get("amount") or 0)
                if body.source_type == "ar_receipt"
                else (s.get("amount") or 0)
            ),
            2,
        )
        if gross <= 0:
            raise FinanceError("凭证金额必须大于 0")
        if body.source_type == "ar":
            net, tax = split_vat(gross, cfg.vat_rate)
            summary = f"确认销售收入（{body.source_id}）"
            lines = [
                VoucherLineIn(account_code=acc.ar.code, account_name=acc.ar.name, debit=gross, memo=summary),
                VoucherLineIn(
                    account_code=acc.revenue.code, account_name=acc.revenue.name, credit=net, memo=summary
                ),
            ]
            if tax > 0:
                lines.append(
                    VoucherLineIn(
                        account_code=acc.tax_out.code, account_name=acc.tax_out.name, credit=tax, memo=summary
                    )
                )
        else:
            summary = f"收到客户回款（{body.source_id}）"
            lines = [
                VoucherLineIn(account_code=acc.bank.code, account_name=acc.bank.name, debit=gross, memo=summary),
                VoucherLineIn(account_code=acc.ar.code, account_name=acc.ar.name, credit=gross, memo=summary),
            ]
        mode = src.data_mode
    else:
        src = await get(session, tid, "payable", body.source_id)
        if src is None:
            raise FinanceNotFound(f"应付单 {body.source_id} 不存在")
        s = src.state
        gross = round(
            float(
                body.amount
                if body.amount is not None
                else (s.get("last_payment_amount") or s.get("paid_amount") or s.get("amount") or 0)
                if body.source_type == "ap_payment"
                else (s.get("amount") or 0)
            ),
            2,
        )
        if gross <= 0:
            raise FinanceError("凭证金额必须大于 0")
        if body.source_type == "ap":
            net, tax = split_vat(gross, cfg.vat_rate)
            summary = f"采购原料入库暂估/挂账（{body.source_id}）"
            lines = [
                VoucherLineIn(
                    account_code=acc.raw_material.code,
                    account_name=acc.raw_material.name,
                    debit=net,
                    memo=summary,
                ),
            ]
            if tax > 0:
                lines.append(
                    VoucherLineIn(
                        account_code=acc.tax_in.code, account_name=acc.tax_in.name, debit=tax, memo=summary
                    )
                )
            lines.append(
                VoucherLineIn(account_code=acc.ap.code, account_name=acc.ap.name, credit=gross, memo=summary)
            )
        else:
            summary = f"支付供应商货款（{body.source_id}）"
            lines = [
                VoucherLineIn(account_code=acc.ap.code, account_name=acc.ap.name, debit=gross, memo=summary),
                VoucherLineIn(
                    account_code=acc.bank.code, account_name=acc.bank.name, credit=gross, memo=summary
                ),
            ]
        mode = src.data_mode

    return await draft_voucher_manual(
        session,
        principal,
        ledger,
        tz,
        VoucherDraftIn(
            voucher_date=body.voucher_date,
            summary=summary,
            source_type=body.source_type,
            source_id=body.source_id,
            lines=lines,
            external_system=body.external_system,
            note=body.note,
            data_mode=mode,
        ),
    )


async def post_voucher(
    session: AsyncSession, principal: Principal, ledger: Ledger, voucher_id: str, body: VoucherPostIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "voucher_draft", voucher_id)
    if o is None:
        raise FinanceNotFound(f"凭证草稿 {voucher_id} 不存在")
    payload: dict[str, Any] = {
        "status": "posted",
        "external_system": body.external_system,
        "external_ref": body.external_ref,
    }
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [
        Evidence(kind="document", ref=f"{body.external_system}:{body.external_ref}", note="外部财务软件过账")
    ]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=VOUCHER_POST_EVENT,
            object_id=voucher_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def mark_vouchers_exported(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    voucher_ids: list[str],
    *,
    external_system: str,
) -> None:
    for vid in voucher_ids:
        o = await get(session, principal.tenant_id, "voucher_draft", vid)
        if o is None or o.state.get("status") != "draft":
            continue
        await ledger.append(
            session,
            principal,
            AppendRequest(
                type=VOUCHER_EXPORT_EVENT,
                object_id=vid,
                payload={"status": "exported", "external_system": external_system},
                expected_version=o.version,
                data_mode=o.data_mode,
            ),
        )


# ---------------- 对账单与逾期提醒 ----------------


async def reconciliation_data(
    session: AsyncSession,
    tenant: str,
    today: date,
    *,
    party_type: Literal["customer", "supplier"],
    party_id: str,
) -> dict[str, Any]:
    """生成正式客户/供应商对账单数据（仅真实数据，禁止模拟数据）。"""
    party = await get(session, tenant, party_type, party_id)
    if party is None:
        raise FinanceNotFound(f"{'客户' if party_type == 'customer' else '供应商'} {party_id} 不存在")
    if party.data_mode == "mock":
        raise FinanceConflict("模拟数据不能导出正式对账单")
    if party_type == "customer":
        board = await ar_board(session, tenant, today, customer=party_id, mode="real")
        lines = board["items"]
        total_amt = board["summary"]["total_amount"]
        total_settled = board["summary"]["total_received"]
        total_bal = board["summary"]["total_balance"]
    else:
        board = await ap_board(session, tenant, today, supplier=party_id, mode="real")
        lines = board["items"]
        total_amt = board["summary"]["total_amount"]
        total_settled = board["summary"]["total_paid"]
        total_bal = board["summary"]["total_balance"]
    return {
        "date": today.isoformat(),
        "party_type": party_type,
        "party_id": party_id,
        "party_name": party.state.get("name") or party_id,
        "total_amount": total_amt,
        "total_settled": total_settled,
        "total_balance": total_bal,
        "items": lines,
        "data_mode": "real",
    }


async def remind_overdue_ar_ap(
    session: AsyncSession, tenant: str, cfg: FinanceConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    """为已逾期应收/应付生成去重待办（同一单据仅保留一条未关闭提醒）。"""
    ar = await ar_board(session, tenant, today, mode=mode)
    ap = await ap_board(session, tenant, today, mode=mode)
    existing = {
        (t.object_type, t.object_id)
        for t in (
            await session.scalars(
                select(Todo).where(
                    Todo.tenant_id == tenant,
                    Todo.object_type.in_(["receivable", "payable"]),
                    Todo.status == "open",
                    Todo.data_mode == mode,
                )
            )
        ).all()
    }
    created: list[str] = []
    role = cfg.reminders.notify_roles[0] if cfg.reminders.notify_roles else "finance"
    for kind, obj_type, rows in (("应收逾期", "receivable", ar["items"]), ("应付逾期", "payable", ap["items"])):
        for it in rows:
            if it["overdue_days"] <= 0 or it["balance"] <= 0:
                continue
            if (obj_type, it["id"]) in existing:
                continue
            due_dt = (
                datetime.combine(date.fromisoformat(it["due_date"]), time(17, 0), tzinfo=UTC)
                if it.get("due_date")
                else None
            )
            name = it.get("customer_name") or it.get("supplier_name") or ""
            session.add(
                Todo(
                    tenant_id=tenant,
                    title=f"【{kind}】{it['id']} {name}（余 ¥{it['balance']:,.2f}）"[:80],
                    body=f"已逾期 {it['overdue_days']} 天，到期日 {it['due_date']}，未结余额 {it['balance']:.2f} 元。",
                    assignee_role=role,
                    object_type=obj_type,
                    object_id=it["id"],
                    due_at=due_dt,
                    source="system:finance",
                    data_mode=mode,
                    created_by="system",
                )
            )
            created.append(it["id"])
    await session.flush()
    return {"created": created, "count": len(created)}
