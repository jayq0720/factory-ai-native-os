"""排窑求解（OR-Tools CP-SAT，Apache-2.0）。纯函数：输入窑炉和待烧需求，输出计划窑次，不碰数据库、不调用模型。

模型（时间单位：小时，从计划开始算；数量按 SCALE 放大成整数）：
- 每个窑一串有序的窑次槽位，前一个出窑后一个才能装（同一窑不重叠）；用了第 s 个才能用第 s+1 个（去对称）；
- 每个窑次选一个烧成组（同组产品可同窑装烧），时长 = 该组在这个窑上的烧成周期；
- 需求片段（订单的一部分）可以拆到多个窑次，一个窑次可以装多个同组订单，合计不超过每窑装量；
- 片段有最早装窑时间（还没开工的量要先配料、成型、干燥）；
- 目标：排不下的量 ≫ 逾期小时 > 开窑数 > 完成时间（权重在 planning.yaml）。

求解器只给建议；确认后的计划由人提交，再由账本守卫（guard.py）逐行校验装量、窑温和时间。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from fos.planning.config import SolverConfig

SCALE = 100  # 数量精度 0.01


def to_int(q: float) -> int:
    return round(q * SCALE)


def to_qty(v: int) -> float:
    return round(v / SCALE, 2)


@dataclass(frozen=True)
class KilnIn:
    id: str
    capacity: int  # 已放大
    cycle_hours: int
    available_from: int = 0  # 正在烧的窑：出窑后才能排
    max_temp: float | None = None


@dataclass(frozen=True)
class PieceIn:
    key: str
    order: str
    group: str
    qty: int  # 已放大
    kilns: tuple[str, ...]  # 允许的窑（单位、窑温已由调用方筛过）
    release: int = 0  # 最早装窑时间
    due: int | None = None  # 最晚可发货时间（小时）；None = 没有交期
    firing_hours: int | None = None  # 产品自己的烧成周期；None = 按窑炉周期
    weight: int = 1


@dataclass
class Problem:
    kilns: list[KilnIn]
    pieces: list[PieceIn]
    horizon: int  # 小时
    post_hours: int  # 出窑到可发货
    max_runs_per_kiln: int
    solver: SolverConfig


@dataclass
class RunOut:
    kiln: str
    slot: int
    start: int
    end: int
    group: str
    loads: dict[str, int] = field(default_factory=dict)  # 片段 key → 数量（放大）


@dataclass
class Solution:
    status: str  # optimal / feasible / unknown / empty
    runs: list[RunOut]
    short: dict[str, int]  # 片段 key → 排不下的量
    ready: dict[str, int]  # 订单 → 可发货时间（小时）；只含排上了的订单
    objective: float = 0.0
    seconds: float = 0.0


def duration(k: KilnIn, pieces: list[PieceIn], group: str) -> int:
    return max((p.firing_hours or k.cycle_hours) for p in pieces if p.group == group and k.id in p.kilns)


def solve(pb: Problem) -> Solution:
    pieces = [p for p in pb.pieces if p.qty > 0]
    if not pieces or not pb.kilns:
        return Solution("empty", [], {p.key: p.qty for p in pieces}, {})
    w = pb.solver.weights
    m = cp_model.CpModel()
    H = pb.horizon
    orders = sorted({p.order for p in pieces})
    fin = {o: m.new_int_var(0, H, f"fin_{o}") for o in orders}
    loads_of: dict[str, list] = {p.key: [] for p in pieces}
    slots: list[tuple[KilnIn, int, cp_model.IntVar, cp_model.IntVar, dict, dict]] = []
    used_vars = []

    for k in pb.kilns:
        mine = [p for p in pieces if k.id in p.kilns]
        if not mine or k.capacity <= 0:
            continue
        groups = sorted({p.group for p in mine})
        durs = {g: duration(k, mine, g) for g in groups}
        span = H - k.available_from
        need = sum(math.ceil(p.qty / k.capacity) for p in mine)
        n = min(pb.max_runs_per_kiln, span // min(durs.values()) if span > 0 else 0, need)
        prev_end = prev_used = None
        for s in range(n):
            used = m.new_bool_var(f"u_{k.id}_{s}")
            x = {g: m.new_bool_var(f"x_{k.id}_{s}_{g}") for g in groups}
            m.add(sum(x.values()) == used)
            start = m.new_int_var(k.available_from, H, f"s_{k.id}_{s}")
            dur = sum(durs[g] * x[g] for g in groups)
            end = m.new_int_var(k.available_from, H, f"e_{k.id}_{s}")
            m.add(end == start + dur)
            if prev_end is not None:
                m.add(start >= prev_end)
                m.add_implication(used, prev_used)
            prev_end, prev_used = end, used
            used_vars.append(used)
            ld: dict[str, cp_model.IntVar] = {}
            for p in mine:
                ub = min(k.capacity, p.qty)
                q = m.new_int_var(0, ub, f"l_{k.id}_{s}_{p.key}")
                y = m.new_bool_var(f"y_{k.id}_{s}_{p.key}")
                m.add(q <= ub * y)
                m.add_implication(y, x[p.group])
                if p.release > k.available_from:
                    m.add(start >= p.release).only_enforce_if(y)
                m.add(fin[p.order] >= end).only_enforce_if(y)
                ld[p.key] = q
                loads_of[p.key].append(q)
            m.add(sum(ld.values()) <= k.capacity)
            slots.append((k, s, start, end, x, ld))

    short = {}
    for p in pieces:
        short[p.key] = m.new_int_var(0, p.qty, f"short_{p.key}")
        m.add(sum(loads_of[p.key]) + short[p.key] == p.qty)

    late_terms = []
    for o in orders:
        dues = [p.due for p in pieces if p.order == o and p.due is not None]
        if not dues:
            continue
        due = min(dues)
        late = m.new_int_var(0, H + pb.post_hours + max(0, -due), f"late_{o}")
        m.add(late >= fin[o] + pb.post_hours - due)
        weight = max(p.weight for p in pieces if p.order == o)
        late_terms.append(w.late_hour * weight * late)

    m.minimize(
        w.shortfall * sum(short.values())
        + sum(late_terms)
        + w.run * sum(used_vars)
        + w.finish_hour * sum(fin.values())
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = pb.solver.time_limit_s
    solver.parameters.num_workers = pb.solver.workers
    solver.parameters.random_seed = 0
    st = solver.solve(m)
    status = {cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible"}.get(st, "unknown")
    if status == "unknown":
        return Solution(status, [], {p.key: p.qty for p in pieces}, {}, seconds=solver.wall_time)

    runs: list[RunOut] = []
    for k, s, start, end, x, ld in slots:
        loads = {key: v for key, q in ld.items() if (v := solver.value(q)) > 0}
        if not loads:
            continue
        group = next(g for g, b in x.items() if solver.value(b))
        runs.append(RunOut(k.id, s, solver.value(start), solver.value(end), group, loads))
    by_key = {p.key: p for p in pieces}
    ready: dict[str, int] = {}
    for r in runs:
        for key in r.loads:
            o = by_key[key].order
            ready[o] = max(ready.get(o, 0), r.end + pb.post_hours)
    return Solution(
        status,
        sorted(runs, key=lambda r: (r.start, r.kiln)),
        {key: v for key, var in short.items() if (v := solver.value(var)) > 0},
        ready,
        objective=solver.objective_value,
        seconds=round(solver.wall_time, 2),
    )
