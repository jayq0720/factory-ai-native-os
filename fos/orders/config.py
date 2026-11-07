"""订单交付配置：packs/<pack>/orders.yaml + tenants/<id>/orders.yaml（可选，按段覆盖）。

阈值都是占位值，待试点现场确认；改配置不需要改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class ShippingRules(BaseModel):
    """发货前校验。policy.yaml 的规则只看得到单个对象，这几条要跨对象（批次、检验、已发数量），所以放这里。"""

    require_in_stock: bool = True  # 只能发已入库的批次
    require_final_inspection: bool = True  # 每个批次要有出厂检验记录且结论合格
    allow_conditional: bool = True  # 有条件放行的批次可以发（提示）
    allow_other_order_batches: bool = False  # 能否发给别的订单排产的批次（同产品）
    over_ship_pct: float = Field(default=3, ge=0, le=100)  # 超发容差：订单数量 × (1 + 3%)


class RiskRules(BaseModel):
    warn_days: int = Field(default=7, ge=0)  # 距交期 N 天内计划量还不够 → 交期风险
    stock_days: int = Field(default=2, ge=0)  # 距交期 N 天内入库量还不够发 → 交期风险


class OrdersConfig(BaseModel):
    shipping: ShippingRules = Field(default_factory=ShippingRules)
    risk: RiskRules = Field(default_factory=RiskRules)
    # 单位同义词：订单、批次、发货的单位按这个归一后比较（不做换算）
    unit_aliases: dict[str, list[str]] = Field(default_factory=dict)

    def norm_unit(self, u: str | None) -> str:
        u = (u or "").strip()
        for canon, names in self.unit_aliases.items():
            if u == canon or u in names:
                return canon
        return u

    def same_unit(self, a: str | None, b: str | None) -> bool:
        return self.norm_unit(a) == self.norm_unit(b)


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_orders_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> OrdersConfig:
    pack = _read(packs_dir / tenant.industry_pack / "orders.yaml")
    own = _read(tenants_dir / tenant.id / "orders.yaml")
    merged: dict[str, Any] = {}
    for key in ("shipping", "risk"):
        merged[key] = {**(pack.get(key) or {}), **(own.get(key) or {})}
    merged["unit_aliases"] = {**(pack.get("unit_aliases") or {}), **(own.get("unit_aliases") or {})}
    return OrdersConfig.model_validate(merged)
