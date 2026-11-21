"""人力、排班、计件与特种资质配置（packs/<pack>/hr.yaml + tenants/<id>/hr.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class HrConfig(BaseModel):
    cert_remind_days: int = Field(default=30, ge=0, le=180)
    standard_work_days: int = Field(default=22, ge=1, le=31)
    overtime_multiplier: float = Field(default=1.5, ge=1.0, le=5.0)
    default_piece_rate: float = Field(default=12.0, ge=0.0)
    max_shift_hours: float = Field(default=16.0, gt=0.0, le=24.0)
    notify_roles: list[str] = Field(default_factory=lambda: ["shift_lead", "admin"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_hr_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> HrConfig | None:
    p1 = packs_dir / tenant.industry_pack / "hr.yaml"
    p2 = tenants_dir / tenant.id / "hr.yaml"
    if not p1.exists() and not p2.exists():
        return None
    return HrConfig.model_validate({**_read(p1), **_read(p2)})
