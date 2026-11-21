"""研发配方与试验只读 Agent 工具（W27），供 rnd_engineer 等 Skill 调用。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from fos.rnd import service as rs
from fos.rnd.config import RndConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> RndConfig:
    cfg = ctx.extras.get("rnd")
    if cfg is None:
        raise ToolError("当前租户未启用研发配方管理模块")
    return cfg


class StatusArgs(BaseModel):
    status: str | None = Field(default=None, max_length=16)


class ProjectFilterArgs(BaseModel):
    project: str | None = Field(default=None, max_length=64)


class FormulaFilterArgs(BaseModel):
    formula: str | None = Field(default=None, max_length=64)


@tool("rnd.projects", "查询研发项目台账、目标理化指标下限与推进状态", StatusArgs)
async def tool_projects(ctx: ToolContext, a: StatusArgs) -> dict[str, Any]:
    ctx.require("rnd.project:read")
    _cfg(ctx)
    return await rs.projects_board(ctx.session, ctx.tenant_id, status=a.status)


@tool("rnd.formulas", "查询耐火材料配方版本、理论 Al2O3、吨原料成本与定型状态", ProjectFilterArgs)
async def tool_formulas(ctx: ToolContext, a: ProjectFilterArgs) -> dict[str, Any]:
    ctx.require("rnd.formula:read")
    _cfg(ctx)
    return await rs.formulas_board(ctx.session, ctx.tenant_id, project=a.project)


@tool("rnd.trials", "查询实验室小试/中试/窑炉试烧理化检测结果与确定性达标率", FormulaFilterArgs)
async def tool_trials(ctx: ToolContext, a: FormulaFilterArgs) -> dict[str, Any]:
    ctx.require("rnd.trial:read")
    _cfg(ctx)
    return await rs.trials_board(ctx.session, ctx.tenant_id, formula=a.formula)
