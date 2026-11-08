"""烧成曲线比对、窑次汇总、能耗（纯函数，确定性代码；规则 3：数字不让模型算）。"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta

from fos.monitoring.config import Curve


@dataclass(frozen=True)
class Sample:
    ts: datetime
    value: float
    quality: str = "good"


def expected(curve: Curve, hours: float) -> float | None:
    """装窑后第 hours 小时的目标温度（线性插值）；超出制度时长返回 None（不比对）。"""
    if hours < 0 or hours > curve.hours:
        return None
    pts = curve.points
    for (h0, t0), (h1, t1) in itertools.pairwise(pts):
        if h0 <= hours <= h1:
            return t0 + (t1 - t0) * (hours - h0) / (h1 - h0)
    return pts[-1][1]


def deviation_of(curve: Curve, start: datetime, s: Sample) -> float | None:
    """实测 − 目标；不在制度时长内或读数质量差时返回 None。"""
    if s.quality == "bad":
        return None
    exp = expected(curve, (s.ts - start).total_seconds() / 3600)
    return None if exp is None else s.value - exp


@dataclass
class Span:
    """一段连续超差（或超限）。"""

    start: datetime
    end: datetime
    worst: float  # 偏差绝对值最大的那个读数的偏差（带符号）

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def spans(samples: list[Sample], out_of: list[float | None], max_gap: timedelta) -> list[Span]:
    """把“超出”的连续读数并成区间；相邻两个读数间隔超过 max_gap 视为断开。

    out_of[i] 是第 i 个读数超出的量（带符号），None 或 0 表示正常。
    """
    res: list[Span] = []
    cur: Span | None = None
    prev: datetime | None = None
    for s, d in zip(samples, out_of, strict=True):
        if not d:
            cur = None
            prev = s.ts
            continue
        if cur is not None and prev is not None and s.ts - prev <= max_gap:
            cur.end = s.ts
            if abs(d) > abs(cur.worst):
                cur.worst = d
        else:
            cur = Span(s.ts, s.ts, d)
            res.append(cur)
        prev = s.ts
    return res


def curve_excess(curve: Curve, start: datetime, samples: list[Sample]) -> list[float | None]:
    """每个读数超出允许偏差的部分（带符号，相对目标曲线）；在偏差内为 None。"""
    out: list[float | None] = []
    for s in samples:
        d = deviation_of(curve, start, s)
        out.append(d if d is not None and abs(d) > curve.tolerance else None)
    return out


def limit_excess(low: float | None, high: float | None, samples: list[Sample]) -> list[float | None]:
    """超上限为正（超出量），低于下限为负；正常为 None。"""
    out: list[float | None] = []
    for s in samples:
        if s.quality == "bad":
            out.append(None)
        elif high is not None and s.value > high:
            out.append(s.value - high)
        elif low is not None and s.value < low:
            out.append(s.value - low)
        else:
            out.append(None)
    return out


def meter_usage(samples: list[Sample]) -> float:
    """累计表（电表 kWh、燃气表 m³）在区间内的用量：相邻读数差的正数部分之和（换表 / 回零不算负数）。"""
    good = [s for s in samples if s.quality != "bad"]
    return sum(max(0.0, b.value - a.value) for a, b in itertools.pairwise(good))


@dataclass
class RunSummary:
    max_temp: float | None
    max_at: datetime | None
    deviation: bool
    deviation_minutes: float
    worst_deviation: float | None
    samples: int
    spans: list[Span]


def summarize(
    curve: Curve | None,
    start: datetime,
    samples: list[Sample],
    *,
    min_minutes: float,
    max_gap: timedelta,
) -> RunSummary:
    """一个窑次（一个或多个参与比对的温度点合并后）的最高温度与超差情况。"""
    good = [s for s in samples if s.quality != "bad"]
    top = max(good, key=lambda s: s.value, default=None)
    found: list[Span] = []
    if curve is not None and good:
        found = [
            x for x in spans(good, curve_excess(curve, start, good), max_gap) if x.minutes >= min_minutes
        ]
    return RunSummary(
        max_temp=top.value if top else None,
        max_at=top.ts if top else None,
        deviation=bool(found),
        deviation_minutes=round(sum(x.minutes for x in found), 1),
        worst_deviation=max((x.worst for x in found), key=abs, default=None),
        samples=len(samples),
        spans=found,
    )


def series(curve: Curve, start: datetime, step_minutes: float = 30) -> list[tuple[datetime, float]]:
    """目标曲线（画图用）。"""
    out = []
    n = int(curve.hours * 60 / step_minutes)
    for i in range(n + 1):
        h = i * step_minutes / 60
        v = expected(curve, h)
        if v is not None:
            out.append((start + timedelta(hours=h), round(v, 1)))
    return out
