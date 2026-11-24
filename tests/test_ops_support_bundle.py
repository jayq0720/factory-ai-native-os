"""脱敏一键运维诊断包（GET /v1/ops/support-bundle.zip & fos support-bundle）回归测试。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from fos.cli import main
from fos.config import get_settings


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_support_bundle_api_sanitization_and_hashes(client, keys) -> None:
    """验证 /v1/ops/support-bundle.zip 权限隔离、8 份文件 SHA-256 完整性及零明文密钥泄露。"""
    assert client.get("/v1/ops/support-bundle.zip", headers=_h(keys, "sales")).status_code == 403

    r = client.get("/v1/ops/support-bundle.zip?audit_limit=20", headers=_h(keys, "admin"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    expected = {
        "manifest.json",
        "doctor.json",
        "security.json",
        "budget.json",
        "edge_fleet.json",
        "packs.json",
        "eval.json",
        "ledger_check.json",
        "audit_recent.json",
    }
    assert expected.issubset(names)

    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["format"] == "fos-support-bundle-v1"
    assert manifest["tenant_id"] == "luneng"
    assert manifest["files_count"] == 8

    admin_key = keys["admin"]
    for entry in manifest["files"]:
        raw = zf.read(entry["name"])
        assert len(raw) == entry["bytes"]
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        text = raw.decode("utf-8")
        assert admin_key not in text

    doctor = json.loads(zf.read("doctor.json").decode("utf-8"))
    assert doctor["ok"] is True


def test_support_bundle_cli_and_web_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证 CLI `fos support-bundle` 生成有效 ZIP，且 ops.html/ops.js 已挂载下载按钮。"""
    db_file = tmp_path / "bundle.db"
    out_zip = tmp_path / "luneng_support.zip"
    monkeypatch.setenv("FOS_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        assert main(["support-bundle", "luneng", str(out_zip)]) == 0
        out = capsys.readouterr().out
        assert "已生成脱敏运维诊断包" in out
        assert out_zip.exists() and out_zip.stat().st_size > 0
        with zipfile.ZipFile(out_zip) as zf:
            assert "manifest.json" in zf.namelist()
            assert "doctor.json" in zf.namelist()
        assert main(["support-bundle", "nonexistent-tenant", str(out_zip)]) == 1
    finally:
        get_settings.cache_clear()

    repo = Path(__file__).resolve().parent.parent
    html = (repo / "fos" / "web" / "static" / "ops.html").read_text(encoding="utf-8")
    js = (repo / "fos" / "web" / "static" / "ops.js").read_text(encoding="utf-8")
    assert "btnSupportBundle" in html and "btnSupportBundle" in js
    assert "/v1/ops/support-bundle.zip" in js
