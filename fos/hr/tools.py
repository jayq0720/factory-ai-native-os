"""人力、排班与薪酬只读 Agent 工具（W26），供 hr_assistant 等 Skill 调用。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.hr import service as hrs
from fos.hr.config import HrConfig
from fos.tools.registry import ToolContext, ToolError, tool


def _cfg(ctx: ToolContext) -> HrConfig:
    cfg = ctx.extras.get("hr")
    if cfg is None:
        raise ToolError("当前租户未启用人力排班与薪酬模块")
    return cfg


def _today(ctx: ToolContext):
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return utcnow().astimezone(tz).date()


class EmptyArgs(BaseModel):
    pass


class StatusArgs(BaseModel):
    status: str | None = Field(default=None, max_length=16)


class MonthArgs(BaseModel):
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")


@tool("hr.employees", "查询员工花名册、部门岗位与在职人数统计", StatusArgs)
async def tool_employees(ctx: ToolContext, a: StatusArgs) -> dict[str, Any]:
    ctx.require("hr.employee:read")
    _cfg(ctx)
    return await hrs.employees_board(ctx.session, ctx.tenant_id, status=a.status)


@tool("hr.certs", "查询特种作业证照台账与过期/临期复审预警清单", EmptyArgs)
async def tool_certs(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("hr.cert:read")
    return await hrs.certs_board(ctx.session, ctx.tenant_id, _cfg(ctx), _today(ctx))


@tool("hr.roster", "查询指定月份班次排班、出勤工时、加班与计件产量记录", MonthArgs)
async def tool_roster(ctx: ToolContext, a: MonthArgs) -> dict[str, Any]:
    ctx.require("hr.roster:read")
    _cfg(ctx)
    month = a.month or _today(ctx).strftime("%Y-%m")
    return await hrs.roster_board(ctx.session, ctx.tenant_id, month=month)


@tool("hr.payroll", "确定性核算指定月份员工出勤、加班费、计件工资与应发合计", MonthArgs)
async def tool_payroll(ctx: ToolContext, a: MonthArgs) -> dict[str, Any]:
    ctx.require("hr.roster:read")
    month = a.month or _today(ctx).strftime("%Y-%m")
    return await hrs.payroll_report(ctx.session, ctx.tenant_id, _cfg(ctx), month)
