"""实时库存：原料批次现存 = 到货数量 − 领用 + 退料 + 盘盈 − 盘亏。确定性计算，只读投影。

库存不单独存一张“余额表”：余额是事件（来料入库、库存变动）的投影，随时可以从账本重放出来（CLAUDE.md 规则 5）。
单厂规模，按租户扫一遍批次和变动即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.orders.config import OrdersConfig
from fos.orders.data import num
from fos.supply.config import SupplyConfig

SIGN = {"issue": -1, "return": 1, "count_gain": 1, "count_loss": -1}
KIND_TEXT = {"issue": "领用", "return": "退料", "count_gain": "盘盈", "count_loss": "盘亏"}
LOT_STATUS_TEXT = {
    "pending_inspection": "待检",
    "accepted": "合格",
    "rejected": "不合格",
    "concession": "让步接收",
}
EPS = 1e-9


@dataclass
class LotBalance:
    lot: ObjectState
    moved: dict[str, float] = field(default_factory=lambda: dict.fromkeys(SIGN, 0.0))

    @property
    def received(self) -> float:
        return num(self.lot.state.get("qty"))

    @property
    def on_hand(self) -> float:
        return round(self.received + sum(SIGN[k] * v for k, v in self.moved.items()), 6)

    @property
    def issued_net(self) -> float:
        """已领未退。"""
        return round(self.moved["issue"] - self.moved["return"], 6)

    @property
    def status(self) -> str | None:
        return self.lot.state.get("status")


async def _scan(session: AsyncSession, tenant: str, object_type: str) -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(ObjectState.tenant_id == tenant, ObjectState.object_type == object_type)
    )
    return sorted(rows, key=lambda o: o.created_seq)


async def lot_balances(
    session: AsyncSession,
    tenant: str,
    *,
    material_ids: set[str] | None = None,
    mode: str | None = None,
) -> dict[str, LotBalance]:
    out: dict[str, LotBalance] = {}
    for lot in await _scan(session, tenant, "material_lot"):
        if mode and lot.data_mode != mode:
            continue
        if material_ids is not None and lot.state.get("material") not in material_ids:
            continue
        out[lot.object_id] = LotBalance(lot)
    if not out:
        return out
    for mv in await _scan(session, tenant, "stock_move"):
        b = out.get(mv.state.get("lot"))
        kind = mv.state.get("kind")
        if b is not None and kind in SIGN:
            b.moved[kind] += num(mv.state.get("qty"))
    return out


async def lot_balance(session: AsyncSession, tenant: str, lot_id: str) -> LotBalance | None:
    lot = await session.get(ObjectState, (tenant, "material_lot", lot_id))
    if lot is None:
        return None
    bals = await lot_balances(session, tenant, material_ids={lot.state.get("material")})
    return bals.get(lot_id)


def fifo(bals: list[LotBalance]) -> list[LotBalance]:
    """先进先出：按到货时间、再按入账顺序。"""
    return sorted(bals, key=lambda b: (str(b.lot.state.get("received_at") or ""), b.lot.created_seq))


def lot_row(b: LotBalance) -> dict[str, Any]:
    s = b.lot.state
    return {
        "lot": b.lot.object_id,
        "status": s.get("status"),
        "status_text": LOT_STATUS_TEXT.get(s.get("status") or "", s.get("status") or "未填"),
        "received": b.received,
        "on_hand": b.on_hand,
        "issued_net": b.issued_net,
        "unit": s.get("unit"),
        "location": s.get("location"),
        "supplier": s.get("supplier"),
        "supplier_lot_no": s.get("supplier_lot_no"),
        "received_at": s.get("received_at"),
        "purchase_order": s.get("purchase_order"),
    }


async def material_stock(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    *,
    mode: str = "real",
    material_ids: set[str] | None = None,
) -> dict[str, Any]:
    """每种物料：可用现存（合格 / 让步）、待检、不可用（不合格等）、安全库存、批次明细（先进先出顺序）。"""
    warnings: list[str] = []
    mats = [
        m
        for m in await _scan(session, tenant, "material")
        if m.data_mode == mode and (material_ids is None or m.object_id in material_ids)
    ]
    bals = await lot_balances(session, tenant, material_ids={m.object_id for m in mats}, mode=mode)
    by_mat: dict[str, list[LotBalance]] = {}
    for b in bals.values():
        by_mat.setdefault(b.lot.state.get("material"), []).append(b)
    rows = []
    for m in mats:
        unit = m.state.get("unit")
        usable = incoming = blocked = 0.0
        lots = []
        for b in fifo(by_mat.get(m.object_id, [])):
            if not ocfg.same_unit(b.lot.state.get("unit"), unit):
                warnings.append(
                    f"批次 {b.lot.object_id} 单位 {b.lot.state.get('unit')} 和物料 {m.state.get('name')} "
                    f"的单位 {unit} 不同，没有计入库存"
                )
                continue
            if b.on_hand <= EPS:
                continue
            if b.status in cfg.stock.usable_statuses:
                usable += b.on_hand
            elif b.status in cfg.stock.incoming_statuses:
                incoming += b.on_hand
            else:
                blocked += b.on_hand
            lots.append(lot_row(b))
        safety = num(m.state.get("safety_stock"))
        rows.append(
            {
                "material": m.object_id,
                "name": m.state.get("name") or m.object_id,
                "spec": m.state.get("spec"),
                "category": m.state.get("category"),
                "unit": unit,
                "usable": round(usable, 3),
                "incoming": round(incoming, 3),
                "blocked": round(blocked, 3),
                "safety_stock": safety or None,
                "below_safety": bool(safety) and usable < safety - EPS,
                "lots": lots,
            }
        )
    rows.sort(key=lambda r: (not r["below_safety"], r["name"]))
    return {"materials": rows, "warnings": warnings}
