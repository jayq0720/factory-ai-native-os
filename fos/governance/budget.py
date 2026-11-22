"""按租户 LLM Token 预算租约核算与硬熔断治理（方案 2.6 / 11）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Run, utcnow
from fos.tenancy.config import TenantConfig


def _window_starts_utc(tz_name: str, now: datetime) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name or "Asia/Shanghai")
    local = now.astimezone(tz)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return day_start, month_start


async def _tokens_since(session: AsyncSession, tenant_id: str, since: datetime) -> int:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Run.prompt_tokens), 0),
                func.coalesce(func.sum(Run.completion_tokens), 0),
            ).where(Run.tenant_id == tenant_id, Run.created_at >= since)
        )
    ).one()
    return int(row[0] or 0) + int(row[1] or 0)


def evaluate_budget(
    *,
    tenant_id: str,
    daily_used: int,
    daily_limit: int,
    monthly_used: int,
    monthly_limit: int,
    warn_ratio: float,
) -> dict[str, Any]:
    """确定性判定租户当前预算租约状态（ok / warn / exceeded）与是否允许继续调用 LLM。"""
    daily_ratio = (daily_used / daily_limit) if daily_limit > 0 else 0.0
    monthly_ratio = (monthly_used / monthly_limit) if monthly_limit > 0 else 0.0
    daily_exceeded = daily_limit > 0 and daily_used >= daily_limit
    monthly_exceeded = monthly_limit > 0 and monthly_used >= monthly_limit
    allowed = not (daily_exceeded or monthly_exceeded)

    if not allowed:
        status = "exceeded"
        reason = "日度 Token 预算已耗尽" if daily_exceeded else "月度 Token 预算已耗尽"
    elif daily_ratio >= warn_ratio or monthly_ratio >= warn_ratio:
        status = "warn"
        reason = "已达到预算预警阈值"
    else:
        status = "ok"
        reason = "预算充足"

    return {
        "tenant_id": tenant_id,
        "status": status,
        "allowed": allowed,
        "reason": reason,
        "daily_used": daily_used,
        "daily_limit": daily_limit,
        "daily_pct": round(daily_ratio * 100.0, 2),
        "monthly_used": monthly_used,
        "monthly_limit": monthly_limit,
        "monthly_pct": round(monthly_ratio * 100.0, 2),
        "warn_ratio": warn_ratio,
    }


async def check_tenant_budget(
    session: AsyncSession,
    tenant: TenantConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """查询数据库并评估租户当日与当月 LLM Token 预算租约。"""
    ref = now or utcnow()
    day_start, month_start = _window_starts_utc(tenant.timezone, ref)
    daily_used = await _tokens_since(session, tenant.id, day_start)
    monthly_used = await _tokens_since(session, tenant.id, month_start)
    b = tenant.budget
    return evaluate_budget(
        tenant_id=tenant.id,
        daily_used=daily_used,
        daily_limit=b.daily_tokens,
        monthly_used=monthly_used,
        monthly_limit=b.monthly_tokens,
        warn_ratio=b.warn_ratio,
    )
