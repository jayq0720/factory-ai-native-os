"""黄金数据集评测运行器（W29）：零模型成本回归 Fast Path、Skill 路由、确定性算子与审批红线。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

from fos.agents.router import keyword_decide
from fos.construction.config import ConstructionConfig
from fos.construction.metrics import evaluate_dryout
from fos.ehs.compliance import evaluate_env_reading
from fos.ehs.config import EhsConfig
from fos.hr.config import HrConfig
from fos.hr.payroll import compute_payroll
from fos.market.analytics import compute_funnel
from fos.rnd.formulation import evaluate_formula
from fos.triggers.intake import (
    _construction_word,
    _eam_word,
    _ehs_word,
    _finance_word,
    _hr_word,
    _market_word,
    _quality_word,
    _rnd_word,
)

if TYPE_CHECKING:
    from fos.services import Services


class FastPathCase(BaseModel):
    id: str
    text: str
    expect: str


class SkillCase(BaseModel):
    id: str
    text: str
    expect_skill: str | None = None


class CalcCase(BaseModel):
    id: str
    kind: str
    input: dict[str, Any]
    expect: dict[str, Any]


class PolicyCase(BaseModel):
    id: str
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] | None = None
    expect_effect: str


class GoldenSuite(BaseModel):
    fast_path_routing: list[FastPathCase] = Field(default_factory=list)
    skill_routing: list[SkillCase] = Field(default_factory=list)
    deterministic_calculators: list[CalcCase] = Field(default_factory=list)
    policy_redlines: list[PolicyCase] = Field(default_factory=list)


def load_golden_suite(path: Path) -> GoldenSuite:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return GoldenSuite.model_validate(raw)


def _classify_fast_path(text: str) -> str:
    t = text.strip()
    if qm := _quality_word(t):
        return f"quality_{qm[0]}"
    if fm := _finance_word(t):
        return f"finance_{fm[0]}"
    if em := _eam_word(t):
        return f"eam_{em}"
    if hm := _ehs_word(t):
        return f"ehs_{hm}"
    if hrm := _hr_word(t):
        return f"hr_{hrm}"
    if rm := _rnd_word(t):
        return f"rnd_{rm}"
    if cm := _construction_word(t):
        return f"construction_{cm}"
    if mm := _market_word(t):
        return f"market_{mm}"
    return "router"


def _eval_calc(svc: Services, tenant_id: str, case: CalcCase) -> dict[str, Any]:
    inp = case.input
    if case.kind == "ehs_env":
        cfg = svc.ehs.get(tenant_id) or EhsConfig()
        return evaluate_env_reading(cfg, str(inp["metric"]), float(inp["value"]))
    if case.kind == "hr_payroll":
        cfg = svc.hr.get(tenant_id) or HrConfig()
        res = compute_payroll(
            cfg,
            "2026-11",
            employees=[{"id": "E1", "base_salary": float(inp["base_salary"])}],
            rosters=[
                {
                    "employee": "E1",
                    "work_date": "2026-11-01",
                    "attendance": "present",
                    "hours": 8.0,
                    "overtime_hours": float(inp.get("overtime_hours", 0.0)),
                    "piece_qty": float(inp.get("piece_qty", 0.0)),
                    "piece_rate": float(inp.get("piece_rate", cfg.default_piece_rate)),
                }
            ],
        )
        return res["items"][0]
    if case.kind == "rnd_formula":
        return evaluate_formula(list(inp["ingredients"]))
    if case.kind == "construction_dryout":
        cfg = svc.construction.get(tenant_id) or ConstructionConfig()
        return evaluate_dryout(
            cfg,
            target_temp_c=float(inp["target_temp_c"]),
            actual_temp_c=float(inp["actual_temp_c"]),
            ramp_rate_c_per_h=float(inp["ramp_rate_c_per_h"]),
        )
    if case.kind == "market_funnel":
        return compute_funnel(list(inp["leads"]))
    raise ValueError(f"未知评测算子类型：{case.kind}")


def run_golden_eval(svc: Services, tenant_id: str, suite_path: Path | None = None) -> dict[str, Any]:
    """执行黄金数据集全量确定性回归评测，返回分类得分与 Badcase 明细。"""
    tenant = svc.registry.get(tenant_id)
    if tenant is None:
        raise ValueError(f"租户 {tenant_id} 不存在")
    path = suite_path or (svc.settings.packs_dir / tenant.industry_pack / "eval" / "golden_suite.yaml")
    if not path.exists():
        raise ValueError(f"未找到黄金评测集：{path}")
    suite = load_golden_suite(path)

    badcases: list[dict[str, Any]] = []
    categories: dict[str, dict[str, int]] = {}

    def _rec(cat: str, cid: str, ok: bool, expected: Any, actual: Any) -> None:
        c = categories.setdefault(cat, {"total": 0, "passed": 0, "failed": 0})
        c["total"] += 1
        if ok:
            c["passed"] += 1
        else:
            c["failed"] += 1
            badcases.append({"category": cat, "id": cid, "expected": expected, "actual": actual})

    for fc in suite.fast_path_routing:
        actual = _classify_fast_path(fc.text)
        _rec("fast_path_routing", fc.id, actual == fc.expect, fc.expect, actual)

    skills = svc.skills.get(tenant_id, {})
    for sc in suite.skill_routing:
        dec = keyword_decide(sc.text, skills)
        actual = dec.skill if dec.action == "dispatch" else None
        _rec("skill_routing", sc.id, actual == sc.expect_skill, sc.expect_skill, actual)

    for cc in suite.deterministic_calculators:
        try:
            got = _eval_calc(svc, tenant_id, cc)
            ok = all(got.get(k) == v for k, v in cc.expect.items())
            _rec("deterministic_calculators", cc.id, ok, cc.expect, {k: got.get(k) for k in cc.expect})
        except Exception as e:
            _rec("deterministic_calculators", cc.id, False, cc.expect, f"ERROR: {e}")

    pol = svc.policies.get(tenant_id)
    for pc in suite.policy_redlines:
        actual = pol.evaluate(pc.event, pc.payload, pc.state).effect if pol is not None else "allow"
        _rec("policy_redlines", pc.id, actual == pc.expect_effect, pc.expect_effect, actual)

    total = sum(c["total"] for c in categories.values())
    passed = sum(c["passed"] for c in categories.values())
    failed = total - passed
    rate = round(passed / total * 100.0, 2) if total > 0 else 100.0
    return {
        "tenant_id": tenant_id,
        "suite": str(path.name),
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate_pct": rate,
        "all_passed": failed == 0,
        "categories": categories,
        "badcases": badcases,
    }
