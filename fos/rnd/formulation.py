"""耐火材料配方确定性核算与试验达标判定（规则 3：绝不让模型算配比、理论成分与成本）。"""

from __future__ import annotations

import math
from typing import Any


def _finite(val: Any, label: str, *, allow_zero: bool = True, max_val: float | None = None) -> float:
    if isinstance(val, bool) or not isinstance(val, int | float) or not math.isfinite(val):
        raise ValueError(f"{label}必须为有限数字")
    f = float(val)
    if (f < 0) if allow_zero else (f <= 0):
        raise ValueError(f"{label}数值范围非法")
    if max_val is not None and f > max_val:
        raise ValueError(f"{label}不能超过 {max_val:g}")
    return f


def evaluate_formula(ingredients: list[dict[str, Any]], *, pct_tolerance: float = 0.5) -> dict[str, Any]:
    """确定性核算耐材配方配比总和、理论 Al2O3 含量与估算吨原料成本。"""
    if not ingredients:
        raise ValueError("配方原料明细不能为空")
    total_pct = 0.0
    al2o3_weighted = 0.0
    cost_per_ton = 0.0
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(ingredients, 1):
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"第 {idx} 项原料名称不能为空")
        pct = _finite(item.get("pct"), f"第 {idx} 项配比(%)", allow_zero=False, max_val=100.0)
        al2o3 = _finite(item.get("al2o3_pct", 0.0), f"第 {idx} 项 Al2O3(%)", allow_zero=True, max_val=100.0)
        unit_cost = _finite(item.get("unit_cost", 0.0), f"第 {idx} 项原料单价(元/吨)", allow_zero=True)
        total_pct += pct
        al2o3_weighted += (pct / 100.0) * al2o3
        cost_per_ton += (pct / 100.0) * unit_cost
        normalized.append(
            {
                "name": name,
                "pct": round(pct, 4),
                "al2o3_pct": round(al2o3, 4),
                "unit_cost": round(unit_cost, 2),
            }
        )
    total_pct = round(total_pct, 4)
    if abs(total_pct - 100.0) > pct_tolerance:
        raise ValueError(f"配方组分总和为 {total_pct:g}%，超出允许范围 100% ± {pct_tolerance:g}%")
    return {
        "ingredients": normalized,
        "total_pct": total_pct,
        "calc_al2o3_pct": round(al2o3_weighted, 2),
        "est_cost_per_ton": round(cost_per_ton, 2),
    }


def evaluate_trial(
    project_state: dict[str, Any],
    *,
    al2o3_pct: float | None,
    bulk_density: float | None,
    ccs_mpa: float | None,
) -> tuple[bool, list[str]]:
    """对照研发项目理化指标下限，确定性判定单次试验是否达标。"""
    reasons: list[str] = []
    checks = (
        ("target_al2o3_min", al2o3_pct, "Al2O3(%)"),
        ("target_bd_min", bulk_density, "体积密度(g/cm³)"),
        ("target_ccs_min", ccs_mpa, "常温耐压(MPa)"),
    )
    for key, actual, label in checks:
        target = project_state.get(key)
        if target is None:
            continue
        t_val = _finite(target, f"目标{label}", allow_zero=True)
        if actual is None:
            reasons.append(f"缺少{label}实测值（要求 ≥ {t_val:g}）")
            continue
        a_val = _finite(actual, f"实测{label}", allow_zero=True)
        if a_val < t_val:
            reasons.append(f"{label} 实测 {a_val:g} 低于目标下限 {t_val:g}")
    return len(reasons) == 0, reasons
