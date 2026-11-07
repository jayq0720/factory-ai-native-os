"""Policy：审批规则与红线（方案 5.1 原则 5、5.2 “Policy 与审批收件箱”）。

规则是声明式的：哪些事件、满足什么条件时，要哪几级人审批（effect=approve），或者直接拒绝（effect=deny，红线）。
条件判断全部是确定性代码（数字比较不交给模型）。规则作用在 Ledger.append() 上，
所以不论事件来自 API、卡片确认还是以后的任何入口，都绕不过去。

两层合并：行业包 packs/<pack>/policy.yaml（可选）→ 租户 tenants/<id>/policy.yaml（可选）。
租户可以用同名规则替换行业包的规则，或用 disable 关掉某条行业包规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from fos.governance.permissions import is_valid_permission
from fos.governance.principal import Principal

_RULE_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
Op = Literal["gt", "gte", "lt", "lte", "eq", "ne", "in", "not_in", "exists"]
_NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
OP_TEXT = {
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "eq": "=",
    "ne": "≠",
    "in": "属于",
    "not_in": "不属于",
    "exists": "已填写",
}


class PolicyError(ValueError):
    pass


class Condition(BaseModel):
    """字段取值：事件 payload 里有就用 payload 的（变更后的值），否则用对象当前状态。"""

    field: str
    op: Op
    value: Any = None

    @model_validator(mode="after")
    def _check(self) -> Condition:
        if self.op in _NUMERIC_OPS and (
            isinstance(self.value, bool) or not isinstance(self.value, int | float)
        ):
            raise ValueError(f"{self.op} 的 value 必须是数字")
        if self.op in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError(f"{self.op} 的 value 必须是列表")
        if self.op == "exists" and self.value is None:
            self.value = True
        return self

    def matches(self, payload: dict[str, Any], state: dict[str, Any] | None) -> bool:
        v = payload[self.field] if self.field in payload else (state or {}).get(self.field)
        if self.op == "exists":
            return (v is not None and v != "") == bool(self.value)
        if v is None:
            return False
        if self.op in _NUMERIC_OPS:
            if isinstance(v, bool) or not isinstance(v, int | float):
                return False
            return {
                "gt": v > self.value,
                "gte": v >= self.value,
                "lt": v < self.value,
                "lte": v <= self.value,
            }[self.op]
        if self.op == "eq":
            return v == self.value
        if self.op == "ne":
            return v != self.value
        if self.op == "in":
            return v in self.value
        return v not in self.value  # not_in

    def describe(self) -> str:
        return f"{self.field} {OP_TEXT[self.op]} {self.value}" if self.op != "exists" else self.field


class Step(BaseModel):
    """一级审批。满足 role / permission / login 任意一个的人都可以审批这一级（任一人通过即可）。"""

    name: str = Field(min_length=1, max_length=32)
    role: str | None = None
    permission: str | None = None
    login: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Step:
        if not (self.role or self.permission or self.login):
            raise ValueError("审批步骤至少要指定 role、permission 或 login 之一")
        if self.permission and (not is_valid_permission(self.permission) or "*" in self.permission):
            raise ValueError(f"审批权限必须是不带通配的权限串：{self.permission}")
        return self

    def eligible(self, principal: Principal, login: str | None) -> bool:
        if self.role and self.role in principal.roles:
            return True
        if self.permission and principal.can(self.permission):
            return True
        return bool(self.login and login and self.login == login)


class Rule(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    events: list[str] = Field(min_length=1)
    when: list[Condition] = Field(default_factory=list)  # 全部满足才命中；空表示总是命中
    effect: Literal["approve", "deny"] = "approve"
    steps: list[Step] = Field(default_factory=list)
    reason: str = Field(default="", max_length=200)  # deny 时给人看的理由
    allow_self_approval: bool = False  # 默认申请人不能审批自己的申请
    expires_hours: int = Field(default=168, ge=1, le=24 * 60)

    @model_validator(mode="after")
    def _check(self) -> Rule:
        if self.effect == "approve" and not self.steps:
            raise ValueError("effect=approve 的规则至少要有一级审批")
        if self.effect == "deny":
            if self.steps:
                raise ValueError("effect=deny 的规则不能有审批步骤")
            if not self.reason:
                raise ValueError("effect=deny 的规则必须写 reason")
        return self


class PolicyLayer(BaseModel):
    rules: dict[str, Rule] = Field(default_factory=dict)
    disable: list[str] = Field(default_factory=list)  # 关掉下层的某些规则

    @field_validator("rules")
    @classmethod
    def _ids(cls, v: dict[str, Rule]) -> dict[str, Rule]:
        for k in v:
            if not _RULE_ID.match(k):
                raise ValueError(f"规则 id 不合法：{k}")
        return v


@dataclass
class Decision:
    effect: Literal["allow", "approve", "deny"]
    rules: list[tuple[str, Rule]] = field(default_factory=list)  # 命中的规则（按定义顺序）
    reason: str = ""

    @property
    def rule_ids(self) -> list[str]:
        return [rid for rid, _ in self.rules]

    def steps(self) -> list[dict[str, Any]]:
        """审批步骤快照：多条规则命中时按规则顺序串联，完全相同的步骤只保留一个。"""
        out: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for rid, r in self.rules:
            for s in r.steps:
                key = (s.role, s.permission, s.login)
                if key in seen:
                    continue
                seen.add(key)
                out.append({**s.model_dump(), "rule": rid})
        return out

    def allow_self(self) -> bool:
        return all(r.allow_self_approval for _, r in self.rules)

    def expires_hours(self) -> int:
        return min((r.expires_hours for _, r in self.rules), default=168)


class Policy:
    def __init__(self) -> None:
        self.rules: dict[str, Rule] = {}
        self.layers: list[str] = []

    def add_layer(self, name: str, layer: PolicyLayer) -> None:
        for rid in layer.disable:
            if rid not in self.rules:
                raise PolicyError(f"[{name}] disable 的规则 {rid} 不存在")
            del self.rules[rid]
        for rid, rule in layer.rules.items():
            self.rules[rid] = rule  # 同名替换下层规则
        self.layers.append(name)

    def validate(self, ontology, roles: set[str]) -> None:
        """规则里的事件、字段、角色都必须存在；比较运算和字段类型要对得上。"""
        for rid, r in self.rules.items():
            for ev in r.events:
                evdef = ontology.events.get(ev)
                if evdef is None:
                    raise PolicyError(f"规则 {rid}：事件类型 {ev} 不存在")
                props = ontology.objects[evdef.object].properties
                for c in r.when:
                    p = props.get(c.field)
                    if p is None:
                        raise PolicyError(f"规则 {rid}：{c.field} 不是 {evdef.object} 的属性")
                    if c.op in _NUMERIC_OPS and p.type not in ("number", "integer"):
                        raise PolicyError(f"规则 {rid}：{c.field} 不是数字，不能用 {c.op}")
                    if p.type == "enum" and c.op in ("eq", "ne", "in", "not_in"):
                        vals = c.value if isinstance(c.value, list) else [c.value]
                        bad = [v for v in vals if v not in (p.values or [])]
                        if bad:
                            raise PolicyError(f"规则 {rid}：{c.field} 没有取值 {bad}")
            for s in r.steps:
                if s.role and s.role not in roles:
                    raise PolicyError(f"规则 {rid}：角色 {s.role} 不存在")

    def evaluate(self, event_type: str, payload: dict[str, Any], state: dict[str, Any] | None) -> Decision:
        hits = [
            (rid, r)
            for rid, r in self.rules.items()
            if event_type in r.events and all(c.matches(payload, state) for c in r.when)
        ]
        deny = [(rid, r) for rid, r in hits if r.effect == "deny"]
        if deny:
            return Decision("deny", deny, reason="；".join(r.reason for _, r in deny))
        if hits:
            return Decision("approve", hits)
        return Decision("allow")

    def describe(self) -> dict[str, Any]:
        return {
            "layers": self.layers,
            "rules": {
                rid: {
                    "name": r.name,
                    "events": r.events,
                    "when": [c.describe() for c in r.when],
                    "effect": r.effect,
                    "steps": [s.model_dump(exclude_none=True) for s in r.steps],
                    "reason": r.reason,
                    "allow_self_approval": r.allow_self_approval,
                    "expires_hours": r.expires_hours,
                }
                for rid, r in self.rules.items()
            },
        }
