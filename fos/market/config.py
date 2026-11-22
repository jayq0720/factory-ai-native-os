"""市场商机与经营决策配置（packs/<pack>/market.yaml + tenants/<id>/market.yaml 覆盖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.tenancy.config import TenantConfig


class MarketConfig(BaseModel):
    stage_probabilities: dict[str, int] = Field(
        default_factory=lambda: {
            "new": 10,
            "qualified": 30,
            "quoted": 50,
            "negotiation": 75,
            "won": 100,
            "lost": 0,
        }
    )
    stale_lead_days: int = Field(default=14, ge=1, le=180)
    large_lead_amount: float = Field(default=500000.0, gt=0.0)
    notify_roles: list[str] = Field(default_factory=lambda: ["sales_lead", "gm"])


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_market_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> MarketConfig | None:
    p1 = packs_dir / tenant.industry_pack / "market.yaml"
    p2 = tenants_dir / tenant.id / "market.yaml"
    if not p1.exists() and not p2.exists():
        return None
    d1, d2 = _read(p1), _read(p2)
    merged = {**d1, **d2}
    if "stage_probabilities" in d1 or "stage_probabilities" in d2:
        merged["stage_probabilities"] = {
            **(d1.get("stage_probabilities") or {}),
            **(d2.get("stage_probabilities") or {}),
        }
    return MarketConfig.model_validate(merged)
