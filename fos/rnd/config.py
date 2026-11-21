"""研发配方与试产配置（packs/<pack>/rnd.yaml + tenants/<id>/rnd.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class RndConfig(BaseModel):
    pct_tolerance: float = Field(default=0.5, ge=0.0, le=5.0)
    min_firing_temp_c: float = Field(default=100.0, ge=0.0)
    max_firing_temp_c: float = Field(default=1900.0, gt=0.0, le=3000.0)
    min_passed_trials_for_release: int = Field(default=1, ge=1, le=20)
    notify_roles: list[str] = Field(default_factory=lambda: ["rnd", "engineer"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_rnd_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> RndConfig | None:
    p1 = packs_dir / tenant.industry_pack / "rnd.yaml"
    p2 = tenants_dir / tenant.id / "rnd.yaml"
    if not p1.exists() and not p2.exists():
        return None
    return RndConfig.model_validate({**_read(p1), **_read(p2)})
