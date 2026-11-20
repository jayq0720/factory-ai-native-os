"""财务集成与成本核算配置（packs/<pack>/finance.yaml + tenants/<id>/finance.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class AccountDef(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)


class AccountsConfig(BaseModel):
    bank: AccountDef = Field(default_factory=lambda: AccountDef(code="1002", name="银行存款"))
    ar: AccountDef = Field(default_factory=lambda: AccountDef(code="1122", name="应收账款"))
    ap: AccountDef = Field(default_factory=lambda: AccountDef(code="2202", name="应付账款"))
    revenue: AccountDef = Field(default_factory=lambda: AccountDef(code="6001", name="主营业务收入"))
    tax_out: AccountDef = Field(
        default_factory=lambda: AccountDef(code="2221.01.05", name="应交税费-应交增值税(销项税额)")
    )
    tax_in: AccountDef = Field(
        default_factory=lambda: AccountDef(code="2221.01.01", name="应交税费-应交增值税(进项税额)")
    )
    raw_material: AccountDef = Field(default_factory=lambda: AccountDef(code="1403", name="原材料"))
    wip: AccountDef = Field(default_factory=lambda: AccountDef(code="5001", name="生产成本"))
    finished_goods: AccountDef = Field(default_factory=lambda: AccountDef(code="1405", name="库存商品"))
    mfg_overhead: AccountDef = Field(default_factory=lambda: AccountDef(code="5101", name="制造费用"))


class CostingConfig(BaseModel):
    electricity_yuan_per_kwh: float = Field(default=0.78, ge=0)
    gas_yuan_per_m3: float = Field(default=3.6, ge=0)
    labor_yuan_per_unit: float = Field(default=1.2, ge=0)
    overhead_yuan_per_unit: float = Field(default=1.5, ge=0)


class FinanceReminderConfig(BaseModel):
    remind_before_days: int = Field(default=7, ge=0, le=90)
    notify_roles: list[str] = Field(default_factory=lambda: ["finance", "sales"])


class FinanceConfig(BaseModel):
    vat_rate: float = Field(default=0.13, ge=0, le=0.5)
    accounts: AccountsConfig = Field(default_factory=AccountsConfig)
    costing: CostingConfig = Field(default_factory=CostingConfig)
    aging_buckets_days: list[int] = Field(default_factory=lambda: [30, 60, 90])
    reminders: FinanceReminderConfig = Field(default_factory=FinanceReminderConfig)


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_finance_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> FinanceConfig | None:
    p1 = packs_dir / tenant.industry_pack / "finance.yaml"
    p2 = tenants_dir / tenant.id / "finance.yaml"
    if not p1.exists() and not p2.exists():
        return None
    return FinanceConfig.model_validate(_deep_merge(_read(p1), _read(p2)))
