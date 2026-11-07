"""从账本投影读出排窑的输入：在手订单还要烧多少、哪些窑能排、什么时候空出来。全部确定性计算。

订单还要烧的量（单位按 orders.yaml 归一，不换算）：
- 已烧 = 已装窑（批次有窑次）或已完工的批次的计划数量（没有计划数量按合格数量）；
- 待烧 = 订单数量 − max(已烧, 已发货)；
- 其中“已开工未装窑”的批次可以马上装窑；其余要先配料、成型、干燥（pre_firing_hours）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num, referrers
from fos.orders.progress import CLOSED, PRODUCED
from fos.planning.config import PlanningConfig
from fos.planning.solver import KilnIn, PieceIn, to_int

NEW = "__new__"  # 交期评估里假设的新订单
UNAVAILABLE = {"maintenance", "stopped"}


@dataclass
class Demand:
    id: str
    order_no: str
    customer: str
    product: ObjectState
    qty: float
    unit: str
    due_date: date | None
    promised_date: date | None = None
    fired: float = 0.0
    started: float = 0.0  # 已开工未装窑
    shipped: float = 0.0
    data_mode: str = "real"
    kilns: list[str] = field(default_factory=list)

    @property
    def to_fire(self) -> float:
        return max(self.qty - max(self.fired, self.shipped), 0.0)

    @property
    def ready_part(self) -> float:
        return min(self.started, self.to_fire)

    @property
    def new_part(self) -> float:
        return self.to_fire - self.ready_part

    @property
    def group(self) -> str:
        return str(self.product.state.get("firing_group") or f"product:{self.product.object_id}")

    @property
    def product_code(self) -> str:
        s = self.product.state
        return str(s.get("code") or s.get("name") or self.product.object_id)


@dataclass
class Context:
    tenant: str
    mode: str
    tz: ZoneInfo
    start: datetime  # 计划开始（整点，UTC）
    end: datetime
    cfg: PlanningConfig
    ocfg: OrdersConfig
    kilns: dict[str, ObjectState] = field(default_factory=dict)
    kiln_free: dict[str, int] = field(default_factory=dict)  # 窑 → 从计划开始多少小时后空出来
    demands: dict[str, Demand] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def hour(self, dt: datetime) -> int:
        return int((dt - self.start).total_seconds() // 3600)

    def at(self, h: int) -> datetime:
        return self.start + timedelta(hours=h)

    def local_date(self, dt: datetime) -> date:
        return dt.astimezone(self.tz).date()

    def due_hour(self, d: date) -> int:
        """交期当天结束前可发货。"""
        return self.hour(datetime.combine(d + timedelta(days=1), time(0), self.tz))

    @property
    def horizon(self) -> int:
        return self.hour(self.end)

    def kiln_name(self, kid: str) -> str:
        k = self.kilns.get(kid)
        return str(k.state.get("name") or kid) if k else kid

    def cycle(self, k: ObjectState) -> int:
        return max(1, round(num(k.state.get("cycle_hours")) or self.cfg.default_cycle_hours))


def plan_start(now: datetime) -> datetime:
    """计划从下一个整点开始。"""
    now = now.astimezone(ZoneInfo("UTC"))
    base = now.replace(minute=0, second=0, microsecond=0)
    return base if base == now else base + timedelta(hours=1)


async def _all(session: AsyncSession, tenant: str, object_type: str, mode: str) -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(ObjectState.tenant_id == tenant, ObjectState.object_type == object_type)
    )
    return sorted((o for o in rows if o.data_mode == mode), key=lambda o: o.created_seq)


def _d(v: Any) -> date | None:
    return date.fromisoformat(v) if v else None


async def order_demand(
    session: AsyncSession, tenant: str, ocfg: OrdersConfig, order: ObjectState
) -> Demand | None:
    s = order.state
    product = await get(session, tenant, "product", s.get("product"))
    if product is None:
        return None
    customer = await get(session, tenant, "customer", s.get("customer"))
    unit = s.get("unit") or ""
    d = Demand(
        id=order.object_id,
        order_no=str(s.get("order_no") or order.object_id),
        customer=str(customer.state.get("name") or customer.object_id) if customer else "",
        product=product,
        qty=num(s.get("qty")),
        unit=unit,
        due_date=_d(s.get("due_date")),
        promised_date=_d(s.get("promised_date")),
        data_mode=order.data_mode,
    )
    for b in await referrers(session, tenant, "production_batch", "order", "sales_order", [order.object_id]):
        bs = b.state
        st = bs.get("status")
        if st == "scrapped" or not ocfg.same_unit(bs.get("unit"), unit):
            continue
        q = num(bs.get("planned_qty")) or (num(bs.get("good_qty")) if st in PRODUCED else 0)
        if bs.get("kiln_run") or st in PRODUCED:
            d.fired += q
        elif st == "in_progress":
            d.started += q
    for sh in await referrers(session, tenant, "shipment", "order", "sales_order", [order.object_id]):
        if sh.state.get("status") != "refused" and ocfg.same_unit(sh.state.get("unit"), unit):
            d.shipped += num(sh.state.get("qty"))
    return d


async def load_context(
    session: AsyncSession,
    *,
    tenant: str,
    cfg: PlanningConfig,
    ocfg: OrdersConfig,
    tz: ZoneInfo,
    now: datetime,
    mode: str = "real",
    exclude: set[str] | None = None,
) -> Context:
    start = plan_start(now)
    ctx = Context(tenant, mode, tz, start, start + timedelta(days=cfg.horizon_days), cfg, ocfg)

    firing: dict[str, datetime] = {}
    for r in await _all(session, tenant, "kiln_run", mode):
        if r.state.get("status") == "firing" and r.state.get("started_at"):
            st = datetime.fromisoformat(r.state["started_at"])
            firing[r.state["kiln"]] = max(firing.get(r.state["kiln"], st), st)
    for k in await _all(session, tenant, "kiln", mode):
        ks = k.state
        name = ks.get("name") or k.object_id
        if ks.get("plan_status") in UNAVAILABLE:
            ctx.warnings.append(f"窑炉 {name} 在{'维修' if ks['plan_status'] == 'maintenance' else '停用'}，不排")
            continue
        if num(ks.get("capacity")) <= 0 or not ks.get("capacity_unit"):
            ctx.warnings.append(f"窑炉 {name} 没有设置每窑装量和单位，不排")
            continue
        if not ks.get("cycle_hours"):
            ctx.warnings.append(f"窑炉 {name} 没有设置烧成周期，按默认 {cfg.default_cycle_hours} 小时")
        ctx.kilns[k.object_id] = k
        free = 0
        if k.object_id in firing:
            free = max(0, ctx.hour(firing[k.object_id] + timedelta(hours=ctx.cycle(k))) + 1)
        ctx.kiln_free[k.object_id] = free

    for o in await _all(session, tenant, "sales_order", mode):
        if o.state.get("status") in CLOSED or o.object_id in (exclude or set()):
            continue
        d = await order_demand(session, tenant, ocfg, o)
        if d is None or d.to_fire <= 1e-9:
            continue
        add_demand(ctx, d)
    return ctx


def add_demand(ctx: Context, d: Demand) -> None:
    ps = d.product.state
    for kid, k in ctx.kilns.items():
        ks = k.state
        if not ctx.ocfg.same_unit(ks.get("capacity_unit"), d.unit):
            continue
        if ps.get("firing_temp") and ks.get("max_temp") and num(ps["firing_temp"]) > num(ks["max_temp"]):
            continue
        d.kilns.append(kid)
    if not d.kilns:
        ctx.warnings.append(f"订单 {d.order_no}（{d.product_code} {d.unit}）没有能烧的窑：单位或窑温不匹配")
    ctx.demands[d.id] = d


def pieces(ctx: Context) -> list[PieceIn]:
    out = []
    for d in ctx.demands.values():
        due = ctx.due_hour(d.due_date) if d.due_date else None
        fh = round(num(d.product.state.get("firing_hours"))) or None
        common = {"order": d.id, "group": d.group, "kilns": tuple(d.kilns), "due": due, "firing_hours": fh}
        if d.ready_part > 1e-9:
            out.append(PieceIn(key=f"{d.id}#ready", qty=to_int(d.ready_part), release=0, **common))
        if d.new_part > 1e-9:
            out.append(
                PieceIn(key=f"{d.id}#new", qty=to_int(d.new_part), release=ctx.cfg.pre_firing_hours, **common)
            )
    return out


def kiln_inputs(ctx: Context) -> list[KilnIn]:
    return [
        KilnIn(
            id=kid,
            capacity=to_int(num(k.state.get("capacity"))),
            cycle_hours=ctx.cycle(k),
            available_from=ctx.kiln_free[kid],
            max_temp=k.state.get("max_temp"),
        )
        for kid, k in ctx.kilns.items()
    ]
