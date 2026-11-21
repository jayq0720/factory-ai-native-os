"""EHS 安全环保只读 Agent 工具（W25），供 ehs_officer 等 Skill 调用。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.ehs import service as hs
from fos.ehs.config import EhsConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> EhsConfig:
    cfg = ctx.extras.get("ehs")
    if cfg is None:
        raise ToolError("当前租户未启用 EHS 安全环保模块")
    return cfg


def _today(ctx: ToolContext):
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return utcnow().astimezone(tz).date()


class EmptyArgs(BaseModel):
    pass


class StatusArgs(BaseModel):
    status: str | None = Field(default=None, max_length=16)


class EnvArgs(BaseModel):
    kind: str | None = Field(default=None, max_length=16)


@tool("ehs.hazards", "查询安全隐患台账、超期隐患与重大隐患未闭环清单", StatusArgs)
async def tool_hazards(ctx: ToolContext, a: StatusArgs) -> dict[str, Any]:
    ctx.require("ehs.hazard:read")
    return await hs.hazards_board(ctx.session, ctx.tenant_id, _cfg(ctx), _today(ctx), status=a.status)


@tool("ehs.permits", "查询动火、受限空间等特种作业票台账与执行状态", StatusArgs)
async def tool_permits(ctx: ToolContext, a: StatusArgs) -> dict[str, Any]:
    ctx.require("ehs.permit:read")
    _cfg(ctx)
    return await hs.permits_board(ctx.session, ctx.tenant_id, status=a.status)


@tool("ehs.env_summary", "查询粉尘、NOx、SO2、噪声、VOC 等环保排放监测与达标率", EnvArgs)
async def tool_env_summary(ctx: ToolContext, a: EnvArgs) -> dict[str, Any]:
    ctx.require("ehs.env:read")
    return await hs.env_board(ctx.session, ctx.tenant_id, _cfg(ctx), kind=a.kind)


@tool("ehs.summary", "确定性汇总 EHS 隐患闭环率、特种作业票、安全事故与环保达标率", EmptyArgs)
async def tool_ehs_summary(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("ehs.hazard:read")
    return await hs.ehs_summary(ctx.session, ctx.tenant_id, _cfg(ctx), _today(ctx))
