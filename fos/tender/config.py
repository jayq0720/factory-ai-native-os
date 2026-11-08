"""招标拆解配置：packs/<pack>/tender.yaml + tenants/<id>/tender.yaml（可选，按段整体覆盖）。

关键词、我方能力、提醒时间都是占位值，改配置不需要改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from fos.ontology import Ontology
from fos.tenancy.config import TenantConfig


class SectionDef(BaseModel):
    id: str
    name: str
    keywords: list[str]
    category: str = "商务"


class Markers(BaseModel):
    star: list[str] = Field(default_factory=lambda: ["★"])
    key: list[str] = Field(default_factory=lambda: ["▲"])
    must: list[str] = Field(default_factory=lambda: ["必须", "不得"])
    disqualify: list[str] = Field(default_factory=lambda: ["废标", "无效投标"])
    requirement: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    id: str
    name: str
    match: list[str]
    unit: str
    better: Literal["higher", "lower"]
    source: Literal["kb", "value", "credential"] = "value"
    property: str | None = None  # source=kb：产品属性
    value: float | None = None  # source=value：我方承诺值
    credential_kind: str | None = None  # source=credential：资料类别（取最新一份的签发日期算时效）
    default_rule: Literal["min", "max"] = "min"
    key_field: str | None = None  # 招标项目上的关键信息字段也拿来比（交货期、质保期）

    @model_validator(mode="after")
    def _check(self) -> Capability:
        if self.source == "kb" and not self.property:
            raise ValueError(f"{self.id}: source=kb 要给出 property")
        if self.source == "value" and self.value is None:
            raise ValueError(f"{self.id}: source=value 要给出 value")
        if self.source == "credential" and not self.credential_kind:
            raise ValueError(f"{self.id}: source=credential 要给出 credential_kind")
        return self


class DocumentDef(BaseModel):
    id: str
    name: str
    keywords: list[str]
    credential_kind: str | None = None


class Reminders(BaseModel):
    offsets_hours: list[float] = Field(default_factory=lambda: [72, 24, 3])
    notify_roles: list[str] = Field(default_factory=lambda: ["sales_lead"])
    poll_minutes: float = Field(default=30, gt=0)
    warn_days: float = Field(default=3, ge=0)


class TenderConfig(BaseModel):
    sections: list[SectionDef] = Field(default_factory=list)
    markers: Markers = Field(default_factory=Markers)
    categories: dict[str, list[str]] = Field(default_factory=dict)
    owners: dict[str, str] = Field(default_factory=dict)
    capabilities: list[Capability] = Field(default_factory=list)
    documents: list[DocumentDef] = Field(default_factory=list)
    reminders: Reminders = Field(default_factory=Reminders)

    def check(self, onto: Ontology) -> list[str]:
        errs = []
        if "tender" not in onto.objects:
            return ["招标配置需要本体里有 tender 对象"]
        product = onto.objects.get("product")
        cred = onto.objects.get("credential")
        kinds = set((cred.properties["kind"].values or []) if cred else [])
        for c in self.capabilities:
            if c.source == "kb" and (product is None or c.property not in product.properties):
                errs.append(f"招标能力 {c.id}: 产品没有属性 {c.property}")
            if c.credential_kind and c.credential_kind not in kinds:
                errs.append(f"招标能力 {c.id}: 资料类别 {c.credential_kind} 不在本体里")
            if c.key_field and c.key_field not in onto.objects["tender"].properties:
                errs.append(f"招标能力 {c.id}: 招标项目没有字段 {c.key_field}")
        for d in self.documents:
            if d.credential_kind and d.credential_kind not in kinds:
                errs.append(f"资料清单 {d.id}: 资料类别 {d.credential_kind} 不在本体里")
        # 建议负责人、提醒角色是行业包默认值：租户没有这个角色就不建议 / 不推（不算错）
        return errs


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_tender_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> TenderConfig | None:
    pack = packs_dir / tenant.industry_pack / "tender.yaml"
    own = tenants_dir / tenant.id / "tender.yaml"
    if not pack.exists() and not own.exists():
        return None
    return TenderConfig.model_validate({**_read(pack), **_read(own)})
