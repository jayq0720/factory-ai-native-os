"""窑炉监控配置：packs/<pack>/monitoring.yaml + tenants/<id>/monitoring.yaml（可选，按段整体覆盖）。"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from fos.tenancy.config import TenantConfig


class Curve(BaseModel):
    name: str
    tolerance: float = Field(gt=0)
    points: list[tuple[float, float]] = Field(min_length=2)  # (装窑后小时数, 目标温度)

    @field_validator("points")
    @classmethod
    def _sorted(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        hours = [h for h, _ in v]
        if hours[0] != 0 or any(b <= a for a, b in itertools.pairwise(hours)):
            raise ValueError("烧成制度的小时数必须从 0 开始并严格递增")
        return v

    @property
    def hours(self) -> float:
        return self.points[-1][0]


class Alarms(BaseModel):
    deviation_minutes: float = Field(default=5, ge=0)
    limit_minutes: float = Field(default=1, ge=0)
    edge_offline_minutes: float = Field(default=5, gt=0)
    stale_factor: float = Field(default=3, ge=1)
    manual_grace_minutes: float = Field(default=15, ge=0)
    poll_seconds: float = Field(default=30, gt=0)


class Notify(BaseModel):
    alarm_roles: list[str] = Field(default_factory=lambda: ["shift_lead"])
    manual_roles: list[str] = Field(default_factory=lambda: ["shift_lead"])
    repeat_minutes: float = Field(default=60, gt=0)


class Edge(BaseModel):
    """云端发给 fos-edge 的只读命令与厂端状态（W21）。"""

    command_ttl_s: int = Field(default=300, ge=30, le=3600)  # 每次签名的有效期（厂端上限 1 小时）
    queue_minutes: int = Field(default=30, ge=1, le=60)  # 排队多久没结果就作废（厂端按 id 去重的窗口内）
    status_every_minutes: float = Field(default=60, ge=0)  # 多久自动要一次状态；0 = 不自动要
    backlog_minutes: float = Field(default=30, gt=0)  # 网关在线但积压最早一批超过这么久 → 告警


class MonitorConfig(BaseModel):
    curves: dict[str, Curve] = Field(default_factory=dict)
    alarms: Alarms = Field(default_factory=Alarms)
    notify: Notify = Field(default_factory=Notify)
    edge: Edge = Field(default_factory=Edge)


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_monitor_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> MonitorConfig:
    pack = packs_dir / tenant.industry_pack / "monitoring.yaml"
    own = tenants_dir / tenant.id / "monitoring.yaml"
    return MonitorConfig.model_validate({**_read(pack), **_read(own)})
