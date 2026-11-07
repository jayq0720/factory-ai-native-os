"""字段抽取 Schema：放在行业包 packs/<pack>/extraction/*.yaml，
租户可在 tenants/<id>/extraction/ 覆盖同名 Schema。

一份 Schema 对应一类文件（技术文件、招标文件……），按上传时的 kind 选用。
加字段、改叫法只改 YAML，不改 Python。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from fos.entry.spec import EntrySpec
from fos.tenancy.config import TenantConfig

FieldType = Literal["text", "number", "temperature", "date", "money", "enum", "list"]


class SchemaError(ValueError):
    pass


class FieldDef(BaseModel):
    key: str
    name: str
    group: str
    type: FieldType = "text"
    unit: str | None = None  # 默认单位；原文写了单位以原文为准
    labels: list[str] = Field(default_factory=list)  # 原文里的常见叫法，越靠前越可信
    exclude_before: list[str] = Field(default_factory=list)  # 叫法前面紧挨着这些词时不算（"最高"工作温度）
    enum: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)  # 不靠叫法、直接在全文里找的正则（标准号、★条款）
    scan_all: bool = False  # enum 值不跟在叫法后面也算（如标题里的"辊道窑"），可信度较低
    multi: bool = False  # 可以有多个值（气氛、部位、标准号）
    required: bool = False  # 缺失时在审核清单里提示
    description: str = ""  # 给模型的说明

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"字段 key 只能用小写字母、数字、下划线：{v!r}")
        return v

    @model_validator(mode="after")
    def _check(self) -> FieldDef:
        if self.type == "enum" and not self.enum:
            raise ValueError(f"{self.key}: enum 字段要列出 enum 取值")
        if not (self.labels or self.patterns or (self.enum and self.scan_all)):
            raise ValueError(f"{self.key}: 至少要有 labels、patterns 或 scan_all 的 enum 之一")
        for p in self.patterns:
            re.compile(p)
        return self


class ExtractionSchema(BaseModel):
    id: str
    name: str
    version: int = 1
    kinds: list[str]  # 适用的文件类别（上传时的 kind）
    fields: list[FieldDef]
    detect: list[str] = Field(default_factory=list)  # 同一类别有多份 Schema 时，按原文里的这些词自动选
    entry: EntrySpec | None = None  # 录单：确认后写哪个事件（fos/entry/spec.py）
    source: str = ""

    @model_validator(mode="after")
    def _unique(self) -> ExtractionSchema:
        keys = [f.key for f in self.fields]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ValueError(f"Schema {self.id} 字段 key 重复：{sorted(dup)}")
        return self

    def field(self, key: str) -> FieldDef | None:
        return next((f for f in self.fields if f.key == key), None)

    @property
    def groups(self) -> list[str]:
        return list(dict.fromkeys(f.group for f in self.fields))


def _load_dir(d: Path, source: str) -> dict[str, ExtractionSchema]:
    out: dict[str, ExtractionSchema] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.yaml")):
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            s = ExtractionSchema.model_validate({**raw, "source": source})
        except Exception as e:
            raise SchemaError(f"{f}: {e}") from e
        out[s.id] = s
    return out


def load_schemas(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> dict[str, ExtractionSchema]:
    schemas = _load_dir(packs_dir / tenant.industry_pack / "extraction", tenant.industry_pack)
    schemas.update(_load_dir(tenants_dir / tenant.id / "extraction", f"tenant:{tenant.id}"))
    return schemas


def schema_for_kind(schemas: dict[str, ExtractionSchema], kind: str) -> ExtractionSchema | None:
    return next((s for s in schemas.values() if kind in s.kinds), None)


def detect_scores(schemas: dict[str, ExtractionSchema], kind: str | None, text: str) -> list[tuple[int, str]]:
    """按 detect 词在原文里出现的次数给 Schema 打分（零模型成本），高分在前。"""
    t = re.sub(r"\s+", "", text)
    out = []
    for s in schemas.values():
        if not s.detect or (kind is not None and kind not in s.kinds):
            continue
        n = sum(t.count(w) for w in s.detect)
        if n:
            out.append((n, s.id))
    return sorted(out, key=lambda x: (-x[0], x[1]))


def detect_schema(
    schemas: dict[str, ExtractionSchema], kind: str | None, text: str
) -> ExtractionSchema | None:
    """原文最像哪种单据。两份 Schema 同分时不猜，返回 None。"""
    scores = detect_scores(schemas, kind, text)
    if not scores or (len(scores) > 1 and scores[0][0] == scores[1][0]):
        return None
    return schemas[scores[0][1]]
