"""物料需求（MRP）：按排窑计划算每种物料什么时候要多少，和现存、在途比，找出缺口。确定性计算。

- 需求：每个计划装窑行里还没开工的量 × 当前配方版本的单位用量 × (1 + 损耗率)，
  在装窑前 pre_firing_hours 要到位；
  订单里已开工的批次先抵扣（原料已经领走了）；
- 现存：状态在 materials.stock_statuses 的原料批次合计（W17 仓储上线前没有领用记录，未扣领用，会偏多）；
- 在途：已到货待检的批次（现在可用）+ 未到货采购单（按采购单交期到位，没交期的不计入时间线，单列）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.orders.data import get, num, referrers
from fos.planning.demand import NEW, Context, _all

STOCK_NOTE = "现存 = 合格到货批次合计，还没有扣领用（W17 仓储上线后改为实时库存）"


@dataclass
class _Mat:
    obj: ObjectState
    need: float = 0.0
    needs: list[tuple[datetime, float, str]] = field(default_factory=list)  # (要到位时间, 数量, 订单号)


async def _recipe_lines(session: AsyncSession, ctx: Context, product_id: str, cache: dict) -> list | None:
    if product_id in cache:
        return cache[product_id]
    recipes = [
        r for r in await _all(session, ctx.tenant, "recipe", ctx.mode) if r.state.get("product") == product_id
    ]
    lines = None
    if recipes:
        cur = max(recipes, key=lambda r: (int(r.state.get("version") or 0), r.created_seq))
        lines = await referrers(session, ctx.tenant, "recipe_material", "recipe", "recipe", [cur.object_id])
    cache[product_id] = lines
    return lines


async def material_plan(session: AsyncSession, ctx: Context, runs: list[dict[str, Any]]) -> dict[str, Any]:
    warnings: list[str] = []
    pre = timedelta(hours=ctx.cfg.pre_firing_hours)
    # 订单里已开工的量先抵扣：按装窑时间顺序分摊
    covered = {oid: d.started for oid, d in ctx.demands.items()}
    cache: dict[str, list | None] = {}
    mats: dict[str, _Mat] = {}
    no_recipe: set[str] = set()
    for run in sorted(runs, key=lambda r: r["start_at"]):
        if run["end_at"] <= ctx.start:
            continue
        for ld in run["loads"]:
            d = ctx.demands.get(ld["order"])
            if d is None:
                continue
            use = min(covered.get(d.id, 0.0), ld["qty"])
            covered[d.id] = covered.get(d.id, 0.0) - use
            new = ld["qty"] - use
            if new <= 1e-9:
                continue
            if not ctx.ocfg.same_unit(d.unit, d.product.state.get("unit")):
                warnings.append(
                    f"订单 {d.order_no} 单位 {d.unit} 和产品计量单位 "
                    f"{d.product.state.get('unit')} 不同，未算物料"
                )
                continue
            lines = await _recipe_lines(session, ctx, d.product.object_id, cache)
            if not lines:
                no_recipe.add(d.product_code)
                continue
            at = max(run["start_at"] - pre, ctx.start)
            for ln in lines:
                ls = ln.state
                m = await get(session, ctx.tenant, "material", ls.get("material"))
                if m is None:
                    continue
                if not ctx.ocfg.same_unit(ls.get("unit"), m.state.get("unit")):
                    warnings.append(
                        f"配方用量单位 {ls.get('unit')} 和物料 {m.state.get('name')} 的单位不同，未算"
                    )
                    continue
                q = new * num(ls.get("qty_per_unit")) * (1 + num(ls.get("loss_pct")) / 100)
                mm = mats.setdefault(m.object_id, _Mat(m))
                mm.need += q
                mm.needs.append((at, q, "新订单" if d.id == NEW else d.order_no))
    if no_recipe:
        warnings.append(f"这些产品没有配方用量，算不出物料需求：{'、'.join(sorted(no_recipe))}")

    rows = []
    for mid, mm in mats.items():
        unit = mm.obj.state.get("unit")
        on_hand = incoming = undated = 0.0
        arrivals: list[tuple[datetime, float]] = []
        for lot in await referrers(session, ctx.tenant, "material_lot", "material", "material", [mid]):
            ls = lot.state
            if lot.data_mode != ctx.mode or not ctx.ocfg.same_unit(ls.get("unit"), unit):
                continue
            if ls.get("status") in ctx.cfg.materials.stock_statuses:
                on_hand += num(ls.get("qty"))
            elif ls.get("status") in ctx.cfg.materials.incoming_statuses:
                incoming += num(ls.get("qty"))
        for po in await referrers(session, ctx.tenant, "purchase_order", "material", "material", [mid]):
            ps = po.state
            if po.data_mode != ctx.mode or ps.get("status") not in ctx.cfg.materials.po_statuses:
                continue
            if not ctx.ocfg.same_unit(ps.get("unit"), unit):
                continue
            if ps.get("due_date"):
                due = datetime.fromisoformat(ps["due_date"])
                arrivals.append((datetime.combine(due.date(), time(0), ctx.tz), num(ps.get("qty"))))
            else:
                undated += num(ps.get("qty"))
        cum = 0.0
        worst = 0.0
        first = None
        orders: list[str] = []
        for at, q, who in sorted(mm.needs):
            cum += q
            if who not in orders:
                orders.append(who)
            avail = on_hand + incoming + sum(a for t, a in arrivals if t <= at)
            gap = cum - avail
            if gap > 1e-9:
                worst = max(worst, gap)
                first = first or at
        on_order = sum(a for _, a in arrivals)
        safety = num(mm.obj.state.get("safety_stock"))
        left = on_hand + incoming + on_order - mm.need
        rows.append(
            {
                "material": mid,
                "name": mm.obj.state.get("name") or mid,
                "unit": unit,
                "need": round(mm.need, 3),
                "on_hand": round(on_hand, 3),
                "incoming": round(incoming, 3),
                "on_order": round(on_order, 3),
                "on_order_undated": round(undated, 3),
                "shortage": round(worst, 3),
                "short_from": ctx.local_date(first).isoformat() if first else None,
                "safety_stock": safety or None,
                "below_safety": bool(safety) and left < safety,
                "orders": orders,
            }
        )
    rows.sort(key=lambda r: (-(r["shortage"] > 0), r["short_from"] or "9999", r["name"]))
    return {
        "materials": rows,
        "shortages": [r for r in rows if r["shortage"] > 0],
        "warnings": warnings,
        "note": STOCK_NOTE,
    }


def shortage_lines(mp: dict[str, Any], limit: int = 10) -> list[str]:
    out = []
    for r in mp["shortages"][:limit]:
        out.append(
            f"{r['name']} 缺 {r['shortage']:g} {r['unit']}（{r['short_from']} 起要用；需 {r['need']:g}，"
            f"现存 {r['on_hand']:g}，在途 {r['incoming'] + r['on_order']:g}）"
        )
    return out
