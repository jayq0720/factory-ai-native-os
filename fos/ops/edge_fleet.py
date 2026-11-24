"""厂端网关车队在线状态与窑炉未闭环告警聚合（供 Ops API 与 Prometheus 导出）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import EdgeBatch, EdgeCommandRecord, KilnAlarm, utcnow


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def edge_fleet_summary(
    session: AsyncSession,
    tenant_id: str,
    *,
    online_window_s: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    """聚合单租户全部厂端网关的最近心跳、在线状态、待执行命令与未闭环窑炉告警。"""
    ref_now = _as_utc(now or utcnow())
    window = max(30, int(online_window_s))

    q_batches = (
        select(
            EdgeBatch.edge_id,
            func.max(EdgeBatch.received_at),
            func.max(EdgeBatch.seq),
            func.count(EdgeBatch.id),
        )
        .where(EdgeBatch.tenant_id == tenant_id)
        .group_by(EdgeBatch.edge_id)
        .order_by(EdgeBatch.edge_id.asc())
    )
    batch_rows = (await session.execute(q_batches)).all()

    q_cmds = (
        select(EdgeCommandRecord.edge_id, func.count(EdgeCommandRecord.id))
        .where(
            EdgeCommandRecord.tenant_id == tenant_id,
            EdgeCommandRecord.status.in_(("pending", "sent")),
        )
        .group_by(EdgeCommandRecord.edge_id)
    )
    cmd_map = {str(r[0]): int(r[1]) for r in (await session.execute(q_cmds)).all()}

    q_alarms = (
        select(KilnAlarm.edge_id, func.count(KilnAlarm.id))
        .where(KilnAlarm.tenant_id == tenant_id, KilnAlarm.status == "open")
        .group_by(KilnAlarm.edge_id)
    )
    alarm_rows = (await session.execute(q_alarms)).all()
    open_alarms_total = sum(int(r[1]) for r in alarm_rows)
    alarm_map = {str(r[0]): int(r[1]) for r in alarm_rows if r[0]}

    gateways: list[dict[str, Any]] = []
    online_cnt = 0
    for edge_id, last_seen, max_seq, total_batches in batch_rows:
        eid = str(edge_id)
        seen_utc = _as_utc(last_seen) if last_seen is not None else None
        age_s = max(0, int((ref_now - seen_utc).total_seconds())) if seen_utc else None
        is_online = age_s is not None and age_s <= window
        if is_online:
            online_cnt += 1
        gateways.append(
            {
                "edge_id": eid,
                "online": is_online,
                "last_seen_at": seen_utc.isoformat() if seen_utc else None,
                "age_s": age_s,
                "last_seq": int(max_seq or 0),
                "total_batches": int(total_batches or 0),
                "pending_commands": cmd_map.get(eid, 0),
                "open_alarms": alarm_map.get(eid, 0),
            }
        )

    return {
        "tenant_id": tenant_id,
        "online_window_s": window,
        "gateways_total": len(gateways),
        "gateways_online": online_cnt,
        "open_alarms_total": open_alarms_total,
        "gateways": gateways,
    }


async def tenant_edge_alarm_counts(
    session: AsyncSession,
    tenant_ids: list[str],
    *,
    online_window_s: int = 300,
) -> list[dict[str, Any]]:
    """批量汇总各租户网关数、在线数与未闭环窑炉告警数，供 Prometheus 导出。"""
    now = utcnow()
    out: list[dict[str, Any]] = []
    for tid in sorted(tenant_ids):
        snap = await edge_fleet_summary(session, tid, online_window_s=online_window_s, now=now)
        out.append(
            {
                "tenant_id": tid,
                "gateways_total": snap["gateways_total"],
                "gateways_online": snap["gateways_online"],
                "open_alarms_total": snap["open_alarms_total"],
            }
        )
    return out
