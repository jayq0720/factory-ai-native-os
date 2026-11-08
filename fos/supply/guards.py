"""采购与仓储的账本守卫（跨对象硬规则，规则值在 supply.yaml，不在代码里写阈值）。

- 库存变动（领料 / 退料 / 盘点）：类型和事件一致、数量为正、批次和物料一致、单位一致；
  领料只能领可用状态（合格 / 让步接收）的批次、不能超过现存（红线：待检、不合格不能领）；
  退料不能超过已领未退；盘亏不能超过账面现存。
- 按采购单到货（来料入库填了采购单）：物料、供应商、单位要和采购单一致，采购单没关闭，
  累计到货不超订（含容差）。
  填的采购单号在系统里找不到时放行（纸质老单号），只在界面上提示。

不论从 /v1/events、专用接口、卡片确认还是审批通过后执行写入，都会过这里。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num
from fos.supply.config import SupplyConfig
from fos.supply.stock import EPS, LOT_STATUS_TEXT, _scan, lot_balance

ISSUE_EVENT = "warehouse.stock_issued"
RETURN_EVENT = "warehouse.stock_returned"
COUNT_EVENT = "warehouse.stock_counted"
RECEIVE_EVENT = "warehouse.lot_received"
MOVE_KINDS = {ISSUE_EVENT: {"issue"}, RETURN_EVENT: {"return"}, COUNT_EVENT: {"count_gain", "count_loss"}}


async def find_po(session: AsyncSession, tenant: str, key: str | None) -> ObjectState | None:
    """采购单：按编号或采购单号找。"""
    if not key:
        return None
    po = await get(session, tenant, "purchase_order", key)
    if po is not None:
        return po
    hits = [o for o in await _scan(session, tenant, "purchase_order") if o.state.get("po_no") == key]
    return hits[0] if len(hits) == 1 else None


async def po_lots(session: AsyncSession, tenant: str, po: ObjectState) -> list[ObjectState]:
    keys = {po.object_id, po.state.get("po_no")} - {None, ""}
    return [
        lot for lot in await _scan(session, tenant, "material_lot") if lot.state.get("purchase_order") in keys
    ]


async def check_move(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    event_type: str,
    payload: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    kind, qty = payload.get("kind"), num(payload.get("qty"))
    allowed = MOVE_KINDS.get(event_type, set())
    if kind not in allowed:
        problems.append(f"类型 {kind} 不能用这个事件记录（应为 {'、'.join(sorted(allowed))}）")
    if qty <= 0:
        problems.append("数量必须大于 0")
    b = await lot_balance(session, tenant, payload.get("lot") or "")
    if b is None:
        return [*problems, f"原料批次 {payload.get('lot')} 不存在"]
    ls = b.lot.state
    if ls.get("material") != payload.get("material"):
        problems.append(f"批次 {b.lot.object_id} 不是这种物料")
    if not ocfg.same_unit(payload.get("unit"), ls.get("unit")):
        problems.append(f"单位 {payload.get('unit')} 和批次单位 {ls.get('unit')} 不同")
    unit = ls.get("unit") or ""
    if kind == "issue":
        if b.status not in cfg.stock.usable_statuses:
            st = LOT_STATUS_TEXT.get(b.status or "", b.status or "未填")
            problems.append(f"批次 {b.lot.object_id} 状态是“{st}”，不能领用")
        if qty > b.on_hand + EPS:
            problems.append(f"批次 {b.lot.object_id} 现存 {max(b.on_hand, 0):g} {unit}，不够领 {qty:g}")
    elif kind == "return" and qty > b.issued_net + EPS:
        problems.append(f"批次 {b.lot.object_id} 已领未退 {b.issued_net:g} {unit}，不能退 {qty:g}")
    elif kind == "count_loss" and qty > b.on_hand + EPS:
        problems.append(f"批次 {b.lot.object_id} 账面现存 {max(b.on_hand, 0):g} {unit}，盘亏不能超过它")
    return problems


async def check_receipt(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    payload: dict[str, Any],
    *,
    exclude_lot: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """按采购单到货的校验。返回 (阻断问题, 采购单到货情况)；没填或找不到采购单返回空。"""
    po = await find_po(session, tenant, payload.get("purchase_order"))
    if po is None:
        return [], {}
    ps = po.state
    problems: list[str] = []
    label = ps.get("po_no") or po.object_id
    if ps.get("status") in cfg.receiving.closed_statuses:
        problems.append(
            f"采购单 {label} 已{'取消' if ps.get('status') == 'cancelled' else '关闭'}，不能再到货"
        )
    if payload.get("material") != ps.get("material"):
        problems.append(f"到货物料和采购单 {label} 的物料不一致")
    if payload.get("supplier") and payload.get("supplier") != ps.get("supplier"):
        problems.append(f"到货供应商和采购单 {label} 的供应商不一致")
    if not ocfg.same_unit(payload.get("unit"), ps.get("unit")):
        problems.append(f"到货单位 {payload.get('unit')} 和采购单单位 {ps.get('unit')} 不同")
    before = sum(
        num(lot.state.get("qty"))
        for lot in await po_lots(session, tenant, po)
        if lot.object_id != exclude_lot and ocfg.same_unit(lot.state.get("unit"), ps.get("unit"))
    )
    qty = num(payload.get("qty"))
    limit = num(ps.get("qty")) * (1 + cfg.receiving.over_receive_pct / 100)
    if before + qty > limit + EPS:
        problems.append(
            f"超订到货：采购单 {label} 订 {num(ps.get('qty')):g} {ps.get('unit')}，已到 {before:g}，"
            f"本次 {qty:g}（容差 {cfg.receiving.over_receive_pct:g}%）"
        )
    info = {
        "purchase_order": po.object_id,
        "po_no": ps.get("po_no"),
        "ordered": num(ps.get("qty")),
        "received_before": round(before, 3),
    }
    return problems, info


class SupplyGuard:
    """账本守卫：库存变动和按采购单到货写入前再查一次，有阻断问题就拒绝。"""

    def __init__(self, event_type: str, configs: dict[str, SupplyConfig], orders: dict[str, OrdersConfig]):
        self.event_type = event_type
        self.configs = configs
        self.orders = orders

    async def __call__(
        self,
        session: AsyncSession,
        principal: Principal,
        onto: Ontology,
        payload: dict[str, Any],
        state: dict[str, Any] | None,
    ) -> list[str]:
        tid = principal.tenant_id
        cfg = self.configs.get(tid)
        if cfg is None:
            return []
        ocfg = self.orders.get(tid) or OrdersConfig()
        if self.event_type == RECEIVE_EVENT:
            problems, _ = await check_receipt(session, tid, cfg, ocfg, payload)
            return problems
        return await check_move(session, tid, cfg, ocfg, self.event_type, payload)
