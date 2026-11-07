"""销售助理用的只读工具：订单进度、订单看板（数字由 fos/orders/progress.py 确定性计算）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.orders.config import OrdersConfig
from fos.orders.data import find_by
from fos.orders.progress import order_board, order_progress
from fos.tools.registry import ToolContext, ToolError, tool

PERM = "sales.sales_order:read"


def _env(ctx: ToolContext):
    cfg = ctx.extras.get("orders") or OrdersConfig()
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return cfg, datetime.now(tz).date()


class ProgressArgs(BaseModel):
    order: str = Field(min_length=1, max_length=64, description="订单编号或订单号")


@tool(
    "orders.progress",
    "查一个订单的交付进度：排产、完工、入库、发货、签收数量，交期风险（数字由代码计算，直接引用）",
    ProgressArgs,
)
async def progress(ctx: ToolContext, a: ProgressArgs) -> dict[str, Any]:
    ctx.require(PERM)
    hits = await find_by(ctx.session, ctx.tenant_id, "sales_order", "order_no", a.order)
    if not hits:
        raise ToolError(f"没有找到订单 {a.order}")
    if len(hits) > 1:
        raise ToolError(f"订单号 {a.order} 对应多个订单：{[h.object_id for h in hits[:5]]}")
    cfg, today = _env(ctx)
    return await order_progress(ctx.session, ctx.ontology, cfg, ctx.user, hits[0], today)


class BoardArgs(BaseModel):
    risky_only: bool = Field(default=False, description="只看有交期风险的订单")
    customer: str | None = Field(default=None, max_length=64, description="客户对象 id")
    limit: int = Field(default=20, ge=1, le=50)


@tool("orders.board", "在手订单看板：每个订单的进度和交期风险，有风险的排前面", BoardArgs)
async def board(ctx: ToolContext, a: BoardArgs) -> dict[str, Any]:
    ctx.require(PERM)
    cfg, today = _env(ctx)
    rows = await order_board(
        ctx.session, ctx.ontology, cfg, ctx.user, today, only_risky=a.risky_only, customer=a.customer
    )
    slim = [
        {
            k: p[k]
            for k in ("order_id", "order_no", "customer", "product", "qty", "unit", "due_date", "stage_text")
        }
        | {"quantities": p["quantities"], "risks": p["risks"]}
        for p in rows[: a.limit]
    ]
    return {"date": today.isoformat(), "count": len(rows), "orders": slim}
