"""发货前校验（跨对象）。

同一套检查有两种用法：
- 发货前预检（API / 卡片）：返回阻断问题和提示，给人看；
- 账本守卫：warehouse.shipment_dispatched 写入前再查一次，有阻断问题就拒绝写入。
  所以不论从 /v1/events、卡片确认还是审批通过后执行，都绕不过去（规则在 orders.yaml，不在代码里写阈值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num, referrers
from fos.orders.progress import CLOSED, RESULT_TEXT, final_inspection

SHIP_EVENT = "warehouse.shipment_dispatched"


@dataclass
class ShipCheck:
    problems: list[str] = field(default_factory=list)  # 阻断：不能发
    warnings: list[str] = field(default_factory=list)  # 提示：可以发，但要人看一眼
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def out(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": self.problems, "warnings": self.warnings, **self.info}


async def check_shipment(
    session: AsyncSession, tenant: str, cfg: OrdersConfig, payload: dict[str, Any]
) -> ShipCheck:
    rules = cfg.shipping
    c = ShipCheck()
    order = await get(session, tenant, "sales_order", payload.get("order"))
    if order is None:
        c.problems.append(f"订单 {payload.get('order')} 不存在")
        return c
    os_ = order.state
    unit = payload.get("unit") or os_.get("unit")
    qty = num(payload.get("qty"))
    if os_.get("status") in CLOSED:
        c.problems.append(f"订单已{'取消' if os_.get('status') == 'cancelled' else '关闭'}，不能发货")
    if not cfg.same_unit(unit, os_.get("unit")):
        c.problems.append(f"发货单位 {unit} 和订单单位 {os_.get('unit')} 不同")
    if qty <= 0:
        c.problems.append("发货数量必须大于 0")

    # ---- 累计发货不超过订单数量（含容差） ----
    shipments = await referrers(session, tenant, "shipment", "order", "sales_order", [order.object_id])
    shipped = sum(
        num(s.state.get("qty"))
        for s in shipments
        if s.state.get("status") != "refused" and cfg.same_unit(s.state.get("unit"), os_.get("unit"))
    )
    limit = num(os_.get("qty")) * (1 + rules.over_ship_pct / 100)
    if shipped + qty > limit + 1e-9:
        c.problems.append(
            f"超发：订单 {num(os_.get('qty')):g} {os_.get('unit')}，已发 {shipped:g}，本次 {qty:g}"
            f"（容差 {rules.over_ship_pct:g}%）"
        )
    c.info["order_qty"] = num(os_.get("qty"))
    c.info["shipped_before"] = round(shipped, 3)

    # ---- 批次 ----
    batch_ids: list[str] = list(payload.get("batches") or [])
    if not batch_ids:
        c.warnings.append("没有填发货批次：以后客户异议时追溯不到批次")
    good_total = 0.0
    units_ok = True
    rows = []
    for bid in batch_ids:
        b = await get(session, tenant, "production_batch", bid)
        if b is None:
            c.problems.append(f"批次 {bid} 不存在")
            continue
        bs = b.state
        if bs.get("product") != os_.get("product"):
            c.problems.append(f"批次 {bid} 的产品和订单不一致")
        owner = bs.get("order")
        if owner and owner != order.object_id:
            msg = f"批次 {bid} 是订单 {owner} 排产的"
            (c.warnings if rules.allow_other_order_batches else c.problems).append(msg)
        st = bs.get("status")
        if st == "scrapped":
            c.problems.append(f"批次 {bid} 已报废")
        elif rules.require_in_stock and st not in ("in_stock", "shipped"):
            c.problems.append(f"批次 {bid} 还没有入库")
        insp = await final_inspection(session, tenant, bid)
        result = insp.state.get("result") if insp else None
        if result is None:
            msg = f"批次 {bid} 没有出厂检验记录"
            (c.problems if rules.require_final_inspection else c.warnings).append(msg)
        elif result == "fail":
            msg = f"批次 {bid} 出厂检验不合格"
            (c.problems if rules.require_final_inspection else c.warnings).append(msg)
        elif result == "conditional":
            msg = f"批次 {bid} 出厂检验为有条件放行"
            (c.warnings if rules.allow_conditional else c.problems).append(msg)
        if cfg.same_unit(bs.get("unit"), unit):
            good_total += num(bs.get("good_qty"))
        else:
            units_ok = False
            c.warnings.append(f"批次 {bid} 单位 {bs.get('unit')} 和发货单位不同，无法核对库存")
        rows.append(
            {
                "id": bid,
                "status": st,
                "good_qty": bs.get("good_qty"),
                "unit": bs.get("unit"),
                "final_inspection": RESULT_TEXT.get(result or "", "无"),
                "report_no": insp.state.get("report_no") if insp else None,
            }
        )
    c.info["batches"] = rows

    # ---- 批次可发数量 = 合格数量 − 引用这些批次的未拒收发货（多批次合发时整单计入，偏保守） ----
    if batch_ids and units_ok:
        used = await referrers(session, tenant, "shipment", "batches", "production_batch", batch_ids)
        used_qty = sum(num(s.state.get("qty")) for s in used if s.state.get("status") != "refused")
        avail = good_total - used_qty
        c.info["batch_available"] = round(avail, 3)
        if qty > avail + 1e-9:
            c.problems.append(f"批次可发数量不足：可发 {max(avail, 0):g} {unit}，本次 {qty:g}")
    return c


class ShipmentGuard:
    """账本守卫：发货事件写入前跑一遍 check_shipment，有阻断问题就拒绝。"""

    def __init__(self, configs: dict[str, OrdersConfig]):
        self.configs = configs

    async def __call__(
        self,
        session: AsyncSession,
        principal: Principal,
        onto: Ontology,
        payload: dict[str, Any],
        state: dict[str, Any] | None,
    ) -> list[str]:
        cfg = self.configs.get(principal.tenant_id)
        if cfg is None:
            return []
        c = await check_shipment(session, principal.tenant_id, cfg, payload)
        return c.problems
