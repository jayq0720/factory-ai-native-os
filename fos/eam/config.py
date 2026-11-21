"""设备管理（EAM）配置（packs/<pack>/eam.yaml + tenants/<id>/eam.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class EamConfig(BaseModel):
    pm_remind_days: int = Field(default=7, ge=0, le=90)
    auto_wo_on_abnormal: bool = True
    default_window_days: int = Field(default=30, ge=1, le=365)
    planned_hours_per_day: float = Field(default=24.0, gt=0, le=24.0)
    notify_roles: list[str] = Field(default_factory=lambda: ["shift_lead", "engineer"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_eam_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> EamConfig | None:
    p1 = packs_dir / tenant.industry_pack / "eam.yaml"
    p2 = tenants_dir / tenant.id / "eam.yaml"
    if not p1.exists() and not p2.exists():
        return None
    return EamConfig.model_validate({**_read(p1), **_read(p2)})
