"""质量体系 Agent 只读工具（供质检员 Skill 调用）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.quality.config import QualityConfig
from fos.quality.service import instruments_board, iso_audit_pack, ncr_board, pending_tasks
from fos.tools.registry import ToolContext, tool


class EmptyArgs(BaseModel):
    pass


class NcrListArgs(BaseModel):
    status: str | None = Field(default=None, description="按状态过滤：open / dispositioned / closed")


class IsoSummaryArgs(BaseModel):
    days: int = Field(default=30, ge=1, le=365, description="统计最近多少天")


def _cfg(ctx: ToolContext) -> QualityConfig:
    return ctx.extras.get("quality") or QualityConfig()


def _today(ctx: ToolContext):
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    return utcnow().astimezone(tz).date()


@tool("quality.pending", "查询当前待检来料批次与待出厂检验生产批次", EmptyArgs)
async def tool_pending(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("quality.inspection:read")
    return await pending_tasks(ctx.session, ctx.tenant_id)


@tool("quality.ncrs", "查询不合格品报告（NCR）列表、处置状态与 8D 进度", NcrListArgs)
async def tool_ncrs(ctx: ToolContext, a: NcrListArgs) -> dict[str, Any]:
    ctx.require("quality.ncr:read")
    rows = await ncr_board(ctx.session, ctx.tenant_id, status=a.status)
    return {"items": rows[:30], "total": len(rows)}


@tool("quality.instruments", "查询计量器具校准状态（正常 / 即将到期 / 已超期）", EmptyArgs)
async def tool_instruments(ctx: ToolContext, _a: EmptyArgs) -> dict[str, Any]:
    ctx.require("quality.measuring_instrument:read")
    return await instruments_board(ctx.session, ctx.tenant_id, _cfg(ctx), _today(ctx))


@tool(
    "quality.iso_summary",
    "按天数汇总 ISO 9001 质量 KPI（来料/出厂合格率、NCR 关闭率、校准率）",
    IsoSummaryArgs,
)
async def tool_iso_summary(ctx: ToolContext, a: IsoSummaryArgs) -> dict[str, Any]:
    ctx.require("quality.inspection:read")
    tz = ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")
    end_d = _today(ctx)
    start_d = end_d - timedelta(days=a.days - 1)
    pack = await iso_audit_pack(ctx.session, ctx.tenant_id, _cfg(ctx), start_d, end_d, tz)
    return {"period": pack["period"], "kpis": pack["kpis"]}
