"""企业本体：对象类型、属性、引用关系与事件类型（方案 5.1 原则 1、5.2）。

三层合并：核心本体（fos/ontology/core.yaml）→ 行业包（packs/<pack>/ontology.yaml）
→ 租户（tenants/<id>/ontology.yaml，可选）。后加载的层可以：
- 新增对象类型、事件类型；
- 给已有对象类型**追加**属性。
不能修改或删除已有属性、不能重定义事件类型，以保证上层的 Skill 和数据不被下层打破。

事件类型声明它作用在哪个对象上（create / update），payload 的字段就是该对象的属性。
投影（对象当前状态）因此可以完全由事件机械地算出，不需要为每个事件写代码。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from fos.governance.permissions import is_valid_permission

PropType = Literal["string", "text", "integer", "number", "bool", "date", "datetime", "enum", "ref"]
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_EVENT_KEY = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class OntologyError(ValueError):
    pass


class PropertyDef(BaseModel):
    type: PropType
    name: str  # 中文显示名
    required: bool = False
    unit: str | None = None  # 物理单位，例如 t、℃、g/cm³、元
    values: list[str] | None = None  # enum 取值
    target: str | None = None  # ref 指向的对象类型
    many: bool = False  # ref 是否为列表（例如批次用了多个原料批）

    @model_validator(mode="after")
    def _check(self) -> PropertyDef:
        if self.type == "enum" and not self.values:
            raise ValueError("enum 属性必须给出 values")
        if self.type == "ref" and not self.target:
            raise ValueError("ref 属性必须给出 target")
        if self.many and self.type != "ref":
            raise ValueError("只有 ref 属性可以 many")
        return self


class ObjectTypeDef(BaseModel):
    name: str
    domain: str  # 权限域，例如 warehouse、quality；读对象需要 <domain>.<key>:read
    description: str = ""
    properties: dict[str, PropertyDef] = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _keys(cls, v: dict[str, PropertyDef]) -> dict[str, PropertyDef]:
        for k in v:
            if not _KEY.match(k):
                raise ValueError(f"属性名不合法：{k}")
        return v


class EventTypeDef(BaseModel):
    name: str
    object: str  # 作用的对象类型
    op: Literal["create", "update"]
    permission: str  # 追加该事件需要的权限
    evidence: Literal["required", "optional"] = "optional"
    fields: list[str]  # payload 允许的字段（对象属性名）
    description: str = ""

    @field_validator("permission")
    @classmethod
    def _perm(cls, v: str) -> str:
        if not is_valid_permission(v) or "*" in v:
            raise ValueError(f"事件权限必须是不带通配的权限串：{v}")
        return v


class OntologyLayer(BaseModel):
    """一个 YAML 文件的内容。"""

    objects: dict[str, ObjectTypeDef] = Field(default_factory=dict)
    extend: dict[str, dict[str, PropertyDef]] = Field(default_factory=dict)  # 给已有对象追加属性
    events: dict[str, EventTypeDef] = Field(default_factory=dict)


class Ontology:
    def __init__(self) -> None:
        self.objects: dict[str, ObjectTypeDef] = {}
        self.events: dict[str, EventTypeDef] = {}
        self.layers: list[str] = []

    # ---------- 合并 ----------
    def add_layer(self, name: str, layer: OntologyLayer) -> None:
        for key, obj in layer.objects.items():
            if not _KEY.match(key):
                raise OntologyError(f"[{name}] 对象类型名不合法：{key}")
            if key in self.objects:
                raise OntologyError(f"[{name}] 对象类型 {key} 已存在，只能用 extend 追加属性")
            self.objects[key] = obj.model_copy(deep=True)
        for key, props in layer.extend.items():
            target = self.objects.get(key)
            if target is None:
                raise OntologyError(f"[{name}] extend 的对象类型 {key} 不存在")
            for pk, pd in props.items():
                if pk in target.properties:
                    raise OntologyError(f"[{name}] {key}.{pk} 已存在，不能覆盖")
                if pd.required:
                    raise OntologyError(f"[{name}] 追加的属性 {key}.{pk} 不能是必填（会让已有数据失效）")
                target.properties[pk] = pd
        for key, ev in layer.events.items():
            if not _EVENT_KEY.match(key):
                raise OntologyError(f"[{name}] 事件类型名应为 <域>.<动作>：{key}")
            if key in self.events:
                raise OntologyError(f"[{name}] 事件类型 {key} 已存在")
            self.events[key] = ev
        self.layers.append(name)

    def validate(self) -> None:
        """合并完成后做一次整体校验：引用目标存在、事件字段存在、create 覆盖必填。"""
        for ok, obj in self.objects.items():
            for pk, p in obj.properties.items():
                if p.type == "ref" and p.target not in self.objects:
                    raise OntologyError(f"{ok}.{pk} 引用了不存在的对象类型 {p.target}")
        for ek, ev in self.events.items():
            obj = self.objects.get(ev.object)
            if obj is None:
                raise OntologyError(f"事件 {ek} 作用的对象类型 {ev.object} 不存在")
            for f in ev.fields:
                if f not in obj.properties:
                    raise OntologyError(f"事件 {ek} 的字段 {f} 不是 {ev.object} 的属性")
            if ev.op == "create":
                missing = [k for k, p in obj.properties.items() if p.required and k not in ev.fields]
                if missing:
                    raise OntologyError(f"创建事件 {ek} 没有覆盖必填属性：{missing}")

    # ---------- 运行时校验 ----------
    def check_payload(self, event_type: str, payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        """校验并规范化 payload，返回只含合法字段的新 dict。引用是否存在由账本检查。"""
        ev = self.events.get(event_type)
        if ev is None:
            raise OntologyError(f"未知事件类型：{event_type}")
        obj = self.objects[ev.object]
        unknown = set(payload) - set(ev.fields)
        if unknown:
            raise OntologyError(f"{event_type} 不接受字段：{sorted(unknown)}")
        out: dict[str, Any] = {}
        for k, v in payload.items():
            out[k] = _coerce(f"{ev.object}.{k}", obj.properties[k], v)
        if creating:
            missing = [k for k, p in obj.properties.items() if p.required and out.get(k) in (None, "", [])]
            if missing:
                raise OntologyError(f"缺少必填字段：{missing}")
        return out

    def ref_props(self, object_type: str) -> dict[str, PropertyDef]:
        return {k: p for k, p in self.objects[object_type].properties.items() if p.type == "ref"}

    def describe(self) -> dict[str, Any]:
        return {
            "layers": self.layers,
            "objects": {k: v.model_dump(exclude_defaults=True) for k, v in self.objects.items()},
            "events": {k: v.model_dump(exclude_defaults=True) for k, v in self.events.items()},
        }


def _coerce(path: str, p: PropertyDef, v: Any) -> Any:
    if v is None:
        if p.required:
            raise OntologyError(f"{path} 不能为空")
        return None
    err = OntologyError(f"{path} 应为 {p.type}，收到 {v!r}")
    match p.type:
        case "string" | "text":
            if not isinstance(v, str):
                raise err
            return v.strip()
        case "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                raise err
            return v
        case "number":
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise err
            return float(v)
        case "bool":
            if not isinstance(v, bool):
                raise err
            return v
        case "date":
            try:
                return date.fromisoformat(v).isoformat()
            except (TypeError, ValueError):
                raise err from None
        case "datetime":
            try:
                dt = datetime.fromisoformat(v)
            except (TypeError, ValueError):
                raise err from None
            if dt.tzinfo is None:
                raise OntologyError(f"{path} 必须带时区") from None
            return dt.isoformat()
        case "enum":
            if v not in (p.values or []):
                raise OntologyError(f"{path} 取值应为 {p.values} 之一，收到 {v!r}")
            return v
        case "ref":
            ids = v if p.many else [v]
            if p.many and not isinstance(v, list):
                raise OntologyError(f"{path} 应为对象 id 列表")
            if not all(isinstance(i, str) and i for i in ids):
                raise OntologyError(f"{path} 的引用必须是非空字符串 id")
            return list(dict.fromkeys(ids)) if p.many else v
    raise err  # pragma: no cover
