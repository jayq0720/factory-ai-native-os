from __future__ import annotations

from pathlib import Path

import yaml

from fos.ontology import Ontology
from fos.policy.schema import Policy, PolicyLayer
from fos.tenancy import TenantConfig


def _read(path: Path) -> PolicyLayer:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PolicyLayer.model_validate(data)


def load_policy(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path, ontology: Ontology) -> Policy:
    """行业包 → 租户，两层合并并整体校验。两个文件都没有时，所有事件直接放行。"""
    policy = Policy()
    pack = packs_dir / tenant.industry_pack / "policy.yaml"
    if pack.exists():
        policy.add_layer(f"pack:{tenant.industry_pack}", _read(pack))
    own = tenants_dir / tenant.id / "policy.yaml"
    if own.exists():
        policy.add_layer(f"tenant:{tenant.id}", _read(own))
    policy.validate(ontology, set(tenant.roles))
    return policy
