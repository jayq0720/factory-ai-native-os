"""主数据导入配置：packs/<pack>/import.yaml + tenants/<id>/import.yaml（可选）。

启动时对照本体检查（事件存在、第一个是创建事件、key 属性存在），不一致拒绝启动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fos.ontology import Ontology, PropertyDef
from fos.tenancy.config import TenantConfig

ID_COLUMN = "编号"


class SheetDef(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=31)  # 工作表名（Excel 限 31 字）
    events: list[str] = Field(min_length=1)
    key: str  # 判断已存在、被引用时按这个属性查找


class Column(BaseModel):
    field: str
    header: str  # 中文名，必填带 *
    prop: PropertyDef
    required: bool


class ImportConfig(BaseModel):
    sheets: list[SheetDef] = Field(default_factory=list)
    value_names: dict[str, str] = Field(default_factory=dict)

    def sheet(self, sid: str) -> SheetDef | None:
        return next((s for s in self.sheets if s.id == sid), None)

    def object_of(self, onto: Ontology, s: SheetDef) -> str:
        return onto.events[s.events[0]].object

    def columns(self, onto: Ontology, s: SheetDef) -> list[Column]:
        """工作表的列：各事件字段按出现顺序去重。必填 = 对象上的必填属性（创建时必须给）。"""
        obj = onto.objects[self.object_of(onto, s)]
        seen: dict[str, Column] = {}
        for ev in s.events:
            for f in onto.events[ev].fields:
                if f in seen:
                    continue
                p = obj.properties[f]
                seen[f] = Column(
                    field=f, header=p.name + ("*" if p.required else ""), prop=p, required=p.required
                )
        return list(seen.values())

    def key_sheet(self, onto: Ontology, object_type: str) -> SheetDef | None:
        """被引用的对象类型对应哪张表（用它的 key 查找）。"""
        return next((s for s in self.sheets if self.object_of(onto, s) == object_type), None)

    def check(self, onto: Ontology) -> list[str]:
        errs: list[str] = []
        ids, names = set(), set()
        for s in self.sheets:
            if s.id in ids or s.name in names:
                errs.append(f"import.sheets: 工作表 {s.id}/{s.name} 重复")
            ids.add(s.id)
            names.add(s.name)
            missing = [e for e in s.events if e not in onto.events]
            if missing:
                errs.append(f"import.sheets.{s.id}: 没有事件 {missing}")
                continue
            first = onto.events[s.events[0]]
            if first.op != "create":
                errs.append(f"import.sheets.{s.id}: 第一个事件必须是创建事件")
            if first.evidence == "required":
                errs.append(f"import.sheets.{s.id}: {s.events[0]} 必须附原始单据，不适合批量导入")
            others = {onto.events[e].object for e in s.events}
            if len(others) != 1:
                errs.append(f"import.sheets.{s.id}: 事件必须作用于同一类对象")
            if any(onto.events[e].op != "update" for e in s.events[1:]):
                errs.append(f"import.sheets.{s.id}: 第二个起必须是更新事件")
            if s.key not in onto.objects[first.object].properties:
                errs.append(f"import.sheets.{s.id}: {first.object} 没有属性 {s.key}")
            elif s.key not in first.fields:
                errs.append(f"import.sheets.{s.id}: key {s.key} 不在创建事件的字段里")
        return errs


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_import_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> ImportConfig:
    """行业包 + 租户：租户的同 id 工作表覆盖行业包的，新的追加在后面；枚举中文按值覆盖。"""
    pack = _read(packs_dir / tenant.industry_pack / "import.yaml")
    own = _read(tenants_dir / tenant.id / "import.yaml")
    sheets = {s["id"]: s for s in pack.get("sheets", [])}
    for s in own.get("sheets", []):
        sheets[s["id"]] = s
    return ImportConfig.model_validate(
        {
            "sheets": list(sheets.values()),
            "value_names": {**pack.get("value_names", {}), **own.get("value_names", {})},
        }
    )
