from pathlib import Path

import pytest
from pydantic import ValidationError

from fos.governance.permissions import grants
from fos.tenancy import TenantConfig, TenantRegistry

REPO = Path(__file__).resolve().parents[1]


def test_repo_tenants_load():
    reg = TenantRegistry.load(REPO / "tenants")
    t = reg.get("luneng")
    assert t is not None
    assert t.industry_pack == "refractory"
    assert set(t.im) == {"dingtalk", "wecom", "feishu"}
    assert t.permissions_for(["sales_lead"]) >= {"eng.quote:approve"}


def test_permissions_union_and_unknown_role():
    t = TenantRegistry.load(REPO / "tenants").get("luneng")
    assert t
    perms = t.permissions_for(["warehouse", "qc", "no_such_role"])
    assert grants(perms, "warehouse.stock:write")
    assert grants(perms, "quality.inspection:write")
    assert not grants(perms, "finance.voucher:read")


def test_rejects_bad_permission():
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(
            {
                "id": "x1",
                "name": "x",
                "industry_pack": "p",
                "roles": {"admin": {"name": "a", "permissions": ["bad"]}},
            }
        )


def test_requires_admin_role():
    with pytest.raises(ValidationError):
        TenantConfig.model_validate({"id": "x1", "name": "x", "industry_pack": "p", "roles": {}})


def test_id_must_match_dir(tmp_path: Path):
    (tmp_path / "abc").mkdir()
    (tmp_path / "abc" / "tenant.yaml").write_text(
        "id: xyz\nname: n\nindustry_pack: p\nroles: {admin: {name: a}}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="不一致"):
        TenantRegistry.load(tmp_path)
