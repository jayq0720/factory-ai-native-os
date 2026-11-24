"""云端控制面一键诊断（fos doctor / GET /v1/ops/doctor）回归测试。"""

from __future__ import annotations

import asyncio
import json

from fos.cli import main as cli_main
from fos.config import Settings
from fos.ops.doctor import format_doctor_text, run_doctor


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_ops_doctor_api_and_permissions(client, keys) -> None:
    """验证 GET /v1/ops/doctor 权限隔离与全量六维诊断项。"""
    assert client.get("/v1/ops/doctor", headers=_h(keys, "sales")).status_code == 403

    r = client.get("/v1/ops/doctor", headers=_h(keys, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["summary"]["fail"] == 0

    by_name = {c["name"]: c for c in body["checks"]}
    required_checks = (
        "env_profile",
        "blob_dir",
        "packs",
        "tenants",
        "database",
        "budgets",
        "edge_fleet",
        "ledger_integrity",
    )
    for required in required_checks:
        assert required in by_name
    assert by_name["packs"]["status"] == "ok"
    assert "refractory" in by_name["packs"]["detail"]
    assert "ceramics" in by_name["packs"]["detail"]
    assert by_name["database"]["status"] == "ok"
    assert by_name["budgets"]["status"] == "ok"
    assert by_name["edge_fleet"]["status"] == "ok"
    assert by_name["ledger_integrity"]["status"] == "ok"


def test_doctor_prod_warnings_and_text_render(tmp_path) -> None:
    """验证 prod 环境下关闭限流/未配边缘签名密钥时产出 warn，且文本渲染完整。"""
    st = Settings(
        env="prod",
        rate_limit_per_min=0,
        edge_command_key="",
        blob_dir=tmp_path,
    )
    report = asyncio.run(run_doctor(st))
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["env_profile"]["status"] == "warn"
    assert "FOS_RATE_LIMIT_PER_MIN=0" in by_name["env_profile"]["detail"]
    assert "FOS_EDGE_COMMAND_KEY" in by_name["env_profile"]["detail"]
    assert by_name["blob_dir"]["status"] == "ok"

    txt = format_doctor_text(report)
    assert "FOS 云端控制面自检" in txt
    assert "[WARN]" in txt
    assert "汇总：" in txt


def test_doctor_cli_skip_db_json(capsys) -> None:
    """验证 CLI `fos doctor --skip-db --json` 离线自检返回 0 与合法 JSON。"""
    rc = cli_main(["doctor", "--skip-db", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["summary"]["fail"] == 0
    names = {c["name"] for c in payload["checks"]}
    assert {"env_profile", "blob_dir", "packs", "tenants"}.issubset(names)
    assert "database" not in names
