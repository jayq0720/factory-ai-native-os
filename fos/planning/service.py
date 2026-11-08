"""排窑建议、确认排窑计划、交期评估。

- draft：读在手订单和窑炉 → 求解 → 计划窑次 + 每个订单的预计可发货日期 + 窑炉利用率 + 物料缺口（不入账）；
- confirm：计划员核对（可以改）后提交，一次写入“确认排窑计划 + 每个装窑行”（全有或全无），旧版本标为已替代；
  勾选的订单同时记“承诺交期”；每个装窑行再过一遍账本守卫（guard.py）；
- evaluate：新订单（或假设的订单）放进去重排一次，和不放的结果比：能不能按期、会把哪些订单往后推、缺什么料。

数字全部由求解器和确定性代码算；模型（计划员 Skill）只负责解释，不改数。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ledger import AppendRequest, ApprovalRequired, Ledger
from fos.ledger.service import Evidence
from fos.orders.config import OrdersConfig
from fos.orders.data import get, num, referrers
from fos.orders.progress import CLOSED
from fos.orders.service import new_id
from fos.planning.config import PlanningConfig
from fos.planning.demand import (
    NEW,
    Context,
    Demand,
    _all,
    add_demand,
    kiln_inputs,
    load_context,
    order_demand,
    pieces,
)
from fos.planning.materials import material_plan
from fos.planning.solver import Problem, Solution, solve, to_qty

EPS = 1e-6


class PlanningError(Exception):
    status = 422


class PlanNotFound(PlanningError):
    status = 404


class PlanConflict(PlanningError):
    status = 409


# ---------------- 求解 → 计划窑次 ----------------


def _solve(ctx: Context) -> Solution:
    cfg = ctx.cfg
    return solve(
        Problem(
            kilns=kiln_inputs(ctx),
            pieces=pieces(ctx),
            horizon=ctx.horizon,
            post_hours=cfg.post_firing_hours,
            max_runs_per_kiln=cfg.max_runs_per_kiln,
            solver=cfg.solver,
        )
    )


def runs_from_solution(ctx: Context, sol: Solution) -> list[dict[str, Any]]:
    seq: dict[str, int] = {}
    runs = []
    for r in sol.runs:
        seq[r.kiln] = seq.get(r.kiln, 0) + 1
        loads: dict[str, int] = {}
        for key, v in r.loads.items():
            oid = key.rsplit("#", 1)[0]
            loads[oid] = loads.get(oid, 0) + v
        runs.append(
            {
                "kiln": r.kiln,
                "run_no": f"{ctx.kiln_name(r.kiln)}-{seq[r.kiln]:02d}",
                "start_at": ctx.at(r.start),
                "end_at": ctx.at(r.end),
                "loads": [{"order": oid, "qty": to_qty(v)} for oid, v in loads.items()],
            }
        )
    return runs


def _iso(ctx: Context, dt: datetime) -> str:
    return dt.astimezone(ctx.tz).isoformat(timespec="minutes")


async def summarize(session: AsyncSession, ctx: Context, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """计划窑次 → 订单预计可发货、窑炉利用率、物料缺口。草稿和已确认计划共用。"""
    post = timedelta(hours=ctx.cfg.post_firing_hours)
    run_rows = []
    planned: dict[str, float] = {}
    ready: dict[str, datetime] = {}
    for r in runs:
        k = ctx.kilns.get(r["kiln"])
        cap = num(k.state.get("capacity")) if k else 0.0
        loads = []
        for ld in r["loads"]:
            d = ctx.demands.get(ld["order"])
            if r["end_at"] > ctx.start:
                planned[ld["order"]] = planned.get(ld["order"], 0.0) + ld["qty"]
                ready[ld["order"]] = max(ready.get(ld["order"], r["end_at"] + post), r["end_at"] + post)
            loads.append(
                {
                    "order": ld["order"],
                    "order_no": d.order_no if d else ld.get("order_no", ld["order"]),
                    "customer": d.customer if d else "",
                    "product": d.product.object_id if d else ld.get("product"),
                    "product_code": d.product_code if d else ld.get("product_code", ""),
                    "qty": round(ld["qty"], 3),
                }
            )
        qty = sum(ld["qty"] for ld in r["loads"])
        run_rows.append(
            {
                "kiln": r["kiln"],
                "kiln_name": ctx.kiln_name(r["kiln"]),
                "run_no": r["run_no"],
                "start_at": _iso(ctx, r["start_at"]),
                "end_at": _iso(ctx, r["end_at"]),
                "unit": k.state.get("capacity_unit") if k else None,
                "capacity": cap or None,
                "qty": round(qty, 3),
                "fill_pct": round(qty / cap * 100, 1) if cap else None,
                "loads": loads,
            }
        )

    order_rows = []
    for d in sorted(ctx.demands.values(), key=lambda d: (d.due_date or date.max, d.order_no)):
        p = planned.get(d.id, 0.0)
        short = max(d.to_fire - p, 0.0)
        rd = ctx.local_date(ready[d.id]) if d.id in ready else None
        late = (rd - d.due_date).days if rd and d.due_date and rd > d.due_date else 0
        if short > EPS:
            text = f"计划期内排不下 {short:g} {d.unit}"
        elif late:
            text = f"预计晚 {late} 天"
        elif d.due_date is None:
            text = "没有交期"
        else:
            text = "按期"
        order_rows.append(
            {
                "order": d.id,
                "order_no": d.order_no,
                "customer": d.customer,
                "product": d.product.object_id,
                "product_code": d.product_code,
                "qty": d.qty,
                "unit": d.unit,
                "to_fire": round(d.to_fire, 3),
                "planned": round(p, 3),
                "short": round(short, 3),
                "due_date": d.due_date.isoformat() if d.due_date else None,
                "promised_date": d.promised_date.isoformat() if d.promised_date else None,
                "ready_date": rd.isoformat() if rd else None,
                "late_days": late,
                "status_text": text,
                "ok": short <= EPS and not late,
            }
        )

    kiln_rows = []
    for kid, k in ctx.kilns.items():
        mine = [r for r in runs if r["kiln"] == kid]
        hours = sum(
            (min(r["end_at"], ctx.end) - max(r["start_at"], ctx.start)).total_seconds() / 3600
            for r in mine
            if r["end_at"] > ctx.start
        )
        span = max(ctx.horizon - ctx.kiln_free.get(kid, 0), 0)
        cap = num(k.state.get("capacity"))
        fills = [sum(ld["qty"] for ld in r["loads"]) / cap * 100 for r in mine if cap]
        kiln_rows.append(
            {
                "kiln": kid,
                "name": ctx.kiln_name(kid),
                "capacity": cap,
                "unit": k.state.get("capacity_unit"),
                "cycle_hours": ctx.cycle(k),
                "free_from": _iso(ctx, ctx.at(ctx.kiln_free.get(kid, 0))),
                "runs": len(mine),
                "utilization_pct": round(hours / span * 100, 1) if span else None,
                "avg_fill_pct": round(sum(fills) / len(fills), 1) if fills else None,
            }
        )

    mp = await material_plan(session, ctx, runs)
    return {
        "data_mode": ctx.mode,
        "horizon_start": _iso(ctx, ctx.start),
        "horizon_end": _iso(ctx, ctx.end),
        "runs": run_rows,
        "orders": order_rows,
        "kilns": kiln_rows,
        "materials": mp,
        "warnings": list(dict.fromkeys(ctx.warnings + mp["warnings"])),
    }


async def draft_plan(session: AsyncSession, ctx: Context) -> dict[str, Any]:
    sol = _solve(ctx)
    out = await summarize(session, ctx, runs_from_solution(ctx, sol))
    out["solver"] = {"status": sol.status, "seconds": sol.seconds}
    if sol.status == "feasible":
        out["warnings"].insert(
            0, f"求解到时限（{ctx.cfg.solver.time_limit_s:g} 秒）还不是最优，可以再算一次或人工调整"
        )
    return out


# ---------------- 已确认的计划 ----------------


async def current_plan_obj(session: AsyncSession, tenant: str, mode: str):
    plans = [
        p for p in await _all(session, tenant, "kiln_plan", mode) if p.state.get("status") == "confirmed"
    ]
    return plans[-1] if plans else None


async def stored_runs(session: AsyncSession, tenant: str, plan_id: str) -> list[dict[str, Any]]:
    rows = await referrers(session, tenant, "planned_run", "plan", "kiln_plan", [plan_id])
    runs: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        s = r.state
        key = (s["kiln"], s["run_no"])
        run = runs.setdefault(
            key,
            {
                "kiln": s["kiln"],
                "run_no": s["run_no"],
                "start_at": datetime.fromisoformat(s["start_at"]),
                "end_at": datetime.fromisoformat(s["end_at"]),
                "loads": [],
            },
        )
        if s.get("order"):
            run["loads"].append({"order": s["order"], "qty": num(s.get("qty")), "product": s.get("product")})
    return sorted(runs.values(), key=lambda r: (r["start_at"], r["kiln"]))


async def current_plan(session: AsyncSession, ctx: Context) -> dict[str, Any] | None:
    plan = await current_plan_obj(session, ctx.tenant, ctx.mode)
    if plan is None:
        return None
    runs = await stored_runs(session, ctx.tenant, plan.object_id)
    # 计划里有、但现在已经不在待烧清单里的订单（已烧完、已关闭），只用来显示名称
    for r in runs:
        for ld in r["loads"]:
            if ld["order"] not in ctx.demands:
                o = await get(session, ctx.tenant, "sales_order", ld["order"])
                if o is not None:
                    ld["order_no"] = o.state.get("order_no") or o.object_id
    out = await summarize(session, ctx, runs)
    out["plan"] = {"id": plan.object_id, **plan.state}
    return out


# ---------------- 确认 ----------------


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("时间必须带时区")
    return v


class LoadIn(BaseModel):
    order: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0)


class RunIn(BaseModel):
    kiln: str = Field(min_length=1, max_length=64)
    run_no: str | None = Field(default=None, max_length=64)
    start_at: datetime
    end_at: datetime
    loads: list[LoadIn] = Field(min_length=1, max_length=50)

    _tz = field_validator("start_at", "end_at")(_aware)


class PlanIn(BaseModel):
    horizon_start: datetime
    horizon_end: datetime
    runs: list[RunIn] = Field(min_length=1, max_length=2000)
    promise: list[str] = Field(default_factory=list, max_length=500)  # 按这版计划承诺交期的订单
    note: str | None = Field(default=None, max_length=500)
    solver_status: str | None = Field(default=None, max_length=32)
    data_mode: Literal["real", "mock"] = "real"

    _tz = field_validator("horizon_start", "horizon_end")(_aware)


async def confirm_plan(
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    body: PlanIn,
    *,
    cfg: PlanningConfig,
    ocfg: OrdersConfig,
    tz: ZoneInfo,
) -> dict[str, Any]:
    """全有或全无（任何一行被守卫拒绝，整批回滚，由调用方的事务负责）。"""
    tenant = principal.tenant_id
    if body.horizon_end <= body.horizon_start:
        raise PlanningError("计划结束时间要晚于开始时间")
    plan_id = new_id("KP", tz)
    calc = Evidence(
        kind="calculation",
        ref=f"planning:solver:{body.solver_status or 'manual'}",
        note="排窑求解器建议，计划员核对后确认",
    )

    async def append(req: AppendRequest):
        try:
            return await ledger.append(session, principal, req)
        except ApprovalRequired as e:
            raise PlanConflict(f"命中审批规则（{e}），排窑计划不能整批确认") from e

    prev = await current_plan_obj(session, tenant, body.data_mode)
    await append(
        AppendRequest(
            type="pmc.kiln_plan_confirmed",
            object_id=plan_id,
            payload={
                "plan_no": plan_id,
                "horizon_start": body.horizon_start.isoformat(),
                "horizon_end": body.horizon_end.isoformat(),
                "status": "confirmed",
                "solver_status": body.solver_status or "manual",
                **({"note": body.note} if body.note else {}),
            },
            evidence=[calc],
            data_mode=body.data_mode,
        )
    )
    post = timedelta(hours=cfg.post_firing_hours)
    planned: dict[str, float] = {}
    ready: dict[str, date] = {}
    seq: dict[str, int] = {}
    n = 0
    for run in body.runs:
        kiln = await get(session, tenant, "kiln", run.kiln)
        if kiln is None:
            raise PlanningError(f"窑炉 {run.kiln} 不存在")
        seq[run.kiln] = seq.get(run.kiln, 0) + 1
        run_no = run.run_no or f"{kiln.state.get('name') or run.kiln}-{seq[run.kiln]:02d}"
        rd = (run.end_at + post).astimezone(tz).date()
        for ld in run.loads:
            order = await get(session, tenant, "sales_order", ld.order)
            if order is None:
                raise PlanningError(f"订单 {ld.order} 不存在")
            n += 1
            await append(
                AppendRequest(
                    type="pmc.run_planned",
                    object_id=f"{plan_id}-{n:03d}",
                    payload={
                        "plan": plan_id,
                        "kiln": run.kiln,
                        "run_no": run_no,
                        "start_at": run.start_at.isoformat(),
                        "end_at": run.end_at.isoformat(),
                        "product": order.state.get("product"),
                        "order": ld.order,
                        "qty": ld.qty,
                        "unit": order.state.get("unit"),
                        "ready_date": rd.isoformat(),
                    },
                    data_mode=body.data_mode,
                )
            )
            planned[ld.order] = planned.get(ld.order, 0.0) + ld.qty
            ready[ld.order] = max(ready.get(ld.order, rd), rd)
    if prev is not None:
        await append(
            AppendRequest(
                type="pmc.kiln_plan_superseded",
                object_id=prev.object_id,
                payload={"status": "superseded"},
                data_mode=body.data_mode,
            )
        )

    promised, warnings = [], []
    for oid in dict.fromkeys(body.promise):
        order = await get(session, tenant, "sales_order", oid)
        if order is None or order.state.get("status") in CLOSED:
            warnings.append(f"订单 {oid} 不存在或已关闭，没有承诺交期")
            continue
        d = await order_demand(session, tenant, ocfg, order)
        label = order.state.get("order_no") or oid
        if d is None or oid not in ready:
            warnings.append(f"订单 {label} 不在这版计划里，没有承诺交期")
            continue
        if planned[oid] + EPS < d.to_fire:
            warnings.append(f"订单 {label} 只排了 {planned[oid]:g} / {d.to_fire:g} {d.unit}，没有承诺交期")
            continue
        await append(
            AppendRequest(
                type="pmc.delivery_promised",
                object_id=oid,
                payload={"promised_date": ready[oid].isoformat()},
                evidence=[Evidence(kind="calculation", ref=f"kiln_plan:{plan_id}", note="按排窑计划承诺")],
                data_mode=body.data_mode,
            )
        )
        promised.append({"order": oid, "order_no": label, "promised_date": ready[oid].isoformat()})
    return {
        "plan_id": plan_id,
        "plan_no": plan_id,
        "rows": n,
        "superseded": prev.object_id if prev else None,
        "promised": promised,
        "warnings": warnings,
    }


# ---------------- 交期评估 ----------------


class CandidateIn(BaseModel):
    """假设的新订单（还没下单，问“能不能接”）。"""

    product: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0)
    unit: str | None = Field(default=None, max_length=16)
    due_date: date | None = None


async def evaluate(
    session: AsyncSession,
    *,
    tenant: str,
    cfg: PlanningConfig,
    ocfg: OrdersConfig,
    tz: ZoneInfo,
    now: datetime,
    mode: str = "real",
    order_id: str | None = None,
    candidate: CandidateIn | None = None,
) -> dict[str, Any]:
    kw = {"tenant": tenant, "cfg": cfg, "ocfg": ocfg, "tz": tz, "now": now, "mode": mode}
    if order_id:
        order = await get(session, tenant, "sales_order", order_id)
        if order is None:
            raise PlanNotFound(f"订单 {order_id} 不存在")
        if order.state.get("status") in CLOSED:
            raise PlanConflict("订单已关闭或取消")
        mode = kw["mode"] = order.data_mode
        target = order_id
        base_ctx = await load_context(session, exclude={order_id}, **kw)
        with_ctx = await load_context(session, **kw)
        if target not in with_ctx.demands:
            d = await order_demand(session, tenant, ocfg, order)
            return {
                "target": {"order": order_id, "order_no": d.order_no if d else order_id},
                "verdict": "这个订单已经全部装窑或发货，不需要再排窑。",
                "ok": True,
                "impacts": [],
                "shortages": [],
                "warnings": [],
            }
    elif candidate is not None:
        product = await get(session, tenant, "product", candidate.product)
        if product is None or product.data_mode != mode:
            raise PlanNotFound(f"产品 {candidate.product} 不存在")
        target = NEW
        base_ctx = await load_context(session, **kw)
        with_ctx = await load_context(session, **kw)
        add_demand(
            with_ctx,
            Demand(
                id=NEW,
                order_no="新订单",
                customer="",
                product=product,
                qty=candidate.qty,
                unit=candidate.unit or str(product.state.get("unit") or ""),
                due_date=candidate.due_date,
                data_mode=mode,
            ),
        )
    else:
        raise PlanningError("要给出订单，或者假设订单的产品和数量")

    base = await draft_plan(session, base_ctx)
    plan = await draft_plan(session, with_ctx)
    row = next(r for r in plan["orders"] if r["order"] == target)
    before = {r["order"]: r for r in base["orders"]}
    impacts = []
    for r in plan["orders"]:
        b = before.get(r["order"])
        if r["order"] == target or b is None:
            continue
        later = r["ready_date"] and b["ready_date"] and r["ready_date"] > b["ready_date"]
        if later or r["short"] > b["short"] + EPS or r["late_days"] > b["late_days"]:
            impacts.append(
                {
                    "order": r["order"],
                    "order_no": r["order_no"],
                    "customer": r["customer"],
                    "due_date": r["due_date"],
                    "ready_before": b["ready_date"],
                    "ready_after": r["ready_date"],
                    "late_days_before": b["late_days"],
                    "late_days_after": r["late_days"],
                    "short_after": r["short"],
                }
            )
    label = row["order_no"]
    shortages = [m for m in plan["materials"]["shortages"] if label in m["orders"]]
    if row["short"] > EPS:
        verdict = (
            f"按现有窑炉和在手订单，{cfg.horizon_days} 天内排不下：还差 {row['short']:g} {row['unit']}。"
        )
        ok = False
    elif row["late_days"]:
        verdict = f"预计 {row['ready_date']} 可发货，比交期 {row['due_date']} 晚 {row['late_days']} 天。"
        ok = False
    else:
        verdict = f"预计 {row['ready_date']} 可发货" + (
            f"，能赶上交期 {row['due_date']}。" if row["due_date"] else "（没有交期）。"
        )
        ok = True
    if impacts:
        verdict += f"会让 {len(impacts)} 个在手订单往后推。"
        ok = False if any(i["late_days_after"] > i["late_days_before"] for i in impacts) else ok
    if shortages:
        verdict += f"缺 {len(shortages)} 种原料。"
    return {
        "target": row,
        "verdict": verdict,
        "ok": ok,
        "impacts": impacts,
        "shortages": shortages,
        "warnings": plan["warnings"],
        "solver": {"base": base["solver"], "with": plan["solver"]},
    }
