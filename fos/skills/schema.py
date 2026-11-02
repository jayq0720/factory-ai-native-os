"""Skill 包（方案 5.1 原则 8、5.2）：一个岗位做一类事的方法。

每个 Skill 是一个目录：
  skill.yaml  机器读的声明：角色、何时用（关键词与说明）、可用工具、所需读权限、可以提议的事件、模型、步数上限
  SKILL.md    给模型读的做事方法（自然语言，可版本化、可评测）

Skill 本身没有写权限：要改业务数据只能“提议”一个事件，生成卡片由有权限的人确认（L2）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from fos.governance.permissions import is_valid_permission


class SkillError(ValueError):
    pass


class SkillDef(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    name: str
    role: str  # 岗位 Agent 名称，如“车间助理”
    version: int = Field(ge=1)
    description: str  # 给 Router 看：这个 Skill 管什么
    keywords: list[str] = Field(default_factory=list)  # 模拟模式下的 Router 规则；真实模式作为提示
    model: Literal["fos-chat", "fos-reason", "fos-fast"] = "fos-chat"
    autonomy: Literal["L1", "L2"] = "L1"
    tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)  # Skill 最多能读到的范围（还要与提问人的权限取交集）
    proposes: list[str] = Field(default_factory=list)  # 可以提议的事件类型
    max_steps: int = Field(default=6, ge=1, le=20)
    instructions: str = ""  # SKILL.md 内容
    source: str = ""  # 来自哪个包

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not is_valid_permission(p)]
        if bad:
            raise ValueError(f"权限格式不对：{bad}")
        return v


def load_skill_dir(d: Path, source: str) -> SkillDef:
    try:
        raw = yaml.safe_load((d / "skill.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise SkillError(f"{d}: 读取 skill.yaml 失败：{e}") from e
    md = d / "SKILL.md"
    raw["instructions"] = md.read_text(encoding="utf-8") if md.exists() else ""
    raw["source"] = source
    try:
        skill = SkillDef.model_validate(raw)
    except ValueError as e:
        raise SkillError(f"{d}: {e}") from e
    if skill.id != d.name:
        raise SkillError(f"{d}: id {skill.id} 与目录名不一致")
    if skill.proposes and skill.autonomy != "L2":
        raise SkillError(f"{d}: 会提议事件的 Skill 必须是 L2（人确认）")
    return skill
