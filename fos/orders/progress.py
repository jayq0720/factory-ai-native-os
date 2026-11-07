"""订单交付进度与交期风险：全部从账本投影确定性算出，不调用模型（CLAUDE.md 规则 3）。

数量只在单位一致时合计（单位同义词见 orders.yaml 的 unit_aliases），单位不同的批次单列并提示，不做换算。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num, referrers

CLOSED = {"closed", "cancelled"}
PRODUCED = {"completed", "in_stock", "shipped"}
STOCKED = {"in_stock", "shipped"}
STAGE_TEXT = {
    "cancelled": "已取消",
    "closed": "已关闭",
    "delivered": "已签收",
    "shipped": "已发货",
    "partly_shipped": "部分发货",
    "ready": "待发货",
    "in_production": "生产中",
    "planned": "已排产",
    "not_planned": "待排产",
}
RESULT_TEXT = {"pass": "合格", "fail": "不合格", "conditional": "有条件放行"}


def _r(x: float) -> float:
    return round(x, 3)


def can_read(onto: Ontology, principal: Principal, object_type: str) -> bool:
    return principal.can(f"{onto.objects[object_type].domain}.{object_type}:read")


async def final_inspection(session: AsyncSession, tenant: str, batch_id: str) -> ObjectState | None:
    """批次最近一次出厂检验。"""
    rows = await referrers(session, tenant, "inspection", "batch", "production_batch", [batch_id])
    finals = [r for r in rows if r.state.get("kind") == "final"]
    return max(finals, key=lambda o: o.updated_seq) if finals else None


def label(obj: ObjectState | None, *keys: str) -> str:
    if obj is None:
        return ""
    for k in keys:
        if obj.state.get(k):
            return str(obj.state[k])
    return obj.object_id


async def order_progress(
    session: AsyncSession,
    onto: Ontology,
    cfg: OrdersConfig,
    principal: Principal,
    order: ObjectState,
    today: date,
) -> dict[str, Any]:
    tenant = principal.tenant_id
    s = order.state
    unit = s.get("unit")
    qty = num(s.get("qty"))
    customer = await get(session, tenant, "customer", s.get("customer"))
    product = await get(session, tenant, "product", s.get("product"))
    contract = await get(session, tenant, "contract", s.get("contract"))

    batches = await referrers(session, tenant, "production_batch", "order", "sales_order", [order.object_id])
    shipments = await referrers(session, tenant, "shipment", "order", "sales_order", [order.object_id])

    planned = produced = stocked = 0.0
    other_unit: list[str] = []
    batch_rows: list[dict[str, Any]] = []
    uninspected: list[str] = []
    failed: list[str] = []
    for b in batches:
        bs = b.state
        st = bs.get("status")
        same = cfg.same_unit(bs.get("unit"), unit)
        insp = await final_inspection(session, tenant, b.object_id)
        if st != "scrapped" and same:
            planned += num(bs.get("planned_qty")) or (num(bs.get("good_qty")) if st in PRODUCED else 0)
            if st in PRODUCED:
                produced += num(bs.get("good_qty"))
            if st in STOCKED:
                stocked += num(bs.get("good_qty"))
        if not same:
            other_unit.append(b.object_id)
        if st in STOCKED and st != "shipped":
            if insp is None:
                uninspected.append(b.object_id)
            elif insp.state.get("result") == "fail":
                failed.append(b.object_id)
        batch_rows.append(
            {
                "id": b.object_id,
                "status": st,
                "planned_qty": bs.get("planned_qty"),
                "good_qty": bs.get("good_qty"),
                "unit": bs.get("unit"),
                "unit_ok": same,
                "started_at": bs.get("started_at"),
                "completed_at": bs.get("completed_at"),
                "final_inspection": (
                    {
                        "id": insp.object_id,
                        "result": insp.state.get("result"),
                        "report_no": insp.state.get("report_no"),
                    }
                    if insp
                    else None
                ),
            }
        )

    shipped = delivered = refused = in_transit = 0.0
    ship_rows: list[dict[str, Any]] = []
    for sh in shipments:
        ss = sh.state
        st = ss.get("status")
        same = cfg.same_unit(ss.get("unit"), unit)
        q = num(ss.get("qty"))
        if same:
            if st == "refused":
                refused += q
            else:
                shipped += q
                if st == "delivered":
                    delivered += num(ss.get("received_qty")) or q
                else:
                    in_transit += q
        else:
            other_unit.append(sh.object_id)
        ship_rows.append(
            {
                "id": sh.object_id,
                "shipment_no": ss.get("shipment_no"),
                "qty": ss.get("qty"),
                "unit": ss.get("unit"),
                "shipped_at": ss.get("shipped_at"),
                "status": st,
                "received_qty": ss.get("received_qty"),
                "receiver": ss.get("receiver"),
                "batches": ss.get("batches") or [],
            }
        )

    remaining = max(qty - shipped, 0.0)
    due = date.fromisoformat(s["due_date"]) if s.get("due_date") else None
    days = (due - today).days if due else None

    status = s.get("status") or "open"
    if status in CLOSED:
        stage = status
    elif qty > 0 and delivered >= qty:
        stage = "delivered"
    elif qty > 0 and shipped >= qty:
        stage = "shipped"
    elif shipped > 0:
        stage = "partly_shipped"
    elif qty > 0 and stocked >= qty:
        stage = "ready"
    elif any(b.state.get("status") in ("in_progress", *PRODUCED) for b in batches):
        stage = "in_production"
    elif batches:
        stage = "planned"
    else:
        stage = "not_planned"

    risks: list[dict[str, str]] = []

    def risk(code: str, level: str, text: str) -> None:
        risks.append({"code": code, "level": level, "text": text})

    open_ = stage not in CLOSED and remaining > 0
    if open_ and days is not None:
        if days < 0:
            risk("overdue", "high", f"已逾期 {-days} 天，还有 {_r(remaining):g} {unit} 未发")
        elif days <= cfg.risk.warn_days and planned < qty:
            risk(
                "under_planned",
                "high",
                f"{days} 天后交期，排产计划 {_r(planned):g} / {qty:g} {unit}",
            )
        elif days <= cfg.risk.stock_days and stocked - shipped < remaining:
            avail = max(stocked - shipped, 0.0)
            risk(
                "not_stocked",
                "high",
                f"{days} 天后交期，可发库存 {_r(avail):g} / 待发 {_r(remaining):g} {unit}",
            )
    if open_ and due is None:
        risk("no_due_date", "medium", "订单没有交期")
    if failed:
        risk("final_fail", "high", f"批次出厂检验不合格：{'、'.join(failed)}")
    if uninspected:
        risk("final_missing", "medium", f"已入库批次没有出厂检验：{'、'.join(uninspected)}")
    if refused > 0:
        risk("refused", "high", f"客户拒收 {_r(refused):g} {unit}")
    if other_unit:
        risk("unit_mismatch", "medium", f"单位和订单（{unit}）不同，未计入进度：{'、'.join(other_unit)}")

    show_batches = can_read(onto, principal, "production_batch")
    show_ships = can_read(onto, principal, "shipment")
    return {
        "order_id": order.object_id,
        "order_no": s.get("order_no") or order.object_id,
        "data_mode": order.data_mode,
        "status": status,
        "customer": {"id": s.get("customer"), "name": label(customer, "name")},
        "product": {
            "id": s.get("product"),
            "code": product.state.get("code") if product else None,
            "name": label(product, "name"),
        },
        "contract": (
            {"id": contract.object_id, "contract_no": contract.state.get("contract_no")} if contract else None
        ),
        "qty": qty,
        "unit": unit,
        "amount": s.get("amount"),
        "due_date": s.get("due_date"),
        "promised_date": s.get("promised_date"),  # 计划员按排窑计划承诺的交期（W16）
        "days_to_due": days,
        "quantities": {
            "planned": _r(planned),
            "produced": _r(produced),
            "stocked": _r(stocked),
            "shipped": _r(shipped),
            "in_transit": _r(in_transit),
            "delivered": _r(delivered),
            "refused": _r(refused),
            "remaining": _r(remaining),
            "available": _r(max(stocked - shipped, 0.0)),
        },
        "stage": stage,
        "stage_text": STAGE_TEXT[stage],
        "risks": risks,
        "batches": batch_rows if show_batches else None,
        "shipments": ship_rows if show_ships else None,
    }


_LEVEL = {"high": 0, "medium": 1}


def risk_rank(p: dict[str, Any]) -> tuple:
    lv = min((_LEVEL[r["level"]] for r in p["risks"]), default=2)
    return (lv, p["due_date"] or "9999-12-31", p["order_no"])


async def order_board(
    session: AsyncSession,
    onto: Ontology,
    cfg: OrdersConfig,
    principal: Principal,
    today: date,
    *,
    include_done: bool = False,
    only_risky: bool = False,
    customer: str | None = None,
    contract: str | None = None,
) -> list[dict[str, Any]]:
    """订单看板：有风险的排前面，再按交期。默认不含已签收、已关闭、已取消的订单。"""
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == principal.tenant_id, ObjectState.object_type == "sales_order"
        )
    )
    out = []
    for o in rows:
        if customer and o.state.get("customer") != customer:
            continue
        if contract and o.state.get("contract") != contract:
            continue
        if not include_done and (o.state.get("status") in CLOSED):
            continue
        p = await order_progress(session, onto, cfg, principal, o, today)
        if not include_done and p["stage"] == "delivered":
            continue
        if only_risky and not any(r["level"] == "high" for r in p["risks"]):
            continue
        out.append(p)
    return sorted(out, key=risk_rank)


def progress_line(p: dict[str, Any]) -> str:
    q = p["quantities"]
    u = p["unit"] or ""
    due = f"交期 {p['due_date']}" if p["due_date"] else "无交期"
    return (
        f"{p['order_no']} {p['customer']['name']} {p['product']['code'] or p['product']['name']} "
        f"{p['qty']:g}{u}｜{p['stage_text']}｜入库 {q['stocked']:g} 发货 {q['shipped']:g}｜{due}"
    )
