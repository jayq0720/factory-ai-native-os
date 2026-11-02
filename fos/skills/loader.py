"""按租户加载 Skill：行业包 packs/<pack>/skills/* + 职能包 packs/functions/skills/*（如有）。"""

from __future__ import annotations

from pathlib import Path

from fos.ontology import Ontology
from fos.skills.schema import SkillDef, SkillError, load_skill_dir
from fos.tenancy.config import TenantConfig


def load_skills(
    tenant: TenantConfig, *, packs_dir: Path, ontology: Ontology, tools: set[str]
) -> dict[str, SkillDef]:
    out: dict[str, SkillDef] = {}
    for pack in (tenant.industry_pack, "functions"):
        root = packs_dir / pack / "skills"
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if (p / "skill.yaml").exists()):
            s = load_skill_dir(d, source=pack)
            if s.id in out:
                raise SkillError(f"Skill {s.id} 重复定义（{out[s.id].source} 与 {pack}）")
            unknown_tools = set(s.tools) - tools
            if unknown_tools:
                raise SkillError(f"Skill {s.id} 用了未注册的工具：{sorted(unknown_tools)}")
            unknown_events = [e for e in s.proposes if e not in ontology.events]
            if unknown_events:
                raise SkillError(f"Skill {s.id} 提议的事件不在本体里：{unknown_events}")
            if s.proposes and "ledger.propose" not in s.tools:
                raise SkillError(f"Skill {s.id} 声明了 proposes，但没有 ledger.propose 工具")
            out[s.id] = s
    return out
