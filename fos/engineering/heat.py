"""多层平壁 / 圆筒一维稳态传热（方案 4.2 第 6 步）。

导热系数随温度线性变化 k(T) = a + b·T（W/(m·K)，T 为 ℃），对单层积分得到精确解：
    ∫k dT = a(T₁−T₂) + b/2·(T₁²−T₂²) = q·δ            （平壁，δ 为厚度 m）
    ∫k dT = q'/(2π)·ln(r₂/r₁)                          （圆筒，q' 为每米长热流 W/m）
外表面：q = h·(Ts − Ta)，h = 自然对流 C·ΔT^(1/3) + 辐射 εσ(Ts⁴−Ta⁴)/(Ts−Ta)。
对热流 q 二分，使从热面逐层推得的外壁温度与外表面散热平衡。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

SIGMA = 5.670374419e-8
FORMULAS = [
    "k(T) = a + b·T",
    "平壁：a(T₁−T₂) + b/2·(T₁²−T₂²) = q·δ",
    "圆筒：a(T₁−T₂) + b/2·(T₁²−T₂²) = q'/(2π)·ln(r₂/r₁)",
    "外表面：q = (C·ΔT^(1/3) + εσ(Ts⁴−Ta⁴)/ΔT)·ΔT",
]


class HeatError(ValueError):
    pass


@dataclass(frozen=True)
class Layer:
    name: str
    thickness_mm: float
    tc_a: float
    tc_b: float = 0.0


@dataclass
class HeatResult:
    hot_temp: float
    ambient: float
    q: float  # W/m²（圆筒为外表面单位面积）
    temps: list[float]  # 各界面温度，从热面到外壁，长度 = 层数 + 1
    layer_k: list[float] = field(default_factory=list)  # 各层平均导热系数
    geometry: str = "flat"

    @property
    def shell_temp(self) -> float:
        return self.temps[-1]

    def as_dict(self) -> dict:
        return {
            "geometry": self.geometry,
            "hot_temp": self.hot_temp,
            "ambient": self.ambient,
            "heat_flux": round(self.q, 1),
            "shell_temp": round(self.shell_temp, 1),
            "interface_temps": [round(t, 1) for t in self.temps],
            "layer_k": [round(k, 4) for k in self.layer_k],
            "formulas": FORMULAS,
        }


def surface_h(ts: float, ta: float, *, emissivity: float = 0.8, conv: float = 1.31) -> float:
    d = max(ts - ta, 0.01)
    hc = conv * d ** (1 / 3)
    hr = emissivity * SIGMA * ((ts + 273.15) ** 4 - (ta + 273.15) ** 4) / d
    return hc + hr


def _next_temp(t1: float, a: float, b: float, rhs: float) -> float | None:
    """解 a(T₁−T₂) + b/2(T₁²−T₂²) = rhs 求 T₂。"""
    if abs(b) < 1e-12:
        return t1 - rhs / a
    c = a * t1 + b / 2 * t1 * t1 - rhs
    disc = a * a + 2 * b * c
    if disc < 0:
        return None
    return (-a + math.sqrt(disc)) / b


def solve(
    layers: list[Layer],
    hot_temp: float,
    ambient: float = 25.0,
    *,
    emissivity: float = 0.8,
    conv: float = 1.31,
    geometry: Literal["flat", "cylinder"] = "flat",
    inner_radius_m: float | None = None,
) -> HeatResult:
    if not layers:
        raise HeatError("至少需要一层")
    if hot_temp <= ambient:
        raise HeatError("热面温度必须高于环境温度")
    for ly in layers:
        if ly.thickness_mm <= 0:
            raise HeatError(f"{ly.name} 厚度必须大于 0")
        if ly.tc_a <= 0 or ly.tc_a + ly.tc_b * hot_temp <= 0:
            raise HeatError(f"{ly.name} 导热系数不合法")
    if geometry == "cylinder" and not (inner_radius_m and inner_radius_m > 0):
        raise HeatError("圆筒需要内半径")

    radii = [inner_radius_m or 0.0]
    for ly in layers:
        radii.append(radii[-1] + ly.thickness_mm / 1000)

    def march(q: float) -> list[float] | None:
        # q 为外表面热流密度（W/m²）；圆筒换算成每米长热流
        temps = [hot_temp]
        t = hot_temp
        for i, ly in enumerate(layers):
            if geometry == "flat":
                rhs = q * ly.thickness_mm / 1000
            else:
                qline = q * 2 * math.pi * radii[-1]
                rhs = qline / (2 * math.pi) * math.log(radii[i + 1] / radii[i])
            t2 = _next_temp(t, ly.tc_a, ly.tc_b, rhs)
            if t2 is None or t2 < ambient:
                return None
            temps.append(t2)
            t = t2
        return temps

    lo, hi = 0.0, 200000.0
    best: list[float] | None = None
    best_q = 0.0
    for _ in range(100):
        q = (lo + hi) / 2
        temps = march(q)
        if temps is None:
            hi = q
            continue
        ts = temps[-1]
        q_out = surface_h(ts, ambient, emissivity=emissivity, conv=conv) * (ts - ambient)
        best, best_q = temps, q
        if q_out > q:
            lo = q
        else:
            hi = q
        if hi - lo < 1e-4:
            break
    if best is None:
        raise HeatError("传热计算不收敛")
    ks = [ly.tc_a + ly.tc_b * (best[i] + best[i + 1]) / 2 for i, ly in enumerate(layers)]
    return HeatResult(hot_temp, ambient, best_q, best, ks, geometry)
