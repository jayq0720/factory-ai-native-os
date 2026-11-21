"""设备管理（EAM）只读 Agent 工具（W24），供 equipment_manager 等 Skill 调用。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.eam import service as es
from fos.eam.config import EamConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> EamConfig:
    cfg = ctx.extras.get("eam")
    if cfg is None:
        raise ToolError("当前租户未启用设备管理模块")
    return cfg


def _today(ctx: ToolContext):
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return utcnow().astimezone(tz).date()


class EmptyArgs(BaseModel):
    pass


class WorkOrderArgs(BaseModel):
    status: str | None = Field(default=None, max_length=16)
    equipment: str | None = Field(default=None, max_length=64)


class ReliabilityArgs(BaseModel):
    window_days: int | None = Field(default=None, ge=1, le=365)
    equipment: str | None = Field(default=None, max_length=64)


@tool("eam.equipment", "查询设备台账、运行/维保/报废状态统计", EmptyArgs)
async def tool_equipment(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("eam.equipment:read")
    _cfg(ctx)
    return await es.equipment_board(ctx.session, ctx.tenant_id)


@tool("eam.pm_due", "查询设备预防性保养计划与超期/即将到期清单", EmptyArgs)
async def tool_pm_due(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("eam.maintenance:read")
    return await es.pm_board(ctx.session, ctx.tenant_id, _cfg(ctx), _today(ctx))


@tool("eam.work_orders", "查询设备维修/保养工单列表与进度", WorkOrderArgs)
async def tool_work_orders(ctx: ToolContext, a: WorkOrderArgs) -> dict[str, Any]:
    ctx.require("eam.work_order:read")
    _cfg(ctx)
    rows = await es.work_orders_board(ctx.session, ctx.tenant_id, status=a.status, equipment=a.equipment)
    return {"items": rows[:30], "total": len(rows)}


@tool("eam.reliability", "确定性计算指定窗口内的设备可用率、停机时长、MTBF 与 MTTR", ReliabilityArgs)
async def tool_reliability(ctx: ToolContext, a: ReliabilityArgs) -> dict[str, Any]:
    ctx.require("eam.work_order:read")
    return await es.reliability_report(
        ctx.session,
        ctx.tenant_id,
        _cfg(ctx),
        window_days=a.window_days,
        equipment=a.equipment,
    )
