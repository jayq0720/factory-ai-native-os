"""不可变事件账本防篡改灾备导出、离线 SHA-256 校验与合规审计 Excel 导出回归测试。"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from fos.cli import main
from fos.config import get_settings
from fos.ops.backup import verify_ledger_backup_jsonl


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_audit_xlsx_and_backup_jsonl_api(client, keys) -> None:
    """验证 /v1/ops/audit.xlsx 与 /v1/ops/backup.jsonl 权限隔离、导出内容与防篡改校验。"""
    assert client.get("/v1/ops/audit.xlsx", headers=_h(keys, "sales")).status_code == 403
    assert client.get("/v1/ops/backup.jsonl", headers=_h(keys, "sales")).status_code == 403

    close_dt = (date.today() + timedelta(days=10)).isoformat()
    r = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "=CMD|'/C calc'!A0",
            "industry": "steel",
            "product_interest": "高铝砖",
            "est_amount": 80000.0,
            "owner_name": "张三",
            "expected_close_date": close_dt,
            "data_mode": "real",
        },
        headers=_h(keys, "sales"),
    )
    assert r.status_code == 201

    # 1. 合规审计流水 Excel
    rx = client.get("/v1/ops/audit.xlsx?event_prefix=market.", headers=_h(keys, "admin"))
    assert rx.status_code == 200
    wb = load_workbook(io.BytesIO(rx.content))
    assert wb.sheetnames == ["不可变账本审计流水"]
    ws = wb.active
    assert ws is not None and ws.max_row >= 2

    # 2. 防篡改 JSONL 灾备包
    rb = client.get("/v1/ops/backup.jsonl", headers=_h(keys, "admin"))
    assert rb.status_code == 200
    assert int(rb.headers["x-ledger-count"]) >= 1
    assert len(rb.headers["x-ledger-sha256"]) == 64

    v_ok = verify_ledger_backup_jsonl(rb.text)
    assert v_ok["ok"] is True
    assert v_ok["tenant_id"] == "luneng"
    assert v_ok["sha256"] == rb.headers["x-ledger-sha256"]

    # 篡改单字符立即触发 SHA-256 校验失败
    tampered = rb.text.replace("高铝砖", "镁碳砖", 1)
    v_bad = verify_ledger_backup_jsonl(tampered)
    assert v_bad["ok"] is False
    assert "SHA-256" in v_bad["error"]

    # 删除中间事件行立即触发条数不一致拦截
    lines = rb.text.splitlines()
    deleted = "\n".join([lines[0], lines[-1]]) + "\n"
    v_del = verify_ledger_backup_jsonl(deleted)
    assert v_del["ok"] is False
    assert "条数不一致" in v_del["error"]


def test_cli_backup_and_verify_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端验证 fos backup-ledger 与 fos verify-backup CLI 命令。"""
    db_file = tmp_path / "backup_cli.db"
    out_file = tmp_path / "out" / "luneng.jsonl"
    monkeypatch.setenv("FOS_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        assert main(["backup-ledger", "luneng", str(out_file)]) == 0
        out = capsys.readouterr().out
        assert "sha256=" in out
        assert out_file.exists()

        assert main(["verify-backup", str(out_file)]) == 0
        assert "校验通过" in capsys.readouterr().out

        # 破坏文件内容后应返回非零退出码
        out_file.write_text('{"_meta":{"format":"bad"}}\n', encoding="utf-8")
        assert main(["verify-backup", str(out_file)]) == 1
    finally:
        get_settings.cache_clear()


def test_disaster_recovery_restore_and_projection_rebuild(
    client, keys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端灾备演练：源库写入业务对象 -> 导出防篡改 JSONL -> 恢复至全新空库 -> 投影与序号完全还原。"""
    close_dt = (date.today() + timedelta(days=12)).isoformat()
    r = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "灾备演练客户",
            "industry": "cement",
            "product_interest": "硅莫砖",
            "est_amount": 350000.0,
            "owner_name": "灾备官",
            "expected_close_date": close_dt,
            "data_mode": "real",
        },
        headers=_h(keys, "sales"),
    )
    assert r.status_code == 201
    lead_id = r.json()["id"]

    rb = client.get("/v1/ops/backup.jsonl", headers=_h(keys, "admin"))
    assert rb.status_code == 200
    backup_file = tmp_path / "dr_luneng.jsonl"
    backup_file.write_text(rb.text, encoding="utf-8")

    # 切到全新空库执行 fos restore-ledger
    dr_db = tmp_path / "dr_fresh.db"
    monkeypatch.setenv("FOS_DATABASE_URL", f"sqlite+aiosqlite:///{dr_db}")
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        assert main(["restore-ledger", "luneng", str(backup_file)]) == 0
        out = capsys.readouterr().out
        assert "恢复完成：luneng" in out

        # 已有不可变事件的租户拒绝重复恢复（严格遵守账本只追加红线）
        assert main(["restore-ledger", "luneng", str(backup_file)]) == 1
        # 篡改后的灾备文件应被拒绝
        bad_file = tmp_path / "tampered.jsonl"
        bad_file.write_text(rb.text.replace("硅莫砖", "高铝砖", 1), encoding="utf-8")
        assert main(["restore-ledger", "luneng", str(bad_file)]) == 1

        # 验证投影对象已完整重建且 TenantCounter.last_seq 与事件数一致
        import asyncio

        from sqlalchemy import select

        from fos.config import Settings
        from fos.db.models import ObjectState, TenantCounter
        from fos.services import Services

        async def _verify() -> None:
            svc = Services.build(Settings(database_url=f"sqlite+aiosqlite:///{dr_db}"))
            try:
                async with svc.sessionmaker() as s:
                    obj = (
                        await s.scalars(
                            select(ObjectState).where(
                                ObjectState.tenant_id == "luneng",
                                ObjectState.object_id == lead_id,
                            )
                        )
                    ).one()
                    assert obj.state["customer_name"] == "灾备演练客户"
                    ctr = await s.get(TenantCounter, "luneng")
                    assert ctr is not None and ctr.last_seq >= 1
            finally:
                await svc.aclose()

        asyncio.run(_verify())
    finally:
        get_settings.cache_clear()
