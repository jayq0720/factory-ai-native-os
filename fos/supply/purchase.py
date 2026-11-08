"""采购：缺料 / 低于安全库存 → 请购建议 → 请购单 → 比价 → 采购单 → 按单到货。

- 请购建议（L1，只是草稿）：按当前排窑计划的原料缺口和安全库存算，扣掉还没下单的请购单，避免重复请购；
- 比价：有效期内、单位一致、每家供应商最新的一张报价，按“能按需要日期到 → 单价最低”推荐；
- 采购单金额 = 数量 × 单价，由代码算（规则 3）；金额审批阈值在 policy.yaml（规则 6）；
- 请购单被采购单引用即为“已下单”，状态从引用关系算，不另记事件。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Approval, ObjectState
from fos.engineering.design import money
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num, referrers
from fos.orders.service import new_id
from fos.supply.config import SupplyConfig
from fos.supply.guards import RECEIVE_EVENT, check_receipt, po_lots
from fos.supply.stock import EPS, _scan, material_stock
from fos.supply.suppliers import score_suppliers

PO_EVENT = "purchase.order_created"
OPEN_PO = ("open",)


class SupplyError(Exception):
    status = 422


class SupplyNotFound(SupplyError):
    status = 404


class SupplyConflict(SupplyError):
    status = 409


def _up(x: float) -> float:
    """数量向上取到 0.001。"""
    return math.ceil(round(x, 9) * 1000) / 1000


# ---------------- 采购单到货进度 ----------------


async def po_row(
    session: AsyncSession, tenant: str, ocfg: OrdersConfig, po: ObjectState, today: date
) -> dict[str, Any]:
    ps = po.state
    lots = await po_lots(session, tenant, po)
    received = sum(
        num(lot.state.get("qty")) for lot in lots if ocfg.same_unit(lot.state.get("unit"), ps.get("unit"))
    )
    remaining = max(num(ps.get("qty")) - received, 0.0)
    open_ = ps.get("status") in OPEN_PO
    due = ps.get("due_date")
    return {
        "id": po.object_id,
        "po_no": ps.get("po_no") or po.object_id,
        "supplier": ps.get("supplier"),
        "material": ps.get("material"),
        "qty": num(ps.get("qty")),
        "unit": ps.get("unit"),
        "unit_price": ps.get("unit_price"),
        "amount": ps.get("amount"),
        "due_date": due,
        "status": ps.get("status"),
        "requisitions": ps.get("requisitions") or [],
        "received": round(received, 3),
        "remaining": round(remaining, 3) if open_ else 0.0,
        "lots": [lot.object_id for lot in lots],
        "overdue": bool(open_ and due and due < today.isoformat() and remaining > EPS),
        "data_mode": po.data_mode,
    }


async def po_rows(
    session: AsyncSession,
    tenant: str,
    ocfg: OrdersConfig,
    today: date,
    *,
    mode: str = "real",
    status: str | None = None,
    material: str | None = None,
) -> list[dict[str, Any]]:
    out = []
    for po in await _scan(session, tenant, "purchase_order"):
        if po.data_mode != mode or (status and po.state.get("status") != status):
            continue
        if material and po.state.get("material") != material:
            continue
        out.append(await po_row(session, tenant, ocfg, po, today))
    return out


# ---------------- 请购单 ----------------


async def _pending_po_reqs(session: AsyncSession, tenant: str) -> dict[str, str]:
    """审批中的采购单引用的请购单 → 审批单 id。"""
    q = select(Approval).where(Approval.tenant_id == tenant, Approval.status == "pending")
    out: dict[str, str] = {}
    for ap in await session.scalars(q):
        req = ap.request or {}
        if req.get("type") == PO_EVENT:
            for rid in (req.get("payload") or {}).get("requisitions") or []:
                out[rid] = ap.id
    return out


async def requisition_rows(
    session: AsyncSession, tenant: str, *, mode: str = "real", only_open: bool = False
) -> list[dict[str, Any]]:
    reqs = [r for r in await _scan(session, tenant, "purchase_requisition") if r.data_mode == mode]
    ids = [r.object_id for r in reqs]
    ordered: dict[str, str] = {}
    for po in await referrers(session, tenant, "purchase_order", "requisitions", "purchase_requisition", ids):
        if po.state.get("status") != "cancelled":
            for rid in po.state.get("requisitions") or []:
                ordered.setdefault(rid, po.object_id)
    pending = await _pending_po_reqs(session, tenant)
    out = []
    for r in reqs:
        s = r.state
        if s.get("status") == "cancelled":
            stage, text = "cancelled", "已取消"
        elif r.object_id in ordered:
            stage, text = "ordered", f"已下单 {ordered[r.object_id]}"
        elif r.object_id in pending:
            stage, text = "approving", "采购单审批中"
        else:
            stage, text = "open", "待下单"
        if only_open and stage not in ("open", "approving"):
            continue
        out.append(
            {
                "id": r.object_id,
                "req_no": s.get("req_no"),
                "material": s.get("material"),
                "qty": num(s.get("qty")),
                "unit": s.get("unit"),
                "need_date": s.get("need_date"),
                "source": s.get("source"),
                "reason": s.get("reason"),
                "suggested_supplier": s.get("suggested_supplier"),
                "stage": stage,
                "stage_text": text,
                "purchase_order": ordered.get(r.object_id),
                "approval": pending.get(r.object_id),
                "data_mode": r.data_mode,
            }
        )
    return out


class RequisitionLine(BaseModel):
    material: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0)
    need_date: date | None = None
    source: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=500)
    suggested_supplier: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)


class RequisitionsIn(BaseModel):
    lines: list[RequisitionLine] = Field(min_length=1, max_length=50)
    data_mode: Literal["real", "mock"] = "real"


async def requisition_requests(
    session: AsyncSession, tenant: str, body: RequisitionsIn, tz: ZoneInfo
) -> list[AppendRequest]:
    out = []
    for ln in body.lines:
        m = await get(session, tenant, "material", ln.material)
        if m is None:
            raise SupplyNotFound(f"物料 {ln.material} 不存在")
        rid = new_id("PR", tz)
        payload: dict[str, Any] = {
            "req_no": rid,
            "material": m.object_id,
            "qty": ln.qty,
            "unit": m.state.get("unit"),
            "status": "open",
        }
        if ln.need_date:
            payload["need_date"] = ln.need_date.isoformat()
        for k in ("source", "reason", "suggested_supplier", "note"):
            if getattr(ln, k):
                payload[k] = getattr(ln, k)
        out.append(
            AppendRequest(
                type="purchase.requisition_raised",
                object_id=rid,
                payload=payload,
                evidence=[Evidence(kind="manual", ref=f"requisition:{rid}", note="采购员确认请购")],
                data_mode=body.data_mode,
            )
        )
    return out


# ---------------- 比价 ----------------


async def compare_quotes(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    *,
    material_id: str,
    qty: float | None,
    need_date: date | None,
    today: date,
    tz: ZoneInfo,
    mode: str = "real",
) -> dict[str, Any]:
    m = await get(session, tenant, "material", material_id)
    if m is None:
        raise SupplyNotFound(f"物料 {material_id} 不存在")
    unit = m.state.get("unit")
    latest: dict[str, ObjectState] = {}
    expired = other_unit = 0
    for q in await _scan(session, tenant, "supplier_quote"):
        s = q.state
        if q.data_mode != mode or s.get("material") != m.object_id:
            continue
        if not ocfg.same_unit(s.get("unit"), unit):
            other_unit += 1
            continue
        if s.get("valid_until") and s["valid_until"] < today.isoformat():
            expired += 1
            continue
        cur = latest.get(s.get("supplier"))
        if cur is None or (str(s.get("quoted_at")), q.created_seq) > (
            str(cur.state.get("quoted_at")),
            cur.created_seq,
        ):
            latest[s.get("supplier")] = q
    scores = {
        r["supplier"]: r
        for r in await score_suppliers(session, tenant, cfg, ocfg, today=today, tz=tz, mode=mode)
    }
    rows = []
    for sup_id, q in latest.items():
        s = q.state
        sup = await get(session, tenant, "supplier", sup_id)
        price = num(s.get("unit_price"))
        lead = s.get("lead_days")
        eta = today + timedelta(days=math.ceil(num(lead))) if lead is not None else None
        meets = None if eta is None or need_date is None else eta <= need_date
        rows.append(
            {
                "quote": q.object_id,
                "quote_no": s.get("quote_no"),
                "supplier": sup_id,
                "supplier_name": sup.state.get("name") if sup else sup_id,
                "unit_price": price,
                "unit": s.get("unit"),
                "amount": float(money(Decimal(str(qty)) * Decimal(str(price)))) if qty else None,
                "lead_days": lead,
                "eta": eta.isoformat() if eta else None,
                "meets_need_date": meets,
                "min_qty": s.get("min_qty"),
                "below_min_qty": bool(qty and s.get("min_qty") and qty < num(s.get("min_qty")) - EPS),
                "quoted_at": s.get("quoted_at"),
                "valid_until": s.get("valid_until"),
                "score": (scores.get(sup_id) or {}).get("score"),
            }
        )
    rows.sort(key=lambda r: (r["unit_price"], r["eta"] or "9999"))
    rec, why = None, None
    if rows:
        if need_date is None:
            rec, why = rows[0], "没有需要日期，取单价最低"
        elif ok := [r for r in rows if r["meets_need_date"]]:
            rec, why = ok[0], f"能在 {need_date.isoformat()} 前到货的报价里单价最低"
        elif dated := [r for r in rows if r["eta"]]:
            rec = min(dated, key=lambda r: (r["eta"], r["unit_price"]))
            why = f"没有报价能在 {need_date.isoformat()} 前到货，取最快到货"
        else:
            rec, why = rows[0], "报价都没写交货周期，取单价最低"
    notes = []
    if expired:
        notes.append(f"{expired} 张报价已过有效期，没有参与比价")
    if other_unit:
        notes.append(f"{other_unit} 张报价单位和物料单位 {unit} 不同，没有参与比价")
    if not rows:
        notes.append("没有有效报价：可以拍照登记供应商报价单后再比价")
    return {
        "material": m.object_id,
        "name": m.state.get("name"),
        "unit": unit,
        "qty": qty,
        "need_date": need_date.isoformat() if need_date else None,
        "quotes": rows,
        "recommended": rec["quote"] if rec else None,
        "recommended_supplier": rec["supplier"] if rec else None,
        "reason": why,
        "notes": notes,
    }


# ---------------- 请购建议 ----------------


async def _plan_materials(session, tenant, planning, ocfg, tz, now, mode) -> tuple[dict | None, str | None]:
    if planning is None:
        return None, None
    from fos.planning.demand import load_context
    from fos.planning.service import current_plan

    ctx = await load_context(session, tenant=tenant, cfg=planning, ocfg=ocfg, tz=tz, now=now, mode=mode)
    p = await current_plan(session, ctx)
    if p is None:
        return None, None
    return {r["material"]: r for r in p["materials"]["materials"]}, p["plan"]["plan_no"]


async def suggestions(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    *,
    planning,
    tz: ZoneInfo,
    now: datetime,
    mode: str = "real",
) -> dict[str, Any]:
    """请购建议：缺口 = max(排窑计划原料缺口, 补到安全库存的量) − 还没下单的请购。只是草稿，不入账。"""
    today = now.astimezone(tz).date()
    plan_rows, plan_no = await _plan_materials(session, tenant, planning, ocfg, tz, now, mode)
    stock = {
        r["material"]: r for r in (await material_stock(session, tenant, cfg, ocfg, mode=mode))["materials"]
    }
    on_order: dict[str, float] = {}
    for po in await po_rows(session, tenant, ocfg, today, mode=mode, status="open"):
        on_order[po["material"]] = on_order.get(po["material"], 0.0) + po["remaining"]
    reqd: dict[str, float] = {}
    for r in await requisition_rows(session, tenant, mode=mode, only_open=True):
        reqd[r["material"]] = reqd.get(r["material"], 0.0) + r["qty"]
    out = []
    for mid, st in stock.items():
        pr = (plan_rows or {}).get(mid)
        m = await get(session, tenant, "material", mid)
        assert m is not None
        safety = num(m.state.get("safety_stock"))
        plan_gap = pr["shortage"] if pr else 0.0
        need = pr["need"] if pr else 0.0
        supply = st["usable"] + st["incoming"] + on_order.get(mid, 0.0)
        safety_gap = max(safety - (supply - need), 0.0) if safety else 0.0
        gross = max(plan_gap, safety_gap)
        net = gross - reqd.get(mid, 0.0)
        if net <= EPS:
            continue
        qty = _up(net)
        notes = []
        moq = num(m.state.get("min_order_qty"))
        if cfg.requisition.round_to_min_qty and moq and qty < moq:
            notes.append(f"不足最小起订量，按 {moq:g} 请购")
            qty = moq
        reasons = []
        if plan_gap > EPS:
            reasons.append(f"排窑计划 {plan_no} 缺 {plan_gap:g} {st['unit']}（{pr['short_from']} 起要用）")
        if safety_gap > EPS:
            reasons.append(
                f"补到安全库存 {safety:g}：可用 {st['usable']:g} + 待检 {st['incoming']:g} + 在途 "
                f"{on_order.get(mid, 0.0):g} − 计划需求 {need:g} = {supply - need:g}"
            )
        if reqd.get(mid):
            reasons.append(f"已有未下单请购 {reqd[mid]:g}，已扣除")
        need_date = (
            date.fromisoformat(pr["short_from"]) if pr and plan_gap > EPS and pr["short_from"] else None
        )
        cmp = await compare_quotes(
            session,
            tenant,
            cfg,
            ocfg,
            material_id=mid,
            qty=qty,
            need_date=need_date,
            today=today,
            tz=tz,
            mode=mode,
        )
        supplier = cmp["recommended_supplier"] or m.state.get("default_supplier")
        out.append(
            {
                "material": mid,
                "name": st["name"],
                "unit": st["unit"],
                "qty": qty,
                "need_date": need_date.isoformat() if need_date else None,
                "source": plan_no if plan_gap > EPS else "安全库存",
                "reason": "；".join(reasons),
                "suggested_supplier": supplier,
                "quote": cmp["recommended"],
                "quote_reason": cmp["reason"] if cmp["recommended"] else "没有有效报价，按常用供应商",
                "notes": notes + cmp["notes"],
                "basis": {
                    "plan_shortage": plan_gap,
                    "plan_need": need,
                    "usable": st["usable"],
                    "incoming": st["incoming"],
                    "on_order": round(on_order.get(mid, 0.0), 3),
                    "safety_stock": safety or None,
                    "requisitioned": round(reqd.get(mid, 0.0), 3),
                },
            }
        )
    out.sort(key=lambda r: (r["need_date"] or "9999", r["name"]))
    return {
        "plan_no": plan_no,
        "suggestions": out,
        "note": "只是建议：采购员核对后点“生成请购单”才入账；数量由代码按缺口和安全库存算",
    }


# ---------------- 采购单 ----------------


class PurchaseOrderIn(BaseModel):
    supplier: str = Field(min_length=1, max_length=64)
    material: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    due_date: date | None = None
    requisitions: list[str] = Field(default_factory=list, max_length=50)
    quote: str | None = Field(default=None, max_length=64)
    po_no: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list)
    data_mode: Literal["real", "mock"] = "real"


async def po_request(
    session: AsyncSession, tenant: str, body: PurchaseOrderIn, today: date, tz: ZoneInfo
) -> tuple[AppendRequest, dict[str, Any]]:
    m = await get(session, tenant, "material", body.material)
    if m is None:
        raise SupplyNotFound(f"物料 {body.material} 不存在")
    if await get(session, tenant, "supplier", body.supplier) is None:
        raise SupplyNotFound(f"供应商 {body.supplier} 不存在")
    warnings: list[str] = []
    if body.requisitions:
        rows = {r["id"]: r for r in await requisition_rows(session, tenant, mode=body.data_mode)}
        total = 0.0
        for rid in body.requisitions:
            r = rows.get(rid)
            if r is None:
                raise SupplyNotFound(f"请购单 {rid} 不存在")
            if r["material"] != m.object_id:
                raise SupplyError(f"请购单 {rid} 不是这种物料")
            if r["stage"] != "open":
                raise SupplyConflict(f"请购单 {rid} {r['stage_text']}，不能再下单")
            total += r["qty"]
        if body.qty < total - EPS:
            warnings.append(f"采购数量 {body.qty:g} 少于请购合计 {total:g}")
    due = body.due_date
    if body.quote:
        q = await get(session, tenant, "supplier_quote", body.quote)
        if q is None:
            raise SupplyNotFound(f"报价 {body.quote} 不存在")
        qs = q.state
        if qs.get("supplier") != body.supplier or qs.get("material") != m.object_id:
            raise SupplyError("依据的报价不是这家供应商、这种物料的")
        if abs(num(qs.get("unit_price")) - body.unit_price) > 0.005:
            warnings.append(f"单价 {body.unit_price:g} 和报价单价 {num(qs.get('unit_price')):g} 不同")
        if qs.get("valid_until") and qs["valid_until"] < today.isoformat():
            warnings.append(f"报价已过有效期（{qs['valid_until']}）")
        if due is None and qs.get("lead_days") is not None:
            due = today + timedelta(days=math.ceil(num(qs["lead_days"])))
            warnings.append(f"交期按报价交货周期 {num(qs['lead_days']):g} 天算：{due.isoformat()}")
    amount = money(Decimal(str(body.qty)) * Decimal(str(body.unit_price)))
    oid = new_id("PO", tz)
    payload: dict[str, Any] = {
        "po_no": body.po_no or oid,
        "supplier": body.supplier,
        "material": m.object_id,
        "qty": body.qty,
        "unit": m.state.get("unit"),
        "unit_price": body.unit_price,
        "amount": float(amount),
        "status": "open",
    }
    if due:
        payload["due_date"] = due.isoformat()
    if body.requisitions:
        payload["requisitions"] = body.requisitions
    if body.quote:
        payload["quote"] = body.quote
    if body.note:
        payload["note"] = body.note
    req = AppendRequest(
        type=PO_EVENT,
        object_id=oid,
        payload=payload,
        evidence=body.evidence
        or [Evidence(kind="calculation", ref=f"po:{oid}", note=f"金额 = {body.qty:g} × {body.unit_price:g}")],
        data_mode=body.data_mode,
    )
    check = {
        "amount": float(amount),
        "formula": f"{body.qty:g} {m.state.get('unit')} × {body.unit_price:g} 元 = {float(amount):g} 元",
        "warnings": warnings,
    }
    return req, check


# ---------------- 按采购单到货 ----------------


class ReceiptIn(BaseModel):
    qty: float = Field(gt=0)
    supplier_lot_no: str | None = Field(default=None, max_length=64)
    received_at: datetime | None = None
    location: str | None = Field(default=None, max_length=64)
    evidence: list[Evidence] = Field(min_length=1)  # 送货单照片


async def receipt_request(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    po_id: str,
    body: ReceiptIn,
    tz: ZoneInfo,
) -> tuple[AppendRequest, dict[str, Any]]:
    po = await get(session, tenant, "purchase_order", po_id)
    if po is None:
        raise SupplyNotFound("采购单不存在")
    ps = po.state
    lid = new_id("LT", tz)
    payload: dict[str, Any] = {
        "material": ps.get("material"),
        "supplier": ps.get("supplier"),
        "qty": body.qty,
        "unit": ps.get("unit"),
        "received_at": (body.received_at or datetime.now(tz)).isoformat(),
        "purchase_order": po.object_id,
        "status": "pending_inspection",
    }
    if body.supplier_lot_no:
        payload["supplier_lot_no"] = body.supplier_lot_no
    if body.location:
        payload["location"] = body.location
    problems, info = await check_receipt(session, tenant, cfg, ocfg, payload)
    if problems:
        raise SupplyConflict("；".join(problems))
    req = AppendRequest(
        type=RECEIVE_EVENT, object_id=lid, payload=payload, evidence=body.evidence, data_mode=po.data_mode
    )
    return req, info


async def after_receipt(
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    ocfg: OrdersConfig,
    po_id: str,
    res: AppendResult,
    today: date,
) -> list[str]:
    """到齐了：有采购单权限就把采购单标为已到货，否则提示采购。"""
    po = await get(session, principal.tenant_id, "purchase_order", po_id)
    assert po is not None
    row = await po_row(session, principal.tenant_id, ocfg, po, today)
    if row["status"] != "open" or row["received"] < row["qty"] - EPS:
        return [f"采购单 {row['po_no']} 已到 {row['received']:g} / {row['qty']:g} {row['unit']}"]
    if not principal.can("purchase.order:write"):
        return [f"采购单 {row['po_no']} 已到齐，请采购把采购单标为已到货"]
    await ledger.append(
        session,
        principal,
        AppendRequest(
            type="purchase.order_status_changed",
            object_id=po.object_id,
            payload={"status": "received"},
            causation_id=res.event_id,
            idempotency_key=f"po_received:{po.object_id}",
            expected_version=po.version,
            data_mode=po.data_mode,
        ),
    )
    return [f"采购单 {row['po_no']} 已到齐，已标为已到货"]
