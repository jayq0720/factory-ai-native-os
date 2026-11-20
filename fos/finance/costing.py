"""确定性成本核算与价税分离计算（纯函数，不调用模型，结果可复算）。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from fos.finance.config import FinanceConfig


def _finite(v: Any, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 不是有效数字") from e
    if not math.isfinite(f):
        raise ValueError(f"{name} 必须是有限数字")
    return f


def split_vat(gross_amount: float, vat_rate: float) -> tuple[float, float]:
    """含税金额拆分为（不含税金额，税额），确保两者之和精确等于含税金额。"""
    gross = round(_finite(gross_amount, "含税金额"), 2)
    rate = _finite(vat_rate, "税率")
    if gross < 0 or rate < 0:
        raise ValueError("金额与税率不能为负数")
    net = round(gross / (1.0 + rate), 2)
    tax = round(gross - net, 2)
    return net, tax


@dataclass(frozen=True)
class MaterialCostLine:
    material_id: str
    material_name: str
    net_qty: float
    unit: str
    unit_price: float | None
    cost: float

    def out(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "material_name": self.material_name,
            "net_qty": round(self.net_qty, 4),
            "unit": self.unit,
            "unit_price": round(self.unit_price, 4) if self.unit_price is not None else None,
            "cost": round(self.cost, 2),
        }


@dataclass(frozen=True)
class BatchCostResult:
    batch_id: str
    product_id: str
    product_name: str
    qty: float
    unit: str
    material_cost: float
    energy_cost: float
    labor_cost: float
    overhead_cost: float
    total_cost: float
    unit_cost: float | None
    materials: list[MaterialCostLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "qty": round(self.qty, 4),
            "unit": self.unit,
            "material_cost": round(self.material_cost, 2),
            "energy_cost": round(self.energy_cost, 2),
            "labor_cost": round(self.labor_cost, 2),
            "overhead_cost": round(self.overhead_cost, 2),
            "total_cost": round(self.total_cost, 2),
            "unit_cost": round(self.unit_cost, 4) if self.unit_cost is not None else None,
            "materials": [m.out() for m in self.materials],
            "warnings": list(self.warnings),
        }


def compute_batch_cost(
    cfg: FinanceConfig,
    *,
    batch_id: str,
    batch_state: dict[str, Any],
    product_state: dict[str, Any] | None,
    stock_moves: list[dict[str, Any]],
    material_names: dict[str, str],
    material_prices: dict[str, float],
    kiln_run_state: dict[str, Any] | None = None,
    kiln_share_ratio: float = 1.0,
) -> BatchCostResult:
    """按领料（净耗）+ 窑次能耗（按占比分摊）+ 人工与制造费用，确定性核算生产批次成本。"""
    warnings: list[str] = []
    qty_raw = (
        batch_state.get("good_qty")
        if batch_state.get("good_qty") is not None
        else batch_state.get("planned_qty") or 0.0
    )
    qty = _finite(qty_raw, "批次数量")
    if qty < 0:
        raise ValueError("批次数量不能为负数")
    ratio = _finite(kiln_share_ratio, "窑次分摊比例")
    if ratio <= 0 or ratio > 1.0:
        raise ValueError("窑次分摊比例必须在 (0, 1] 内")

    # 1. 直接材料成本：汇总本批次领料 - 退料
    net_by_mat: dict[str, tuple[float, str]] = {}
    for mv in stock_moves:
        mid = str(mv.get("material") or "")
        if not mid:
            continue
        q = _finite(mv.get("qty") or 0.0, f"库存变动数量({mid})")
        kind = mv.get("kind")
        delta = q if kind == "issue" else (-q if kind == "return" else 0.0)
        if delta == 0.0:
            continue
        prev_q, prev_u = net_by_mat.get(mid, (0.0, str(mv.get("unit") or "")))
        net_by_mat[mid] = (prev_q + delta, prev_u or str(mv.get("unit") or ""))

    mat_lines: list[MaterialCostLine] = []
    material_cost = 0.0
    for mid in sorted(net_by_mat):
        net_q, unit = net_by_mat[mid]
        if net_q <= 0:
            continue
        price = material_prices.get(mid)
        if price is None:
            warnings.append(f"物料 {material_names.get(mid, mid)} 缺少采购单价，材料成本未计入该项")
            mat_lines.append(MaterialCostLine(mid, material_names.get(mid, mid), net_q, unit, None, 0.0))
        else:
            p = _finite(price, f"物料单价({mid})")
            c = round(net_q * p, 2)
            material_cost += c
            mat_lines.append(MaterialCostLine(mid, material_names.get(mid, mid), net_q, unit, p, c))
    if not mat_lines:
        warnings.append("本批次尚无领料记录，直接材料成本为 0")

    # 2. 窑炉能耗成本（电费 + 燃气费，按同窑次产量占比分摊）
    energy_cost = 0.0
    if kiln_run_state:
        kwh = _finite(kiln_run_state.get("energy_kwh") or 0.0, "窑次用电量")
        gas = _finite(kiln_run_state.get("gas_m3") or 0.0, "窑次燃气量")
        if kwh == 0.0 and gas == 0.0:
            warnings.append("关联窑次尚未登记用电/燃气能耗读数")
        energy_cost = round(
            (kwh * cfg.costing.electricity_yuan_per_kwh + gas * cfg.costing.gas_yuan_per_m3) * ratio, 2
        )
    else:
        warnings.append("本批次尚未关联烧成窑次，能耗成本为 0")

    # 3. 人工与制造费用（按件/单位定额）
    labor_cost = round(qty * cfg.costing.labor_yuan_per_unit, 2)
    overhead_cost = round(qty * cfg.costing.overhead_yuan_per_unit, 2)
    total_cost = round(material_cost + energy_cost + labor_cost + overhead_cost, 2)
    unit_cost = round(total_cost / qty, 4) if qty > 0 else None

    ps = product_state or {}
    return BatchCostResult(
        batch_id=batch_id,
        product_id=str(batch_state.get("product") or ""),
        product_name=str(ps.get("name") or ps.get("code") or batch_state.get("product") or ""),
        qty=qty,
        unit=str(batch_state.get("unit") or ""),
        material_cost=round(material_cost, 2),
        energy_cost=energy_cost,
        labor_cost=labor_cost,
        overhead_cost=overhead_cost,
        total_cost=total_cost,
        unit_cost=unit_cost,
        materials=mat_lines,
        warnings=warnings,
    )


def compute_order_margin(order_amount: float, batch_costs: list[BatchCostResult]) -> dict[str, Any]:
    """根据订单金额与关联生产批次成本，确定性计算订单毛利与毛利率。"""
    rev = round(_finite(order_amount, "订单金额"), 2)
    total_cost = round(sum(b.total_cost for b in batch_costs), 2)
    gross_profit = round(rev - total_cost, 2)
    margin_pct = round(100.0 * gross_profit / rev, 2) if rev > 0 else None
    return {
        "revenue": rev,
        "total_cost": total_cost,
        "gross_profit": gross_profit,
        "gross_margin_pct": margin_pct,
        "batches": [b.out() for b in batch_costs],
    }
