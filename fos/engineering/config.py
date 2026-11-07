"""衬里结构模板与选材参数：packs/<pack>/engineering/linings.yaml，租户可整体替换。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from fos.ontology import Ontology
from fos.tenancy import TenantConfig


class LayerSpec(BaseModel):
    role: str
    categories: list[str] = Field(min_length=1)
    thickness: list[int] = Field(min_length=1)  # 可选厚度 mm；0 表示这一层可省略

    @model_validator(mode="after")
    def _check(self) -> LayerSpec:
        if any(t < 0 for t in self.thickness):
            raise ValueError("厚度不能为负")
        return self


class Template(BaseModel):
    id: str
    name: str
    furnace_types: list[str] = Field(default_factory=list)  # 空 = 不限
    positions: list[str] = Field(default_factory=list)  # 空 = 不限
    temp_range: tuple[float, float]
    layers: list[LayerSpec] = Field(min_length=1)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Template:
        if 0 in self.layers[0].thickness:
            raise ValueError(f"模板 {self.id} 的工作层不能省略")
        return self


class Surface(BaseModel):
    emissivity: float = 0.8
    convection: dict[str, float] = Field(default_factory=lambda: {"wall": 1.31, "roof": 1.52, "floor": 0.64})


class QuoteDefaults(BaseModel):
    tax_rate: float = 13
    freight_per_t: float = 0
    install_per_m2: float = 0
    low_margin_pct: float = 15


class Auxiliary(BaseModel):
    name: str
    category: str
    per: str = "brick_volume"
    rate: float
    unit: str = "t"


class EngineeringConfig(BaseModel):
    version: int = 1
    safety_margin: float = 100
    backup_margin: float = 50
    default_shell_temp: float = 80
    default_ambient: float = 25
    surface: Surface = Field(default_factory=Surface)
    quote: QuoteDefaults = Field(default_factory=QuoteDefaults)
    auxiliaries: list[Auxiliary] = Field(default_factory=list)
    templates: list[Template] = Field(default_factory=list)


def load_engineering(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> EngineeringConfig | None:
    for path in (
        tenants_dir / tenant.id / "engineering" / "linings.yaml",
        packs_dir / tenant.industry_pack / "engineering" / "linings.yaml",
    ):
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return EngineeringConfig.model_validate(data)
    return None


def check_engineering(tenant_id: str, cfg: EngineeringConfig, onto: Ontology) -> None:
    """模板里的产品类别必须是本体 product.category 的取值；工程对象必须在本体里。"""
    if "eng_project" not in onto.objects or "price_item" not in onto.objects:
        raise ValueError(f"[{tenant_id}] 配置了工程模板，但本体里没有 eng_project / price_item")
    cat = onto.objects["product"].properties.get("category")
    allowed = set(cat.values or []) if cat else set()
    for t in cfg.templates:
        for ly in t.layers:
            bad = set(ly.categories) - allowed
            if bad:
                raise ValueError(f"[{tenant_id}] 衬里模板 {t.id} 用了本体里没有的产品类别：{sorted(bad)}")
    for a in cfg.auxiliaries:
        if a.category not in allowed:
            raise ValueError(f"[{tenant_id}] 辅料 {a.name} 的类别 {a.category} 不在本体里")
