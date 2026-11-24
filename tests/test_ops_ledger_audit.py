"""不可变事件账本实时一致性审计（GET /v1/ops/ledger-check & fos verify-ledger）回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from fos.cli import main
from fos.config import get_settings
from fos.db.models import TenantCounter
from fos.db.session import make_engine, make_sessionmaker


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_ledger_check_api_and_evidence_coverage(client, keys) -> None:
    """验证 /v1/ops/ledger-check 权限隔离、序号连续性、计数器对齐、证据链 100% 与投影同步。"""
    assert client.get("/v1/ops/ledger-check", headers=_h(keys, "sales")).status_code == 403

    close_dt = (date.today() + timedelta(days=15)).isoformat()
    r = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "账本审计验证客户",
            "industry": "steel",
            "product_interest": "高铝砖",
            "est_amount": 95000.0,
            "owner_name": "李四",
            "expected_close_date": close_dt,
            "data_mode": "real",
        },
        headers=_h(keys, "sales"),
    )
    assert r.status_code == 201

    rc = client.get("/v1/ops/ledger-check", headers=_h(keys, "admin"))
    assert rc.status_code == 200
    body = rc.json()
    assert body["ok"] is True
    assert body["events_total"] >= 1
    assert body["seq_monotonic_ok"] is True
    assert body["counter_aligned"] is True
    assert body["evidence_coverage_pct"] == 100.0
    assert body["projection_ok"] is True
    assert body["projected_objects_count"] >= 1


def test_verify_ledger_cli_detects_counter_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证 CLI `fos verify-ledger` 正常返回 0，检测到 TenantCounter 漂移时返回非零退出码。"""
    db_file = tmp_path / "audit.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("FOS_DATABASE_URL", db_url)
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        capsys.readouterr()
        assert main(["verify-ledger", "luneng", "--json"]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["ok"] is True and rep["counter_aligned"] is True

        assert main(["verify-ledger", "--all", "--json"]) == 0
        rep_all = json.loads(capsys.readouterr().out)
        assert rep_all["all_ok"] is True and rep_all["tenants_count"] >= 1

        # 模拟 TenantCounter.last_seq 漂移，验证审计引擎能实时捕获并返非零
        async def _drift() -> None:
            eng = make_engine(db_url)
            try:
                sm = make_sessionmaker(eng)
                async with sm() as s, s.begin():
                    s.add(TenantCounter(tenant_id="luneng", last_seq=99))
            finally:
                await eng.dispose()

        asyncio.run(_drift())

        assert main(["verify-ledger", "luneng"]) == 1
        txt = capsys.readouterr().out
        assert "计数器漂移" in txt
        assert main(["verify-ledger", "--all"]) == 1
        assert main(["verify-ledger"]) == 1
        assert main(["verify-ledger", "nonexistent-tenant"]) == 1
    finally:
        get_settings.cache_clear()
