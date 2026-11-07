"""计划与排窑配置：packs/<pack>/planning.yaml + tenants/<id>/planning.yaml（可选，按段覆盖）。

都是占位值，待试点现场确认；改配置不需要改代码。窑炉装量、周期是主数据（账本事件），不在配置里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class Weights(BaseModel):
    shortfall: int = Field(default=1000, ge=0)
    late_hour: int = Field(default=10, ge=0)
    run: int = Field(default=50, ge=0)
    finish_hour: int = Field(default=1, ge=0)


class SolverConfig(BaseModel):
    time_limit_s: float = Field(default=10, gt=0, le=600)
    workers: int = Field(default=8, ge=1, le=64)
    weights: Weights = Field(default_factory=Weights)


class MaterialConfig(BaseModel):
    stock_statuses: list[str] = Field(default_factory=lambda: ["accepted", "concession"])
    incoming_statuses: list[str] = Field(default_factory=lambda: ["pending_inspection"])
    po_statuses: list[str] = Field(default_factory=lambda: ["open"])


class WatchConfig(BaseModel):
    poll_minutes: float = Field(default=5, gt=0)
    lookback_hours: float = Field(default=24, gt=0)
    notify_roles: list[str] = Field(default_factory=lambda: ["planner"])


class PlanningConfig(BaseModel):
    horizon_days: int = Field(default=14, ge=1, le=90)
    pre_firing_hours: int = Field(default=48, ge=0)
    post_firing_hours: int = Field(default=48, ge=0)
    default_cycle_hours: int = Field(default=48, ge=1)
    max_runs_per_kiln: int = Field(default=60, ge=1, le=500)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    materials: MaterialConfig = Field(default_factory=MaterialConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    shortage_notify_roles: list[str] = Field(default_factory=lambda: ["purchaser"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_planning_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> PlanningConfig:
    pack = _read(packs_dir / tenant.industry_pack / "planning.yaml")
    own = _read(tenants_dir / tenant.id / "planning.yaml")
    merged = {**pack, **own}
    for key in ("solver", "materials", "watch"):
        merged[key] = {**(pack.get(key) or {}), **(own.get(key) or {})}
    return PlanningConfig.model_validate(merged)
