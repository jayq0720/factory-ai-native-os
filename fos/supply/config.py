"""采购与仓储配置：packs/<pack>/supply.yaml + tenants/<id>/supply.yaml（可选，按段覆盖）。

都是占位值，待试点现场确认；改配置不需要改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class StockConfig(BaseModel):
    usable_statuses: list[str] = Field(default_factory=lambda: ["accepted", "concession"])
    incoming_statuses: list[str] = Field(default_factory=lambda: ["pending_inspection"])
    low_stock_notify_roles: list[str] = Field(default_factory=lambda: ["warehouse", "purchaser"])


class ReceivingConfig(BaseModel):
    over_receive_pct: float = Field(default=5, ge=0, le=100)
    closed_statuses: list[str] = Field(default_factory=lambda: ["closed", "cancelled"])


class RequisitionConfig(BaseModel):
    round_to_min_qty: bool = True


class SupplierWeights(BaseModel):
    quality: float = Field(default=60, ge=0)
    on_time: float = Field(default=40, ge=0)


class SupplierConfig(BaseModel):
    window_days: int = Field(default=180, ge=1, le=3650)
    weights: SupplierWeights = Field(default_factory=SupplierWeights)


class SupplyConfig(BaseModel):
    stock: StockConfig = Field(default_factory=StockConfig)
    receiving: ReceivingConfig = Field(default_factory=ReceivingConfig)
    requisition: RequisitionConfig = Field(default_factory=RequisitionConfig)
    suppliers: SupplierConfig = Field(default_factory=SupplierConfig)


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_supply_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> SupplyConfig:
    pack = _read(packs_dir / tenant.industry_pack / "supply.yaml")
    own = _read(tenants_dir / tenant.id / "supply.yaml")
    merged = {k: {**(pack.get(k) or {}), **(own.get(k) or {})} for k in SupplyConfig.model_fields}
    return SupplyConfig.model_validate(merged)
