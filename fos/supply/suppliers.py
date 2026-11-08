"""供应商评分：最近 window_days 天的到货，按来料合格率和准时率加权（权重在 supply.yaml）。确定性计算。

- 合格率：合格 = 1、让步接收 = 0.5、不合格 = 0，待检的不计；
- 准时率：按采购单到货的批次，到货日期 ≤ 采购单交期算准时；没关联采购单或采购单没交期的不计；
- 总分 = (合格率 × 质量权重 + 准时率 × 准时权重) ÷ 有数据的权重之和 × 100；两项都没数据时不打分。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from fos.orders.config import OrdersConfig
from fos.supply.config import SupplyConfig
from fos.supply.stock import _scan

QUALITY = {"accepted": 1.0, "concession": 0.5, "rejected": 0.0}


def _day(iso: str | None, tz: ZoneInfo) -> date | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz).date()


async def score_suppliers(
    session: AsyncSession,
    tenant: str,
    cfg: SupplyConfig,
    ocfg: OrdersConfig,
    *,
    today: date,
    tz: ZoneInfo,
    mode: str = "real",
) -> list[dict[str, Any]]:
    since = today - timedelta(days=cfg.suppliers.window_days)
    pos = {po.object_id: po for po in await _scan(session, tenant, "purchase_order")}
    by_no = {po.state.get("po_no"): po for po in pos.values() if po.state.get("po_no")}
    stats: dict[str, dict[str, Any]] = {}
    for s in await _scan(session, tenant, "supplier"):
        if s.data_mode == mode:
            stats[s.object_id] = {"name": s.state.get("name"), "lots": 0, "q": [], "on_time": 0, "timed": 0}
    for lot in await _scan(session, tenant, "material_lot"):
        ls = lot.state
        st = stats.get(ls.get("supplier"))
        day = _day(ls.get("received_at"), tz)
        if st is None or lot.data_mode != mode or day is None or day < since:
            continue
        st["lots"] += 1
        if ls.get("status") in QUALITY:
            st["q"].append(QUALITY[ls["status"]])
        po = pos.get(ls.get("purchase_order") or "") or by_no.get(ls.get("purchase_order"))
        if po is not None and po.state.get("due_date"):
            st["timed"] += 1
            st["on_time"] += day.isoformat() <= po.state["due_date"]
    w = cfg.suppliers.weights
    out = []
    for sid, st in stats.items():
        quality = sum(st["q"]) / len(st["q"]) if st["q"] else None
        on_time = st["on_time"] / st["timed"] if st["timed"] else None
        parts = [(quality, w.quality), (on_time, w.on_time)]
        wsum = sum(wt for v, wt in parts if v is not None)
        score = round(sum(v * wt for v, wt in parts if v is not None) / wsum * 100, 1) if wsum else None
        out.append(
            {
                "supplier": sid,
                "name": st["name"],
                "lots": st["lots"],
                "inspected": len(st["q"]),
                "quality_rate": round(quality * 100, 1) if quality is not None else None,
                "on_time": st["on_time"],
                "timed": st["timed"],
                "on_time_rate": round(on_time * 100, 1) if on_time is not None else None,
                "score": score,
            }
        )
    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["name"] or ""))
    return out


FORMULA = (
    "合格率：合格 1、让步 0.5、不合格 0（待检不计）；准时率：到货日 ≤ 采购单交期；"
    "总分 = 加权平均 × 100（只算有数据的项）"
)
