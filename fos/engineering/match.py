"""知识库匹配：衬里模板 + 硬约束过滤 + 枚举厚度，给出 2–3 个备选方案（方案 4.2 第 5 步）。

硬约束全部由确定性代码判断：工作层分类温度 ≥ 最高温度 + 余量、后续各层分类温度 ≥ 热面温度 + 余量、
外壁温度达标、总厚度要求、气氛相容。满足约束的组合里分别挑“成本最低 / 最薄 / 热损失最小”。
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from fos.engineering.config import EngineeringConfig, Template
from fos.engineering.design import BRICK, Conditions, thermal_checks
from fos.engineering.kb import Product

PER_LAYER = 5  # 每层最多考虑的产品数（成本最低 3 个 + 导热最低 2 个）


@dataclass
class Candidate:
    template: Template
    layers: list[tuple[str, Product, float]]
    cost: float
    total: float
    q: float
    shell: float
    checks: list[dict[str, Any]] = field(default_factory=list)

    def layer_dicts(self) -> list[dict[str, Any]]:
        return [{"role": r, "product_id": p.id, "thickness_mm": t} for r, p, t in self.layers]

    def key(self) -> tuple:
        return tuple((p.id, t) for _, p, t in self.layers)


def price_per_m3(p: Product) -> float | None:
    if not p.price or not p.density:
        return None
    u = p.price.unit
    if u in ("t", "吨"):
        return p.price.price * p.density
    if u in ("kg", "千克"):
        return p.price.price * p.density * 1000
    if u in ("m³", "m3", "立方米", "方"):
        return p.price.price
    if p.piece_volume:
        return p.price.price / p.piece_volume
    return None


def _hit(values: list[str], wanted: list[str]) -> bool:
    return not values or any(w in v or v in w for v in values for w in wanted if w)


def applicable_templates(cond: Conditions, cfg: EngineeringConfig) -> list[Template]:
    out = []
    for t in cfg.templates:
        lo, hi = t.temp_range
        if not (lo <= cond.max_temp <= hi):
            continue
        if cond.furnace_type and t.furnace_types and not _hit(t.furnace_types, [cond.furnace_type]):
            continue
        if cond.positions and t.positions and not _hit(t.positions, cond.positions):
            continue
        out.append(t)
    return out


def _layer_pool(
    cond: Conditions,
    cfg: EngineeringConfig,
    kb: dict[str, Product],
    cats: list[str],
    first: bool,
    why: Counter,
) -> list[Product]:
    pool = []
    for p in kb.values():
        if p.category not in cats:
            continue
        if not p.thermal_ready:
            why[f"{p.code} 缺热工参数"] += 1
            continue
        if price_per_m3(p) is None:
            why[f"{p.code} 没有可用价格"] += 1
            continue
        if any(a == u or u in a for a in cond.atmosphere for u in p.unsuitable):
            why[f"{p.code} 不适用于{'、'.join(cond.atmosphere)}气氛"] += 1
            continue
        if first and (p.classification_temp or 0) < cond.max_temp + cfg.safety_margin:
            why[
                f"{p.code} 分类温度 {p.classification_temp:g}℃ < "
                f"最高温度 {cond.max_temp:g}℃ + 余量 {cfg.safety_margin:g}℃"
            ] += 1
            continue
        pool.append(p)
    cheap = sorted(pool, key=lambda p: price_per_m3(p) or 0)[:3]
    insul = sorted(pool, key=lambda p: (p.tc_a or 0) + p.tc_b * 500)[:2]
    seen: dict[str, Product] = {p.id: p for p in cheap + insul}
    return list(seen.values())[:PER_LAYER]


def search(cond: Conditions, cfg: EngineeringConfig, kb: dict[str, Product]) -> dict[str, Any]:
    templates = applicable_templates(cond, cfg)
    why: Counter = Counter()
    feasible: list[Candidate] = []
    tried = 0
    if not templates:
        where = (
            f"{cond.furnace_type or '该炉型'} / {'、'.join(cond.positions) or '该部位'} / {cond.max_temp:g}℃"
        )
        why[f"没有适用于 {where} 的衬里模板"] += 1
    for tpl in templates:
        options: list[list[tuple[str, Product | None, float]]] = []
        for i, ls in enumerate(tpl.layers):
            pool = _layer_pool(cond, cfg, kb, ls.categories, i == 0, why)
            opts: list[tuple[str, Product | None, float]] = []
            for t in ls.thickness:
                if t == 0:
                    opts.append((ls.role, None, 0))
                else:
                    opts.extend((ls.role, p, float(t)) for p in pool)
            options.append(opts)
        if any(not o for o in options):
            continue
        for combo in itertools.product(*options):
            layers = [(r, p, t) for r, p, t in combo if p is not None]
            if len({p.id for _, p, _ in layers}) < len(layers):
                continue  # 同一产品不重复出现在两层
            tried += 1
            checks, normal, _peak = thermal_checks(cond, cfg, layers)
            bad = [c for c in checks if not c["ok"] and c["severity"] == "high"]
            if bad:
                why[bad[0]["key"]] += 1
                continue
            cost = sum(
                cond.area * t / 1000 * (1 + p.loss_rate / 100) * (price_per_m3(p) or 0) for _, p, t in layers
            )
            assert normal is not None
            feasible.append(
                Candidate(
                    tpl,
                    layers,
                    cost,
                    sum(t for *_, t in layers),
                    normal["heat_flux"],
                    normal["shell_temp"],
                    checks,
                )
            )

    picks: list[tuple[str, str, Candidate]] = []
    if feasible:
        by_cost = min(feasible, key=lambda c: (c.cost, c.total))
        by_thin = min(feasible, key=lambda c: (c.total, c.cost))
        by_loss = min(feasible, key=lambda c: (c.q, c.cost))
        for label, title, c in (
            ("A", "成本最低", by_cost),
            ("B", "衬里最薄", by_thin),
            ("C", "热损失最小", by_loss),
        ):
            if all(c.key() != x.key() for *_, x in picks):
                picks.append((label, title, c))
    # 编号连续
    picks = [(chr(ord("A") + i), title, c) for i, (_, title, c) in enumerate(picks)]
    return {
        "templates": [t.id for t in templates],
        "tried": tried,
        "feasible": len(feasible),
        "alternatives": [
            {
                "label": f"方案 {label}",
                "title": title,
                "template_id": c.template.id,
                "template_name": c.template.name,
                "notes": c.template.notes,
                "layers": c.layer_dicts(),
                "reasons": _reasons(cond, cfg, c, title, picks),
            }
            for label, title, c in picks
        ],
        "rejected": [{"reason": _REASON.get(k, k), "count": n} for k, n in why.most_common(8)],
    }


_REASON = {
    "hot_face_margin": "工作层分类温度余量不足",
    "shell_temp": "外壁温度超过要求",
    "thickness": "总厚度不满足文件要求",
    "thermal_data": "缺热工参数",
    "heat": "传热计算失败",
}


def _reasons(
    cond: Conditions,
    cfg: EngineeringConfig,
    c: Candidate,
    title: str,
    picks: list[tuple[str, str, Candidate]],
) -> list[str]:
    hot = c.layers[0][1]
    out = [
        f"{title}：材料成本约 {c.cost:,.0f} 元，总厚 {c.total:g} mm，"
        f"外壁 {c.shell:.1f}℃（要求 ≤ {cond.shell_temp:g}℃），"
        f"热流 {c.q:.0f} W/m²。",
        f"工作层 {hot.code} 分类温度 {hot.classification_temp:g}℃，相对最高温度 {cond.max_temp:g}℃ 余量 "
        f"{(hot.classification_temp or 0) - cond.max_temp:g}℃（要求 ≥ {cfg.safety_margin:g}℃）。",
    ]
    if hot.category in BRICK:
        out.append("工作层用砖，抗冲刷和结构稳定性好；隔热层用纤维 / 板材降低导热。")
    base = picks[0][2] if picks else None
    if base is not None and base is not c:
        out.append(
            f"与方案 A 相比：成本 {c.cost - base.cost:+,.0f} 元，厚度 {c.total - base.total:+g} mm，"
            f"热流 {c.q - base.q:+.0f} W/m²。"
        )
    return out


_MM = re.compile(r"(\d+(?:\.\d+)?)\s*mm", re.I)


def customer_spec(lining_spec: str | None, kb: dict[str, Product]) -> list[dict[str, Any]] | None:
    """按客户文件里写明的衬里（例如“工作层：JM-26 莫来石轻质砖 230 mm”）组一个方案，用于对照校核。"""
    if not lining_spec:
        return None
    layers = []
    for seg in re.split(r"[；;\n]", lining_spec):
        seg = seg.strip()
        if not seg:
            continue
        role, _, rest = seg.partition("：") if "：" in seg else seg.partition(":")
        text = rest or role
        m = _MM.search(text)
        if not m:
            continue
        norm = text.upper().replace(" ", "")
        hit = None
        for p in kb.values():
            names = [p.code, *p.aliases]
            if any(n and n.upper().replace(" ", "") in norm for n in names):
                hit = p
                break
        if hit is None:
            return None  # 有一层对不上知识库就不组方案，交给工程师
        layers.append(
            {
                "role": role.strip() if rest else f"第{len(layers) + 1}层",
                "product_id": hit.id,
                "thickness_mm": float(m.group(1)),
            }
        )
    return layers or None
