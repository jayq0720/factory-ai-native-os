"""筑炉施工与烘炉验收配置（packs/<pack>/construction.yaml + tenants/<id>/construction.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class ConstructionConfig(BaseModel):
    max_temp_deviation_c: float = Field(default=25.0, gt=0.0, le=200.0)
    max_ramp_rate_c_per_h: float = Field(default=35.0, gt=0.0, le=200.0)
    min_progress_for_accept_pct: float = Field(default=95.0, ge=0.0, le=100.0)
    notify_roles: list[str] = Field(default_factory=lambda: ["engineer", "shift_lead"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_construction_config(
    tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path
) -> ConstructionConfig | None:
    p1 = packs_dir / tenant.industry_pack / "construction.yaml"
    p2 = tenants_dir / tenant.id / "construction.yaml"
    if not p1.exists() and not p2.exists():
        return None
    return ConstructionConfig.model_validate({**_read(p1), **_read(p2)})
