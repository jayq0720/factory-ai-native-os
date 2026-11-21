"""EHS 确定性合规判定与 KPI 计算（规则 3：绝不让模型算达标与合规率）。"""

from __future__ import annotations

import math
from typing import Any

from fos.ehs.config import EhsConfig


def evaluate_env_reading(cfg: EhsConfig, kind: str, value: float) -> dict[str, Any]:
    """按配置限值确定性判定单次环保监测是否达标。"""
    if kind not in cfg.env_limits:
        raise ValueError(f"未配置监测指标 {kind} 的排放限值")
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("实测值必须为有限数字")
    if value < 0:
        raise ValueError("实测值不能为负数")
    lim = cfg.env_limits[kind]
    passed = float(value) <= float(lim.max)
    ratio = round(float(value) / float(lim.max), 4)
    return {
        "kind": kind,
        "name": lim.name,
        "value": round(float(value), 4),
        "unit": lim.unit,
        "limit_value": float(lim.max),
        "passed": passed,
        "exceed_ratio": ratio,
    }


def compute_ehs_kpis(
    hazards: list[dict[str, Any]],
    permits: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    env_readings: list[dict[str, Any]],
) -> dict[str, Any]:
    """确定性汇总隐患闭环率、作业票统计、事故统计与环保达标率。"""
    h_total = len(hazards)
    h_verified = sum(1 for h in hazards if h.get("status") == "verified")
    h_open = h_total - h_verified
    h_overdue = sum(1 for h in hazards if h.get("overdue"))
    h_major_open = sum(1 for h in hazards if h.get("level") == "major" and h.get("status") != "verified")
    h_rate = round(h_verified / h_total * 100.0, 2) if h_total > 0 else 100.0

    p_total = len(permits)
    p_active = sum(1 for p in permits if p.get("status") == "active")
    p_closed = sum(1 for p in permits if p.get("status") in ("closed", "revoked"))
    p_high = sum(1 for p in permits if p.get("kind") in ("hot_work", "confined_space"))

    i_total = len(incidents)
    i_closed = sum(1 for i in incidents if i.get("status") == "closed")
    i_open = i_total - i_closed
    injured = sum(int(i.get("injured_count") or 0) for i in incidents)

    e_total = len(env_readings)
    e_passed = sum(1 for e in env_readings if e.get("passed") is True)
    e_exceeded = e_total - e_passed
    e_rate = round(e_passed / e_total * 100.0, 2) if e_total > 0 else 100.0

    return {
        "hazards_total": h_total,
        "hazards_open": h_open,
        "hazards_verified": h_verified,
        "hazards_overdue": h_overdue,
        "hazards_major_open": h_major_open,
        "hazard_closure_rate_pct": h_rate,
        "permits_total": p_total,
        "permits_active": p_active,
        "permits_closed": p_closed,
        "permits_high_risk": p_high,
        "incidents_total": i_total,
        "incidents_open": i_open,
        "incidents_closed": i_closed,
        "total_injured": injured,
        "env_total": e_total,
        "env_passed": e_passed,
        "env_exceeded": e_exceeded,
        "env_compliance_rate_pct": e_rate,
    }
