"""第二个行业包（packs/ceramics 工业陶瓷）零代码跨行业复制与黄金评测端到端验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fos.config import Settings
from fos.eval.runner import run_golden_eval
from fos.ops.packs import list_packs, validate_pack
from fos.ops.provision import provision_tenant
from fos.services import Services

REPO = Path(__file__).resolve().parent.parent


def test_ceramics_pack_validation_and_catalog() -> None:
    """验证工业陶瓷行业包通过独立校验，且行业包目录同时包含 refractory 与 ceramics。"""
    rep = validate_pack(REPO / "packs", "ceramics")
    assert rep["ok"] is True, rep["errors"]
    assert rep["skills_count"] >= 2
    assert rep["eval_cases"] >= 10
    assert {"ehs", "eam", "market"}.issubset(set(rep["modules"]))

    packs = {p["pack"]: p for p in list_packs(REPO / "packs")}
    assert "refractory" in packs and "ceramics" in packs
    assert packs["refractory"]["ok"] is True and packs["ceramics"]["ok"] is True


@pytest.mark.asyncio
async def test_zero_code_provision_and_golden_eval_for_ceramics_factory(tmp_path: Path) -> None:
    """零代码开通一家工业陶瓷工厂，验证本体加载、陶瓷环保阈值（15mg/m³）与黄金评测 100% 通过。"""
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    rep = provision_tenant(
        packs_dir=REPO / "packs",
        tenants_dir=tenants_dir,
        tenant_id="jingde-ceramics",
        name="景德镇特种陶瓷厂",
        industry_pack="ceramics",
        im=["feishu"],
        template_tenant=None,
    )
    assert rep["ready"] is True
    assert rep["skills_count"] >= 2

    svc = Services.build(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'cer.db'}",
            tenants_dir=tenants_dir,
            packs_dir=REPO / "packs",
        )
    )
    try:
        onto = svc.ontologies["jingde-ceramics"]
        assert "glaze_recipe" in onto.objects
        assert "flexural_strength" in onto.objects["inspection"].properties
        assert "ceramic_process_advisor" in svc.skills["jingde-ceramics"]

        ev = run_golden_eval(svc, "jingde-ceramics")
        assert ev["all_passed"] is True, ev["badcases"]
        assert ev["pass_rate_pct"] == 100.0 and ev["total"] == 10
    finally:
        await svc.aclose()
