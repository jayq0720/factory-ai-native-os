"""筑炉施工与烘炉验收只读 Agent 工具（W27b），供 site_supervisor 等 Skill 调用。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from fos.construction import service as cs
from fos.construction.config import ConstructionConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> ConstructionConfig:
    cfg = ctx.extras.get("construction")
    if cfg is None:
        raise ToolError("当前租户未启用筑炉施工管理模块")
    return cfg


class StatusArgs(BaseModel):
    status: str | None = Field(default=None, max_length=16)


class SiteFilterArgs(BaseModel):
    site: str | None = Field(default=None, max_length=64)


@tool("construction.sites", "查询筑炉施工项目台账、累计砌筑进度与验收状态", StatusArgs)
async def tool_sites(ctx: ToolContext, a: StatusArgs) -> dict[str, Any]:
    ctx.require("construction.site:read")
    _cfg(ctx)
    return await cs.sites_board(ctx.session, ctx.tenant_id, status=a.status)


@tool("construction.logs", "查询现场施工日报、每日人数与砌筑完成吨位", SiteFilterArgs)
async def tool_logs(ctx: ToolContext, a: SiteFilterArgs) -> dict[str, Any]:
    ctx.require("construction.log:read")
    _cfg(ctx)
    return await cs.logs_board(ctx.session, ctx.tenant_id, site=a.site)


@tool("construction.dryout", "查询烘炉升温实测记录、温差与升温速率告警", SiteFilterArgs)
async def tool_dryout(ctx: ToolContext, a: SiteFilterArgs) -> dict[str, Any]:
    ctx.require("construction.dryout:read")
    _cfg(ctx)
    return await cs.dryout_board(ctx.session, ctx.tenant_id, site=a.site)
