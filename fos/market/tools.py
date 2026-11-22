"""市场商机与经营决策只读 Agent 工具（W27c），供 executive_advisor 等 Skill 调用。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.market import service as ms
from fos.market.config import MarketConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> MarketConfig:
    cfg = ctx.extras.get("market")
    if cfg is None:
        raise ToolError("当前租户未启用市场商机与经营决策模块")
    return cfg


def _today(ctx: ToolContext):
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return utcnow().astimezone(tz).date()


class EmptyArgs(BaseModel):
    pass


class StageArgs(BaseModel):
    stage: str | None = Field(default=None, max_length=16)


@tool("market.cockpit", "确定性汇总全厂跨部门经营决策驾驶舱 KPI 指标", EmptyArgs)
async def tool_cockpit(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("market.lead:read")
    _cfg(ctx)
    return await ms.executive_cockpit(ctx.session, ctx.tenant_id, _today(ctx))


@tool("market.funnel", "查询市场商机漏斗、加权预测额、赢单率与分阶段商机清单", StageArgs)
async def tool_funnel(ctx: ToolContext, a: StageArgs) -> dict[str, Any]:
    ctx.require("market.lead:read")
    _cfg(ctx)
    return await ms.funnel_board(ctx.session, ctx.tenant_id, stage=a.stage)


@tool("market.intel", "查询竞争对手报价水平与区域市场行情情报", EmptyArgs)
async def tool_intel(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("market.intel:read")
    _cfg(ctx)
    return await ms.intel_board(ctx.session, ctx.tenant_id)
