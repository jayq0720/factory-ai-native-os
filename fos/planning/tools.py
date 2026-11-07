"""计划员 Skill 用的只读工具：当前排窑计划、交期评估、原料缺口（数字全部由求解器和确定性代码算）。"""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.orders.config import OrdersConfig
from fos.orders.data import find_by
from fos.planning.demand import load_context
from fos.planning.service import CandidateIn, PlanningError, current_plan, evaluate
from fos.tools.registry import ToolContext, ToolError, tool

PERM = "pmc.kiln_plan:read"


def _env(ctx: ToolContext):
    cfg = ctx.extras.get("planning")
    if cfg is None:
        raise ToolError("这个租户没有开计划排窑模块")
    return cfg, ctx.extras.get("orders") or OrdersConfig(), ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")


class NoArgs(BaseModel):
    pass


@tool(
    "planning.current", "当前确认的排窑计划：各窑次时间、装哪些订单、订单预计可发货日期、窑炉利用率", NoArgs
)
async def current(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    ctx.require(PERM)
    cfg, ocfg, tz = _env(ctx)
    c = await load_context(ctx.session, tenant=ctx.tenant_id, cfg=cfg, ocfg=ocfg, tz=tz, now=utcnow())
    p = await current_plan(ctx.session, c)
    if p is None:
        return {"plan": None, "message": "还没有确认的排窑计划"}
    return {
        "plan": {k: p["plan"].get(k) for k in ("plan_no", "horizon_start", "horizon_end", "solver_status")},
        "runs": p["runs"][:30],
        "orders": p["orders"],
        "kilns": p["kilns"],
        "warnings": p["warnings"],
    }


@tool("planning.shortages", "按当前排窑计划算的原料需求和缺口（什么料、缺多少、哪天起要用）", NoArgs)
async def shortages(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    ctx.require(PERM)
    cfg, ocfg, tz = _env(ctx)
    c = await load_context(ctx.session, tenant=ctx.tenant_id, cfg=cfg, ocfg=ocfg, tz=tz, now=utcnow())
    p = await current_plan(ctx.session, c)
    if p is None:
        return {"plan": None, "message": "还没有确认的排窑计划"}
    return {"plan_no": p["plan"]["plan_no"], **p["materials"]}


class EvalArgs(BaseModel):
    order: str | None = Field(default=None, max_length=64, description="已有订单的编号或订单号")
    product: str | None = Field(default=None, max_length=64, description="假设的新订单：产品对象 id 或牌号")
    qty: float | None = Field(default=None, gt=0, description="假设的新订单数量")
    unit: str | None = Field(default=None, max_length=16)
    due_date: date | None = Field(default=None, description="假设的新订单交期 YYYY-MM-DD")


@tool(
    "planning.evaluate",
    "交期评估：把订单（已有的，或假设的产品+数量+交期）放进排窑求解器重排一次，"
    "返回预计可发货日期、能否按期、会推迟哪些在手订单、缺什么原料",
    EvalArgs,
)
async def evaluate_tool(ctx: ToolContext, a: EvalArgs) -> dict[str, Any]:
    ctx.require(PERM)
    cfg, ocfg, tz = _env(ctx)
    order_id = None
    cand = None
    if a.order:
        hits = await find_by(ctx.session, ctx.tenant_id, "sales_order", "order_no", a.order)
        if len(hits) != 1:
            raise ToolError(f"订单 {a.order} 没找到或对应多个订单")
        order_id = hits[0].object_id
    elif a.product and a.qty:
        hits = await find_by(ctx.session, ctx.tenant_id, "product", "code", a.product)
        if len(hits) != 1:
            raise ToolError(f"产品 {a.product} 没找到或对应多个产品")
        cand = CandidateIn(product=hits[0].object_id, qty=a.qty, unit=a.unit, due_date=a.due_date)
    else:
        raise ToolError("要给出订单，或者产品和数量")
    try:
        return await evaluate(
            ctx.session,
            tenant=ctx.tenant_id,
            cfg=cfg,
            ocfg=ocfg,
            tz=tz,
            now=utcnow(),
            order_id=order_id,
            candidate=cand,
        )
    except PlanningError as e:
        raise ToolError(str(e)) from e
