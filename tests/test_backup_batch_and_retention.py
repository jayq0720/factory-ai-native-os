"""全租户批量防篡改灾备、落盘自检与保留策略清理回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fos.cli import main
from fos.config import Settings, get_settings
from fos.ops.backup import backup_all_tenants, prune_old_backups, verify_ledger_backup_jsonl
from fos.services import Services


def test_prune_old_backups_unit(tmp_path: Path) -> None:
    """验证按文件名倒序保留最近 keep 份备份并删除更早文件。"""
    for ts in ("20261101_000001", "20261102_000001", "20261103_000001", "20261104_000001"):
        (tmp_path / f"luneng_{ts}.jsonl").write_text("x\n", encoding="utf-8")
    removed = prune_old_backups(tmp_path, "luneng", keep=2)
    assert removed == 2
    left = sorted(p.name for p in tmp_path.glob("luneng_*.jsonl"))
    assert left == ["luneng_20261103_000001.jsonl", "luneng_20261104_000001.jsonl"]
    assert prune_old_backups(tmp_path, "luneng", keep=0) == 0


def test_batch_backup_manifest_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证 backup_all_tenants 落盘即校验、清单生成、保留裁剪及 CLI --all 模式。"""
    db_file = tmp_path / "batch.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    out_dir = tmp_path / "backups"

    monkeypatch.setenv("FOS_DATABASE_URL", db_url)
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0

        async def _run_batch() -> None:
            svc = Services.build(Settings(database_url=db_url))
            try:
                async with svc.sessionmaker() as session:
                    r1 = await backup_all_tenants(
                        session, ["luneng"], out_dir, keep=2, stamp="20261101_010000"
                    )
                    r2 = await backup_all_tenants(
                        session, ["luneng"], out_dir, keep=2, stamp="20261102_010000"
                    )
                    r3 = await backup_all_tenants(
                        session, ["luneng"], out_dir, keep=2, stamp="20261103_010000"
                    )
                assert r1["tenants_count"] == 1 and r1["pruned_total"] == 0
                assert r2["pruned_total"] == 0
                assert r3["pruned_total"] == 1
                manifest = json.loads((out_dir / r3["manifest"]).read_text(encoding="utf-8"))
                assert manifest["tenants_count"] == 1
                item = manifest["items"][0]
                assert item["verified"] is True
                ver = verify_ledger_backup_jsonl((out_dir / item["file"]).read_text(encoding="utf-8"))
                assert ver["ok"] is True
                assert ver["sha256"] == item["sha256"]
            finally:
                await svc.aclose()

        asyncio.run(_run_batch())

        # CLI --all 模式
        rc = main(["backup-ledger", "--all", "--out-dir", str(out_dir), "--keep", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "全租户灾备完成" in out
        assert len(list(out_dir.glob("luneng_*.jsonl"))) == 2

        # 缺少单租户参数且未传 --all 时返回非零
        assert main(["backup-ledger"]) == 1
    finally:
        get_settings.cache_clear()
