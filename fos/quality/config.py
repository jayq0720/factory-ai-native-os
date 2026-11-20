"""质量体系配置加载（packs/<pack>/quality.yaml + tenants/<id>/quality.yaml）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class SpecLimit(BaseModel):
    name: str
    unit: str = ""
    min: float | None = None
    max: float | None = None


class NcrSettings(BaseModel):
    auto_open_on_fail: bool = True
    require_8d_severities: list[str] = Field(default_factory=lambda: ["major", "critical"])
    notify_roles: list[str] = Field(default_factory=lambda: ["qc", "shift_lead"])


class CalibrationSettings(BaseModel):
    remind_days: int = Field(default=30, ge=1, le=365)
    urgent_days: int = Field(default=7, ge=1, le=90)
    notify_roles: list[str] = Field(default_factory=lambda: ["qc"])


class CertificateSettings(BaseModel):
    issuer_title: str = "产品质量证明书（质保书）"
    standard_ref: str = "企业内控标准"
    footer_note: str = "本质保书由事件账本确定性生成。"


class QualityConfig(BaseModel):
    standards: dict[str, dict[str, SpecLimit]] = Field(default_factory=dict)
    density_tolerance_pct: float = Field(default=10.0, gt=0, le=50)
    ncr: NcrSettings = Field(default_factory=NcrSettings)
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings)
    certificate: CertificateSettings = Field(default_factory=CertificateSettings)

    def spec_for(self, category: str | None) -> dict[str, SpecLimit]:
        base = dict(self.standards.get("default", {}))
        if category and category in self.standards:
            base.update(self.standards[category])
        return base


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_quality_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> QualityConfig:
    pack = _read(packs_dir / tenant.industry_pack / "quality.yaml")
    own = _read(tenants_dir / tenant.id / "quality.yaml")
    merged: dict[str, Any] = {**pack, **own}
    if "standards" in pack and "standards" in own:
        merged["standards"] = {**pack["standards"], **own["standards"]}
    return QualityConfig.model_validate(merged)
