"""全厂统一 Watchdog 巡检引擎（W28）：单次幂等触发全部启用模块的到期提醒与过期审批清理。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, date
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Approval, utcnow
from fos.eam.service import remind_pm_due
from fos.ehs.service import remind_overdue_hazards
from fos.finance.service import remind_overdue_ar_ap
from fos.hr.service import remind_expiring_certs
from fos.quality.service import remind_calibration_due

if TYPE_CHECKING:
    from fos.services import Services

logger = logging.getLogger(__name__)


def _created_count(res: dict[str, Any]) -> int:
    if isinstance(res.get("count"), int):
        return int(res["count"])
    c = res.get("created")
    return len(c) if isinstance(c, list) else int(c or 0)


async def expire_stale_approvals(session: AsyncSession, tenant_id: str) -> int:
    """将已超过 expires_at 的待审批单标记为 expired，避免悬空审批阻塞流程。"""
    now = utcnow()
    rows = (
        await session.scalars(
            select(Approval).where(
                Approval.tenant_id == tenant_id,
                Approval.status == "pending",
            )
        )
    ).all()
    expired = 0
    for ap in rows:
        exp = ap.expires_at
        if exp is None:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= now:
            ap.status = "expired"
            ap.updated_at = now
            expired += 1
    return expired


async def run_tenant_watchdog(
    session: AsyncSession,
    svc: Services,
    tenant_id: str,
    today: date,
    *,
    mode: str = "real",
) -> dict[str, Any]:
    """执行单租户全模块幂等巡检：计量校准、财务逾期、设备保养、EHS 隐患、特种证照与审批超时。"""
    modules: dict[str, int] = {}
    if cfg := svc.quality.get(tenant_id):
        modules["quality_calibration"] = _created_count(
            await remind_calibration_due(session, tenant_id, cfg, today, mode=mode)
        )
    if cfg := svc.finance.get(tenant_id):
        modules["finance_overdue"] = _created_count(
            await remind_overdue_ar_ap(session, tenant_id, cfg, today, mode=mode)
        )
    if cfg := svc.eam.get(tenant_id):
        modules["eam_pm"] = _created_count(await remind_pm_due(session, tenant_id, cfg, today, mode=mode))
    if cfg := svc.ehs.get(tenant_id):
        modules["ehs_hazards"] = _created_count(
            await remind_overdue_hazards(session, tenant_id, cfg, today, mode=mode)
        )
    if cfg := svc.hr.get(tenant_id):
        modules["hr_certs"] = _created_count(
            await remind_expiring_certs(session, tenant_id, cfg, today, mode=mode)
        )
    expired_approvals = await expire_stale_approvals(session, tenant_id)
    await session.flush()
    return {
        "tenant_id": tenant_id,
        "date": today.isoformat(),
        "data_mode": mode,
        "modules": modules,
        "total_todos_created": sum(modules.values()),
        "expired_approvals": expired_approvals,
    }


async def watchdog_loop(
    svc: Services,
    tenant_ids: list[str] | None = None,
    *,
    interval_seconds: float = 1800.0,
    stop_event: asyncio.Event | None = None,
    max_cycles: int | None = None,
) -> int:
    """周期性全厂 Watchdog 守护循环：按固定间隔幂等巡检所有目标租户，异常隔离不中断进程。"""
    cycles = 0
    targets = tenant_ids or list(svc.registry.ids())
    while True:
        today = date.today()
        for tid in targets:
            try:
                async with svc.sessionmaker() as session, session.begin():
                    res = await run_tenant_watchdog(session, svc, tid, today)
                logger.info(
                    "watchdog %s: todos=+%s expired_approvals=%s",
                    tid,
                    res["total_todos_created"],
                    res["expired_approvals"],
                )
            except Exception:
                logger.exception("watchdog 巡检租户 %s 失败，将在下个周期重试", tid)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return cycles
        if stop_event is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            if stop_event.is_set():
                return cycles
        else:
            await asyncio.sleep(interval_seconds)
