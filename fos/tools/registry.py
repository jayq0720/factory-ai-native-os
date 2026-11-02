"""Agent 可调用的工具（方案 2.6 Tool / MCP 端点）。

每个工具声明参数 Schema 和所需权限。调用时权限要同时满足两条：
1. Skill 声明过这个权限（Skill 能读到的上限）；
2. 提问的人自己有这个权限（Agent 不能替人越权）。
工具只读；唯一“写”的工具 ledger.propose 也只是生成待确认的提议，不写账本。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.permissions import grants
from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.skills.schema import SkillDef


class ToolError(Exception):
    """工具调用失败：原因会回给模型，让它换个做法或如实告诉用户。"""


@dataclass
class Proposal:
    """待人确认的记账提议，Run 结束后变成卡片。"""

    type: str
    object_id: str | None
    payload: dict[str, Any]
    title: str
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolContext:
    session: AsyncSession
    user: Principal  # 提问的人
    skill: SkillDef
    ontology: Ontology
    proposals: list[Proposal] = field(default_factory=list)

    @property
    def tenant_id(self) -> str:
        return self.user.tenant_id

    def allowed(self, permission: str) -> bool:
        return grants(frozenset(self.skill.permissions), permission) and self.user.can(permission)

    def require(self, permission: str) -> None:
        if not self.allowed(permission):
            raise ToolError(f"没有权限 {permission}（提问人或当前 Skill 不具备）")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]

    def spec(self) -> dict[str, Any]:
        """给模型看的工具说明（名称、用途、参数 JSON Schema）。"""
        return {"name": self.name, "description": self.description, "args": self.args.model_json_schema()}

    async def call(self, ctx: ToolContext, raw_args: dict[str, Any]) -> dict[str, Any]:
        try:
            args = self.args.model_validate(raw_args or {})
        except ValidationError as e:
            raise ToolError(f"参数不对：{e.errors(include_url=False)[:3]}") from e
        return await self.run(ctx, args)


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, args: type[BaseModel]):
    def deco(fn):
        REGISTRY[name] = Tool(name=name, description=description, args=args, run=fn)
        return fn

    return deco
