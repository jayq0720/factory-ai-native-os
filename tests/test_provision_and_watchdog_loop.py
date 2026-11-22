"""W31 新工厂一键复制脚手架、Watchdog 守护循环与部署编排回归测试。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from fos.cli import main
from fos.config import Settings, get_settings
from fos.ops.provision import provision_tenant
from fos.ops.watchdog import watchdog_loop
from fos.services import Services

REPO = Path(__file__).resolve().parent.parent


def test_provision_tenant_and_guards(tmp_path: Path) -> None:
    """验证零代码一键复制第二家耐火材料工厂及非法参数/防覆盖守卫。"""
    tenants_dir = tmp_path / "tenants"
    shutil.copytree(REPO / "tenants" / "luneng", tenants_dir / "luneng")

    rep = provision_tenant(
        packs_dir=REPO / "packs",
        tenants_dir=tenants_dir,
        tenant_id="huadong-ref",
        name="华东耐材二厂",
        industry_pack="refractory",
        im=["dingtalk", "feishu"],
        template_tenant="luneng",
    )
    assert rep["ready"] is True
    assert rep["tenant_id"] == "huadong-ref"
    assert rep["skills_count"] >= 12
    assert rep["policy_rules_count"] >= 10
    assert (tenants_dir / "huadong-ref" / "tenant.yaml").exists()
    assert (tenants_dir / "huadong-ref" / "policy.yaml").exists()

    with pytest.raises(ValueError, match="已存在"):
        provision_tenant(
            packs_dir=REPO / "packs",
            tenants_dir=tenants_dir,
            tenant_id="huadong-ref",
            name="重复工厂",
        )
    with pytest.raises(ValueError, match="非法"):
        provision_tenant(
            packs_dir=REPO / "packs",
            tenants_dir=tenants_dir,
            tenant_id="bad_underscore",
            name="非法命名",
        )
    with pytest.raises(ValueError, match="不支持的 IM"):
        provision_tenant(
            packs_dir=REPO / "packs",
            tenants_dir=tenants_dir,
            tenant_id="valid-id",
            name="测试厂",
            im=["slack"],
        )


def test_cli_clone_tenant_and_watchdog_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端测试 fos clone-tenant 与 fos watchdog --all CLI 命令。"""
    tenants_dir = tmp_path / "tenants"
    shutil.copytree(REPO / "tenants" / "luneng", tenants_dir / "luneng")
    db_file = tmp_path / "ops_w31.db"
    monkeypatch.setenv("FOS_TENANTS_DIR", str(tenants_dir))
    monkeypatch.setenv("FOS_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        rc = main(["clone-tenant", "huabei-ref", "华北耐材三厂", "--im", "feishu,wecom"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "huabei-ref" in out

        assert main(["watchdog", "--all"]) == 0
        out_wd = capsys.readouterr().out
        assert "luneng Watchdog" in out_wd
        assert "huabei-ref Watchdog" in out_wd
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_watchdog_loop_graceful_stop(tmp_path: Path) -> None:
    """验证 watchdog_loop 守护循环能幂等执行并在 stop_event 触发后优雅退出。"""
    db_file = tmp_path / "wd_loop.db"
    s = Settings(database_url=f"sqlite+aiosqlite:///{db_file}")
    from fos.quickstart import init_db

    await init_db(s)
    svc = Services.build(s)
    try:
        stop = asyncio.Event()
        stop.set()
        cycles = await watchdog_loop(svc, ["luneng"], interval_seconds=0.05, stop_event=stop)
        assert cycles == 1
    finally:
        await svc.aclose()


def test_docker_compose_has_watchdog_and_im_envs() -> None:
    """验证 deploy/docker-compose.yml 语法合法且包含 watchdog 服务与三端 IM 配置。"""
    data = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    services = data["services"]
    assert "watchdog" in services
    assert "ops" in services["watchdog"]["profiles"]
    env = data["x-fos-env"]
    assert "FOS_FEISHU_LUNENG_APP_ID" in env
    assert "FOS_WECOM_LUNENG_CORP_ID" in env
