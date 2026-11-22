"""筑炉施工进度与烘炉曲线确定性计算（规则 3：绝不让模型算施工进度与升温速率）。"""

from __future__ import annotations

import math
from typing import Any

from fos.construction.config import ConstructionConfig


def _finite(val: Any, label: str, *, allow_zero: bool = True) -> float:
    if isinstance(val, bool) or not isinstance(val, int | float) or not math.isfinite(val):
        raise ValueError(f"{label}必须为有限数字")
    f = float(val)
    if (f < 0) if allow_zero else (f <= 0):
        raise ValueError(f"{label}数值范围非法")
    return f


def compute_site_progress(planned_tons: float, logs: list[dict[str, Any]]) -> dict[str, float]:
    """根据计划砌筑量与施工日报确定性汇总累计完成量与进度百分比。"""
    planned = _finite(planned_tons, "计划砌筑量", allow_zero=False)
    completed = sum(_finite(r.get("tons_installed", 0.0), "当日完成量") for r in logs)
    pct = round(min(completed / planned * 100.0, 100.0), 2)
    return {"completed_tons": round(completed, 3), "progress_pct": pct}


def evaluate_dryout(
    cfg: ConstructionConfig,
    *,
    target_temp_c: float,
    actual_temp_c: float,
    ramp_rate_c_per_h: float,
) -> dict[str, Any]:
    """确定性判定烘炉实测温度偏差与升温速率是否满足防爆裂曲线要求。"""
    t_target = _finite(target_temp_c, "目标温度")
    t_actual = _finite(actual_temp_c, "实测温度")
    rate = _finite(ramp_rate_c_per_h, "升温速率")
    dev = round(t_actual - t_target, 2)
    reasons: list[str] = []
    if abs(dev) > cfg.max_temp_deviation_c:
        reasons.append(f"温差 {dev:+.1f}℃ 超出允许偏差 ±{cfg.max_temp_deviation_c:g}℃")
    if rate > cfg.max_ramp_rate_c_per_h:
        reasons.append(f"升温速率 {rate:g}℃/h 超出上限 {cfg.max_ramp_rate_c_per_h:g}℃/h（防爆裂红线）")
    return {
        "within_spec": len(reasons) == 0,
        "deviation_c": dev,
        "reasons": reasons,
    }
