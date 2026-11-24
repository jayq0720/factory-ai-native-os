"""行业包资产盘点与独立完整性校验引擎：无需真实租户即可验证本体、Skill、工具与黄金评测集。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from fos.eval.runner import load_golden_suite
from fos.ontology.loader import load_ontology
from fos.skills.loader import load_skills
from fos.tenancy.config import RoleDef, TenantConfig
from fos.tools.registry import REGISTRY

KNOWN_MODULE_FILES = (
    "orders",
    "planning",
    "supply",
    "monitoring",
    "tender",
    "quality",
    "finance",
    "eam",
    "ehs",
    "hr",
    "rnd",
    "construction",
    "market",
    "reports",
    "trace",
    "import",
)


def _dummy_tenant(pack_name: str) -> TenantConfig:
    return TenantConfig(
        id="pack-check",
        name=f"校验占位({pack_name})",
        industry_pack=pack_name,
        roles={"admin": RoleDef(name="管理员", permissions=["*.*:*"])},
    )


def validate_pack(packs_dir: Path, pack_name: str) -> dict[str, Any]:
    """独立校验单个行业包的 YAML 语法、本体合并、Skill/Tool 绑定与黄金评测集 Schema。"""
    import fos.services  # noqa: F401  确保全部内置业务工具已注册到 REGISTRY

    pack_dir = packs_dir / pack_name
    if not pack_dir.is_dir():
        return {"pack": pack_name, "ok": False, "errors": [f"行业包目录不存在：{pack_dir}"]}

    errors: list[str] = []
    modules = [m for m in KNOWN_MODULE_FILES if (pack_dir / f"{m}.yaml").exists()]

    for yf in sorted(pack_dir.glob("*.yaml")):
        try:
            yaml.safe_load(yf.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{yf.name} YAML 解析失败：{e}")

    tenant = _dummy_tenant(pack_name)
    onto = None
    objects_count = 0
    events_count = 0
    ontology_layers: list[str] = []
    try:
        onto = load_ontology(tenant, packs_dir=packs_dir, tenants_dir=packs_dir / "_no_tenant")
        objects_count = len(onto.objects)
        events_count = len(onto.events)
        ontology_layers = list(onto.layers)
    except Exception as e:
        errors.append(f"本体加载失败：{e}")

    skills_count = 0
    if onto is not None:
        try:
            skills = load_skills(tenant, packs_dir=packs_dir, ontology=onto, tools=set(REGISTRY))
            skills_count = len(skills)
        except Exception as e:
            errors.append(f"Skill 加载失败：{e}")

    eval_cases = 0
    eval_path = pack_dir / "eval" / "golden_suite.yaml"
    if eval_path.exists():
        try:
            suite = load_golden_suite(eval_path)
            eval_cases = (
                len(suite.fast_path_routing)
                + len(suite.skill_routing)
                + len(suite.deterministic_calculators)
                + len(suite.policy_redlines)
            )
        except Exception as e:
            errors.append(f"黄金评测集 Schema 校验失败：{e}")

    return {
        "pack": pack_name,
        "ok": len(errors) == 0,
        "modules": modules,
        "ontology_layers": ontology_layers,
        "objects_count": objects_count,
        "events_count": events_count,
        "skills_count": skills_count,
        "eval_cases": eval_cases,
        "errors": errors,
    }


def list_packs(packs_dir: Path) -> list[dict[str, Any]]:
    """扫描 packs_dir 下全部行业包并返回盘点与校验报告。"""
    if not packs_dir.is_dir():
        return []
    names = sorted(p.name for p in packs_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    return [validate_pack(packs_dir, n) for n in names]
