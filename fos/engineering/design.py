"""一个方案的完整确定性计算：传热校核 → 用量（BOM）→ 报价 → 校核结论。

输入是工况（来自工程项目）、各层产品与厚度、报价选项；输出全部是可复算的数字和公式。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fos.engineering.config import EngineeringConfig
from fos.engineering.heat import HeatError, Layer, solve
from fos.engineering.kb import Product, split_list

BRICK = {"insulating_brick", "dense_brick"}


def money(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Conditions:
    """计算用的工况。缺的值用配置里的默认值，并在 assumptions 里写明（进入审核清单）。"""

    normal_temp: float
    max_temp: float
    shell_temp: float
    ambient: float
    area: float
    atmosphere: list[str] = field(default_factory=list)
    furnace_type: str | None = None
    positions: list[str] = field(default_factory=list)
    thickness_limit: float | None = None
    thickness_rule: str | None = None
    tax_rate: float | None = None
    assumptions: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_project(cls, state: dict[str, Any], cfg: EngineeringConfig) -> Conditions:
        assumptions: list[dict[str, str]] = []
        normal, mx = state.get("normal_temp"), state.get("max_temp")
        if normal is None and mx is None:
            raise ValueError("工程项目缺少工作温度（正常工作温度或最高温度），请先补充工况")
        if not state.get("area"):
            raise ValueError("工程项目缺少衬里面积，无法计算用量，请先补充工况")
        if mx is None:
            mx = normal
            assumptions.append(
                {"key": "max_temp", "text": f"文件未给最高温度，按正常工作温度 {normal}℃ 校核选材"}
            )
        if normal is None:
            normal = mx
            assumptions.append(
                {"key": "normal_temp", "text": f"文件未给正常工作温度，按最高温度 {mx}℃ 计算外壁温度"}
            )
        shell = state.get("shell_temp")
        if shell is None:
            shell = cfg.default_shell_temp
            assumptions.append({"key": "shell_temp", "text": f"文件未给外壁温度要求，按默认 {shell}℃ 设计"})
        ambient = state.get("ambient_temp")
        if ambient is None:
            ambient = cfg.default_ambient
            assumptions.append({"key": "ambient_temp", "text": f"环境温度按 {ambient}℃ 计算"})
        return cls(
            normal_temp=float(normal),
            max_temp=float(mx),
            shell_temp=float(shell),
            ambient=float(ambient),
            area=float(state["area"]),
            atmosphere=split_list(state.get("atmosphere")),
            furnace_type=state.get("furnace_type"),
            positions=split_list(state.get("positions")),
            thickness_limit=state.get("thickness_limit"),
            thickness_rule=state.get("thickness_rule"),
            tax_rate=state.get("tax_rate"),
            assumptions=assumptions,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "normal_temp": self.normal_temp,
            "max_temp": self.max_temp,
            "shell_temp": self.shell_temp,
            "ambient": self.ambient,
            "area": self.area,
            "atmosphere": self.atmosphere,
            "furnace_type": self.furnace_type,
            "positions": self.positions,
            "thickness_limit": self.thickness_limit,
            "thickness_rule": self.thickness_rule,
            "tax_rate": self.tax_rate,
            "assumptions": self.assumptions,
        }


@dataclass
class QuoteOptions:
    discount_pct: float = 0.0
    freight_per_t: float = 0.0
    install_per_m2: float = 0.0
    tax_rate: float = 13.0

    def as_dict(self) -> dict[str, float]:
        return {
            "discount_pct": self.discount_pct,
            "freight_per_t": self.freight_per_t,
            "install_per_m2": self.install_per_m2,
            "tax_rate": self.tax_rate,
        }


def default_options(cond: Conditions, cfg: EngineeringConfig) -> QuoteOptions:
    q = cfg.quote
    return QuoteOptions(
        freight_per_t=q.freight_per_t,
        install_per_m2=q.install_per_m2,
        tax_rate=cond.tax_rate if cond.tax_rate is not None else q.tax_rate,
    )


def _check(key: str, ok: bool, text: str, severity: str = "high") -> dict[str, Any]:
    return {"key": key, "ok": ok, "text": text, "severity": severity}


def thermal_checks(
    cond: Conditions, cfg: EngineeringConfig, chosen: list[tuple[str, Product, float]]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    """传热计算与硬约束校核。返回 (校核项, 正常工况结果, 最高温度工况结果)。"""
    checks: list[dict[str, Any]] = []
    missing = [p for _, p, _ in chosen if not p.thermal_ready]
    if missing:
        checks.append(
            _check(
                "thermal_data",
                False,
                "知识库缺少热工参数（分类温度 / 体积密度 / 导热系数）：" + "、".join(p.code for p in missing),
            )
        )
        return checks, None, None
    layers = [Layer(p.code, t, p.tc_a or 0.0, p.tc_b) for _, p, t in chosen]
    kw = {"emissivity": cfg.surface.emissivity, "conv": cfg.surface.convection.get("wall", 1.31)}
    try:
        normal = solve(layers, cond.normal_temp, cond.ambient, **kw)
        peak = solve(layers, cond.max_temp, cond.ambient, **kw)
    except HeatError as e:
        checks.append(_check("heat", False, f"传热计算失败：{e}"))
        return checks, None, None

    hot = chosen[0][1]
    need = cond.max_temp + cfg.safety_margin
    checks.append(
        _check(
            "hot_face_margin",
            (hot.classification_temp or 0) >= need,
            f"工作层 {hot.code} 分类温度 {hot.classification_temp:g}℃，最高温度 {cond.max_temp:g}℃ + 余量 "
            f"{cfg.safety_margin:g}℃ = {need:g}℃",
        )
    )
    for i, (role, p, _t) in enumerate(chosen[1:], start=1):
        ti = peak.temps[i]
        need_i = ti + cfg.backup_margin
        checks.append(
            _check(
                f"layer{i}_margin",
                (p.classification_temp or 0) >= need_i,
                f"{role} {p.code} 热面温度 {ti:.0f}℃（最高温度工况）+ 余量 {cfg.backup_margin:g}℃，"
                f"分类温度 {p.classification_temp:g}℃",
            )
        )
    checks.append(
        _check(
            "shell_temp",
            normal.shell_temp <= cond.shell_temp,
            f"外壁温度 {normal.shell_temp:.1f}℃（正常工况，环境 {cond.ambient:g}℃），"
            f"要求 ≤ {cond.shell_temp:g}℃",
        )
    )
    total = sum(t for _, _, t in chosen)
    if cond.thickness_limit and cond.thickness_rule in ("min", "max"):
        ok = total >= cond.thickness_limit if cond.thickness_rule == "min" else total <= cond.thickness_limit
        word = "不小于" if cond.thickness_rule == "min" else "不大于"
        checks.append(
            _check("thickness", ok, f"总厚度 {total:g} mm，文件要求{word} {cond.thickness_limit:g} mm")
        )
    for _role, p, _t in chosen:
        bad = [a for a in cond.atmosphere if any(a == u or (u in a) for u in p.unsuitable)]
        if bad:
            checks.append(_check(f"atmosphere_{p.id}", False, f"{p.code} 不适用于{'、'.join(bad)}气氛"))
    return checks, normal.as_dict(), peak.as_dict()


def _qty(p: Product, volume: float, unit: str) -> tuple[float | None, str]:
    """把带损耗的体积换算成计价单位的数量。"""
    weight = volume * (p.density or 0)
    if unit in ("t", "吨"):
        return round(weight, 3), unit
    if unit in ("kg", "千克"):
        return round(weight * 1000, 1), unit
    if unit in ("m³", "m3", "立方米", "方"):
        return round(volume, 3), unit
    if p.piece_volume:
        return float(math.ceil(volume / p.piece_volume)), unit
    return None, unit


def evaluate(
    cond: Conditions,
    cfg: EngineeringConfig,
    kb: dict[str, Product],
    layers: list[dict[str, Any]],
    options: QuoteOptions,
    *,
    project_mode: str = "real",
) -> dict[str, Any]:
    """layers: [{role, product_id, thickness_mm}]，厚度 0 的层忽略。"""
    chosen: list[tuple[str, Product, float]] = []
    problems: list[dict[str, Any]] = []
    for ly in layers:
        t = float(ly.get("thickness_mm") or 0)
        if t <= 0:
            continue
        p = kb.get(ly["product_id"])
        if p is None:
            problems.append(_check("unknown_product", False, f"知识库里没有产品 {ly['product_id']}"))
            continue
        chosen.append((ly.get("role") or f"第{len(chosen) + 1}层", p, t))
    if not chosen:
        raise ValueError("方案至少需要一层")

    checks, heat_normal, heat_peak = thermal_checks(cond, cfg, chosen)
    checks = problems + checks

    # ---------- 用量（BOM） ----------
    bom: list[dict[str, Any]] = []
    brick_volume = 0.0
    total_weight = 0.0
    for i, (role, p, t) in enumerate(chosen):
        net = cond.area * t / 1000
        vol = net * (1 + p.loss_rate / 100)
        if p.category in BRICK:
            brick_volume += vol
        unit = p.price.unit if p.price else p.unit
        qty, unit = _qty(p, vol, unit)
        weight = vol * (p.density or 0)
        total_weight += weight
        bom.append(
            {
                "line": len(bom) + 1,
                "layer": i,
                "role": role,
                "product_id": p.id,
                "code": p.code,
                "name": p.name,
                "spec": p.spec,
                "thickness_mm": t,
                "area_m2": round(cond.area, 3),
                "net_volume_m3": round(net, 3),
                "loss_rate": p.loss_rate,
                "volume_m3": round(vol, 3),
                "weight_t": round(weight, 3),
                "qty": qty,
                "unit": unit,
                "formula": f"{cond.area:g} m² × {t:g} mm × (1 + {p.loss_rate:g}%)"
                + (f" × {p.density:g} t/m³" if unit in ("t", "吨") else ""),
            }
        )
        if qty is None:
            checks.append(
                _check(f"unit_{p.id}", False, f"{p.code} 计价单位 {unit} 无法由体积换算（缺单件体积）")
            )
    for aux in cfg.auxiliaries:
        if aux.per != "brick_volume" or brick_volume <= 0:
            continue
        cands = [p for p in kb.values() if p.category == aux.category and p.price]
        amount_t = brick_volume * aux.rate
        if not cands:
            checks.append(
                _check(f"aux_{aux.category}", False, f"知识库没有带价格的{aux.name}，BOM 未计入", "medium")
            )
            continue
        p = min(cands, key=lambda x: x.price.price if x.price else 0)
        unit = p.price.unit if p.price else p.unit
        qty = round(amount_t, 3) if unit in ("t", "吨") else round(amount_t * 1000, 1)
        total_weight += amount_t
        bom.append(
            {
                "line": len(bom) + 1,
                "layer": None,
                "role": "辅料",
                "product_id": p.id,
                "code": p.code,
                "name": p.name,
                "spec": p.spec,
                "thickness_mm": None,
                "area_m2": None,
                "net_volume_m3": None,
                "loss_rate": 0,
                "volume_m3": None,
                "weight_t": round(amount_t, 3),
                "qty": qty,
                "unit": unit,
                "formula": f"砖砌体 {brick_volume:.3f} m³ × {aux.rate:g} t/m³",
            }
        )

    # ---------- 报价 ----------
    lines = []
    material_list = Decimal(0)
    material_cost: Decimal | None = Decimal(0)
    prices_used: list[str] = []
    for b in bom:
        p = kb[b["product_id"]]
        if p.price is None:
            checks.append(_check(f"price_{p.id}", False, f"{p.code} 在价格表里没有有效价格"))
            lines.append({**_line_base(b), "unit_price": None, "amount": None})
            material_cost = None
            continue
        prices_used.append(p.price.ref())
        amt = money(Decimal(str(b["qty"] or 0)) * Decimal(str(p.price.price)))
        material_list += amt
        if p.price.cost is None or material_cost is None:
            material_cost = None
        else:
            material_cost += money(Decimal(str(b["qty"] or 0)) * Decimal(str(p.price.cost)))
        lines.append(
            {
                **_line_base(b),
                "unit_price": p.price.price,
                "amount": float(amt),
                "price_ref": p.price.ref(),
                "price_effective_from": p.price.effective_from,
                "price_valid_until": p.price.valid_until,
            }
        )
    discount = money(material_list * Decimal(str(options.discount_pct)) / 100)
    material_net = material_list - discount
    freight = money(Decimal(str(round(total_weight, 3))) * Decimal(str(options.freight_per_t)))
    install = money(Decimal(str(cond.area)) * Decimal(str(options.install_per_m2)))
    subtotal = material_net + freight + install
    tax = money(subtotal * Decimal(str(options.tax_rate)) / 100)
    total = subtotal + tax
    margin = None
    if material_cost is not None and material_net > 0:
        margin = float(
            ((material_net - material_cost) / material_net * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    quote = {
        "lines": lines,
        "options": options.as_dict(),
        "material_list": float(material_list),
        "discount": float(discount),
        "material_net": float(material_net),
        "freight": float(freight),
        "install": float(install),
        "subtotal": float(subtotal),
        "tax": float(tax),
        "total": float(total),
        "material_cost": None if material_cost is None else float(material_cost),
        "gross_margin_pct": margin,
        "total_weight_t": round(total_weight, 3),
        "prices_used": prices_used,
        "formulas": [
            "材料金额 = Σ 数量 × 单价（价格表版本见每行）",
            "材料净价 = 材料金额 × (1 − 折扣%)",
            "运费 = 总重 × 运费单价；施工费 = 面积 × 施工单价",
            "含税合计 = (材料净价 + 运费 + 施工费) × (1 + 税率%)",
            "材料毛利率 = (材料净价 − Σ 数量 × 成本价) / 材料净价",
        ],
    }

    modes = {project_mode} | {kb[b["product_id"]].data_mode for b in bom}
    modes |= {kb[b["product_id"]].price.data_mode for b in bom if kb[b["product_id"]].price}
    feasible = all(c["ok"] for c in checks if c["severity"] == "high")
    return {
        "layers": [
            {"role": r, "product_id": p.id, "code": p.code, "name": p.name, "thickness_mm": t}
            for r, p, t in chosen
        ],
        "total_thickness_mm": sum(t for _, _, t in chosen),
        "conditions": cond.as_dict(),
        "heat_normal": heat_normal,
        "heat_max": heat_peak,
        "checks": checks,
        "feasible": feasible,
        "bom": bom,
        "quote": quote,
        "data_mode": "mock" if "mock" in modes else "real",
    }


def _line_base(b: dict[str, Any]) -> dict[str, Any]:
    return {k: b[k] for k in ("line", "role", "product_id", "code", "name", "spec", "qty", "unit")}
