"""财务与成本只读 Agent 工具（W23），供 finance_assistant 等 Skill 调用。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.finance import service as fs
from fos.finance.config import FinanceConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> FinanceConfig:
    cfg = ctx.extras.get("finance")
    if cfg is None:
        raise ToolError("当前租户未启用财务模块")
    return cfg


def _today(ctx: ToolContext):
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return utcnow().astimezone(tz).date()


class EmptyArgs(BaseModel):
    pass


class BatchCostArgs(BaseModel):
    batch_id: str = Field(min_length=1, max_length=64)


class VoucherListArgs(BaseModel):
    status: str | None = Field(default=None, max_length=16)


@tool("finance.ar_summary", "查询当前应收账款总额、已回款、未收余额与账龄分布", EmptyArgs)
async def tool_ar_summary(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("finance.ar:read")
    _cfg(ctx)
    return await fs.ar_board(ctx.session, ctx.tenant_id, _today(ctx))


@tool("finance.ap_summary", "查询当前应付账款总额、已付款、未付余额与逾期汇总", EmptyArgs)
async def tool_ap_summary(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("finance.ap:read")
    _cfg(ctx)
    return await fs.ap_board(ctx.session, ctx.tenant_id, _today(ctx))


@tool("finance.batch_cost", "按领料、窑次能耗与制造费用确定性核算指定生产批次成本", BatchCostArgs)
async def tool_batch_cost(ctx: ToolContext, a: BatchCostArgs) -> dict[str, Any]:
    ctx.require("finance.cost:read")
    try:
        return await fs.batch_cost_report(ctx.session, ctx.tenant_id, _cfg(ctx), a.batch_id)
    except fs.FinanceError as e:
        raise ToolError(str(e)) from e


@tool("finance.vouchers", "查询待导出/已导出/已过账的会计凭证草稿列表", VoucherListArgs)
async def tool_vouchers(ctx: ToolContext, a: VoucherListArgs) -> dict[str, Any]:
    ctx.require("finance.voucher:read")
    _cfg(ctx)
    rows = await fs.vouchers_board(ctx.session, ctx.tenant_id, status=a.status)
    return {"items": rows[:30], "total": len(rows)}
