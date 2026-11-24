"""第三个行业包（packs/foundry 精密铸造与热处理）零代码跨行业复制与黄金评测端到端验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fos.config import Settings
from fos.eval.runner import run_golden_eval
from fos.ops.packs import list_packs, validate_pack
from fos.ops.provision import provision_tenant
from fos.services import Services

REPO = Path(__file__).resolve().parent.parent


def test_foundry_pack_validation_and_catalog() -> None:
    """验证精密铸造行业包通过独立校验，且行业包目录包含 refractory / ceramics / foundry 三包。"""
    rep = validate_pack(REPO / "packs", "foundry")
    assert rep["ok"] is True, rep["errors"]
    assert rep["skills_count"] >= 2
    assert rep["eval_cases"] >= 12
    assert {"ehs", "eam", "market"}.issubset(set(rep["modules"]))

    packs = {p["pack"]: p for p in list_packs(REPO / "packs")}
    for name in ("refractory", "ceramics", "foundry"):
        assert name in packs and packs[name]["ok"] is True


@pytest.mark.asyncio
async def test_zero_code_provision_and_golden_eval_for_foundry_factory(tmp_path: Path) -> None:
    """零代码开通精密铸造工厂，验证本体扩展、环保限值（粉尘 12 / SO2 35）与黄金评测 100% 通过。"""
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    rep = provision_tenant(
        packs_dir=REPO / "packs",
        tenants_dir=tenants_dir,
        tenant_id="wuxi-foundry",
        name="无锡精密铸造厂",
        industry_pack="foundry",
        im=["wecom"],
        template_tenant=None,
    )
    assert rep["ready"] is True
    assert rep["skills_count"] >= 2

    svc = Services.build(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'foundry.db'}",
            tenants_dir=tenants_dir,
            packs_dir=REPO / "packs",
        )
    )
    try:
        onto = svc.ontologies["wuxi-foundry"]
        assert {"furnace", "mold_pattern", "heat_lot"}.issubset(set(onto.objects))
        assert "tensile_strength_mpa" in onto.objects["inspection"].properties
        assert "nodularity_pct" in onto.objects["inspection"].properties
        assert "foundry_metallurgist" in svc.skills["wuxi-foundry"]

        ev = run_golden_eval(svc, "wuxi-foundry")
        assert ev["all_passed"] is True, ev["badcases"]
        assert ev["pass_rate_pct"] == 100.0 and ev["total"] >= 12
    finally:
        await svc.aclose()
