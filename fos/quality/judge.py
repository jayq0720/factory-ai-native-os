"""理化检验确定性判定（纯函数，不调用模型，符合 CLAUDE.md 规则 3）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from fos.quality.config import QualityConfig, SpecLimit

EPS = 1e-9
METRIC_KEYS = (
    "bulk_density",
    "cold_crushing_strength",
    "thermal_conductivity",
    "permanent_linear_change",
    "refractoriness",
    "al2o3",
    "fe2o3",
)


@dataclass(frozen=True)
class MetricCheck:
    key: str
    name: str
    value: float
    unit: str
    min: float | None
    max: float | None
    ok: bool
    note: str

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "min": self.min,
            "max": self.max,
            "ok": self.ok,
            "note": self.note,
        }


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    result: str  # pass / fail
    checks: list[MetricCheck]
    failed_items: list[str]
    summary: str

    def out(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "result": self.result,
            "checks": [c.out() for c in self.checks],
            "failed_items": self.failed_items,
            "summary": self.summary,
        }


def _fmt_range(lo: float | None, hi: float | None, unit: str) -> str:
    u = f" {unit}" if unit else ""
    if lo is not None and hi is not None:
        return f"{lo:g}～{hi:g}{u}"
    if lo is not None:
        return f"≥ {lo:g}{u}"
    if hi is not None:
        return f"≤ {hi:g}{u}"
    return "不限"


def evaluate_measurements(
    cfg: QualityConfig,
    measurements: dict[str, Any],
    *,
    category: str | None = None,
    product_state: dict[str, Any] | None = None,
) -> JudgeResult:
    """按行业包与产品标称参数逐项判定检验指标。"""
    specs = cfg.spec_for(category)
    # 若产品台账定义了标称体积密度，按容差覆盖 bulk_density 限值
    if product_state:
        nom = product_state.get("bulk_density")
        if isinstance(nom, int | float) and not isinstance(nom, bool) and math.isfinite(nom) and nom > 0:
            tol = cfg.density_tolerance_pct / 100.0
            lo = round(float(nom) * (1.0 - tol), 4)
            hi = round(float(nom) * (1.0 + tol), 4)
            specs["bulk_density"] = SpecLimit(name="体积密度", unit="g/cm³", min=lo, max=hi)

    checks: list[MetricCheck] = []
    failed: list[str] = []
    for k in METRIC_KEYS:
        raw = measurements.get(k)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
            raise ValueError(f"检验指标 {k} 必须是有限数字")
        val = float(raw)
        lim = specs.get(k)
        if lim is None:
            checks.append(MetricCheck(k, k, val, "", None, None, True, "无标准限值，仅记录"))
            continue
        lo_ok = lim.min is None or val >= lim.min - EPS
        hi_ok = lim.max is None or val <= lim.max + EPS
        ok = lo_ok and hi_ok
        rng = _fmt_range(lim.min, lim.max, lim.unit)
        note = f"合格（标准 {rng}）" if ok else f"超标（实测 {val:g}，标准 {rng}）"
        checks.append(MetricCheck(k, lim.name, val, lim.unit, lim.min, lim.max, ok, note))
        if not ok:
            failed.append(f"{lim.name} {note}")

    passed = not failed
    if not checks:
        summary = "未提供数值指标，按人工结论记录"
    elif passed:
        summary = f"全部 {len(checks)} 项指标合格"
    else:
        summary = f"{len(failed)}/{len(checks)} 项指标不合格：" + "；".join(failed)
    return JudgeResult(
        passed=passed,
        result="pass" if passed else "fail",
        checks=checks,
        failed_items=failed,
        summary=summary,
    )
