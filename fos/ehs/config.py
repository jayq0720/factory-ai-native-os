"""EHS 安全环保配置（packs/<pack>/ehs.yaml + tenants/<id>/ehs.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class EnvLimit(BaseModel):
    name: str
    unit: str
    max: float = Field(gt=0)


class EhsConfig(BaseModel):
    env_limits: dict[str, EnvLimit] = Field(default_factory=dict)
    auto_hazard_on_env_exceed: bool = True
    hazard_remind_days: int = Field(default=3, ge=0, le=90)
    max_permit_hours: int = Field(default=72, ge=1, le=720)
    notify_roles: list[str] = Field(default_factory=lambda: ["shift_lead", "engineer"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_ehs_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> EhsConfig | None:
    p1 = packs_dir / tenant.industry_pack / "ehs.yaml"
    p2 = tenants_dir / tenant.id / "ehs.yaml"
    if not p1.exists() and not p2.exists():
        return None
    d1, d2 = _read(p1), _read(p2)
    merged = {**d1, **d2}
    if "env_limits" in d1 or "env_limits" in d2:
        merged["env_limits"] = {**(d1.get("env_limits") or {}), **(d2.get("env_limits") or {})}
    return EhsConfig.model_validate(merged)
