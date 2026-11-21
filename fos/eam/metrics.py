"""设备可靠性与停机指标确定性计算（可用率、MTBF、MTTR，纯函数，不调用模型）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def _finite(v: Any, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 不是有效数字") from e
    if not math.isfinite(f):
        raise ValueError(f"{name} 必须是有限数字")
    return f


@dataclass(frozen=True)
class ReliabilityMetrics:
    window_days: int
    planned_minutes: float
    breakdown_count: int
    total_downtime_minutes: float
    availability_pct: float
    mttr_minutes: float | None
    mtbf_hours: float | None

    def out(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "planned_minutes": round(self.planned_minutes, 1),
            "breakdown_count": self.breakdown_count,
            "total_downtime_minutes": round(self.total_downtime_minutes, 1),
            "availability_pct": self.availability_pct,
            "mttr_minutes": self.mttr_minutes,
            "mtbf_hours": self.mtbf_hours,
        }


def compute_reliability(
    *,
    window_days: int,
    planned_hours_per_day: float,
    work_orders: list[dict[str, Any]],
) -> ReliabilityMetrics:
    """按统计窗口内的故障工单确定性计算设备可用率、MTTR（平均修复时间）与 MTBF（平均故障间隔）。"""
    if window_days <= 0:
        raise ValueError("统计天数必须大于 0")
    hours = _finite(planned_hours_per_day, "每日计划运行时长")
    if hours <= 0 or hours > 24.0:
        raise ValueError("每日计划运行时长必须在 (0, 24] 内")
    planned_min = float(window_days) * hours * 60.0

    breakdowns = [w for w in work_orders if w.get("kind") == "breakdown" and w.get("status") != "cancelled"]
    downtime = 0.0
    for w in breakdowns:
        dm = _finite(w.get("downtime_minutes") or 0.0, "停机时长")
        if dm < 0:
            raise ValueError("停机时长不能为负数")
        downtime += dm

    uptime_min = max(planned_min - downtime, 0.0)
    avail = round(100.0 * uptime_min / planned_min, 2) if planned_min > 0 else 100.0
    cnt = len(breakdowns)
    mttr = round(downtime / cnt, 1) if cnt > 0 else None
    mtbf = round((uptime_min / 60.0) / cnt, 1) if cnt > 0 else None
    return ReliabilityMetrics(
        window_days=window_days,
        planned_minutes=planned_min,
        breakdown_count=cnt,
        total_downtime_minutes=downtime,
        availability_pct=avail,
        mttr_minutes=mttr,
        mtbf_hours=mtbf,
    )
