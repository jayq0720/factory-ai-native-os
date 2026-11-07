"""录单映射：一份单据的抽取字段如何变成一个本体事件。

写在抽取 Schema 的 `entry:` 段里（packs/<pack>/extraction/*.yaml），加一种单据只改 YAML：

    entry:
      event: warehouse.lot_received          # 确认后追加的事件
      object_id: {auto: L}                   # 新建对象的编号：auto 前缀 / from 某个字段
      occurred_at: received_at               # 事件发生时间取 payload 里的哪个字段（可选）
      fields:                                # 事件字段 ← 抽取字段
        material: {from: material_name, lookup: [name]}     # 引用：按名称查已有对象
        qty: {from: qty}
        unit: {from: qty, part: unit, default: t}
        received_at: {from: date, default: now}
        status: {const: pending_inspection}

更新类事件（如批次完工）的 object_id 用 {from: batch_no, lookup: []} 指向已有对象。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EntryValue(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source: str | None = Field(default=None, alias="from")  # 抽取字段 key
    part: Literal["value", "unit"] = "value"
    const: Any = None  # 固定值（不来自单据）
    default: Any = None  # 单据上没有时用；now / today 表示录单当时
    lookup: list[str] | None = None  # 引用字段：按对象 id 和这些属性查已有对象
    map: dict[str, str] = Field(default_factory=dict)  # 原文 → 枚举值（如 合格 → pass）
    auto: str | None = None  # 只用于 object_id：自动编号前缀

    @model_validator(mode="after")
    def _check(self) -> EntryValue:
        if self.source is None and self.const is None and self.default is None and self.auto is None:
            raise ValueError("要给出 from、const、default 或 auto 之一")
        if self.auto is not None and not re.fullmatch(r"[A-Z]{1,4}", self.auto):
            raise ValueError("auto 前缀只能是 1–4 个大写字母")
        return self


class EntrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    object_id: EntryValue | None = None
    occurred_at: str | None = None
    fields: dict[str, EntryValue]


def check_entry(schema_id: str, field_keys: set[str], spec: EntrySpec, onto: Any) -> list[str]:
    """启动时对照本体检查映射，返回问题列表（空表示没问题）。"""
    errs: list[str] = []
    ev = onto.events.get(spec.event)
    if ev is None:
        return [f"{schema_id}: 事件 {spec.event} 不在本体里"]
    props = onto.objects[ev.object].properties
    for k, v in spec.fields.items():
        if k not in ev.fields:
            errs.append(f"{schema_id}: {spec.event} 没有字段 {k}")
            continue
        if v.source and v.source not in field_keys:
            errs.append(f"{schema_id}: {k} 取自不存在的抽取字段 {v.source}")
        p = props[k]
        if v.lookup is not None and p.type != "ref":
            errs.append(f"{schema_id}: {k} 不是引用属性，不能 lookup")
        if v.lookup is not None:
            target = onto.objects[p.target].properties
            errs += [f"{schema_id}: {p.target} 没有属性 {a}" for a in v.lookup if a not in target]
        if p.type == "ref" and v.source and v.lookup is None:
            errs.append(f"{schema_id}: 引用字段 {k} 需要写 lookup（可以是 []，表示只按编号查）")
        if v.auto is not None:
            errs.append(f"{schema_id}: auto 只能用在 object_id 上")
    if ev.op == "create":
        missing = [k for k, p in props.items() if p.required and k in ev.fields and k not in spec.fields]
        if missing:
            errs.append(f"{schema_id}: 创建事件缺少必填字段映射 {missing}")
    elif spec.object_id is None or spec.object_id.source is None:
        errs.append(f"{schema_id}: 更新事件 {spec.event} 需要 object_id: {{from: ..., lookup: [...]}}")
    if spec.object_id and spec.object_id.source and spec.object_id.source not in field_keys:
        errs.append(f"{schema_id}: object_id 取自不存在的抽取字段 {spec.object_id.source}")
    if spec.occurred_at and spec.occurred_at not in spec.fields:
        errs.append(f"{schema_id}: occurred_at 应是 fields 里的一个字段")
    return errs
