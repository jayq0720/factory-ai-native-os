from __future__ import annotations

from pathlib import Path

import yaml

from fos.ontology.schema import Ontology, OntologyLayer
from fos.tenancy import TenantConfig

CORE = Path(__file__).with_name("core.yaml")


def _read(path: Path) -> OntologyLayer:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return OntologyLayer.model_validate(data)


def load_ontology(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> Ontology:
    """核心 → 行业包 → 租户，三层合并并整体校验。"""
    onto = Ontology()
    onto.add_layer("core", _read(CORE))
    pack = packs_dir / tenant.industry_pack / "ontology.yaml"
    if pack.exists():
        onto.add_layer(f"pack:{tenant.industry_pack}", _read(pack))
    own = tenants_dir / tenant.id / "ontology.yaml"
    if own.exists():
        onto.add_layer(f"tenant:{tenant.id}", _read(own))
    onto.validate()
    return onto
