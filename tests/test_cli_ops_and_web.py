"""W30 生产运维 CLI 命令与全站静态页面资产完整性回归测试。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fos.cli import main
from fos.config import get_settings

STATIC_DIR = Path(__file__).resolve().parent.parent / "fos" / "web" / "static"
ASSET_RE = re.compile(r"""(?:href|src)=["']([^"']+)["']""")


def test_all_static_html_asset_links_exist() -> None:
    """所有静态 HTML 页面引用的相对 CSS/JS 文件必须真实存在，杜绝生产 404 断链。"""
    html_files = sorted(STATIC_DIR.glob("*.html"))
    assert len(html_files) >= 14
    missing: list[str] = []
    for hf in html_files:
        text = hf.read_text(encoding="utf-8")
        for ref in ASSET_RE.findall(text):
            if ref.startswith(("http://", "https://", "/", "#", "data:")):
                continue
            if not (STATIC_DIR / ref).exists():
                missing.append(f"{hf.name} -> {ref}")
    assert missing == [], f"发现断链静态资源：{missing}"
    ops_html = (STATIC_DIR / "ops.html").read_text(encoding="utf-8")
    assert 'id="btnEval"' in ops_html
    assert 'id="evalTable"' in ops_html


def test_cli_eval_security_and_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证 fos eval / security-check / watchdog 三个生产运维 CLI 命令。"""
    db_file = tmp_path / "cli_ops.db"
    monkeypatch.setenv("FOS_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        assert main(["eval", "luneng"]) == 0
        out = capsys.readouterr().out
        assert "100.0%" in out
        assert main(["eval", "--all"]) == 0

        bad_suite = tmp_path / "bad.yaml"
        bad_suite.write_text(
            "fast_path_routing:\n  - {id: fp_bad, text: 设备, expect: router}\n",
            encoding="utf-8",
        )
        assert main(["eval", "luneng", "--suite", str(bad_suite)]) == 1
        out_bad = capsys.readouterr().out
        assert "[BADCASE]" in out_bad

        assert main(["security-check", "luneng"]) == 0
        out_sec = capsys.readouterr().out
        assert "[OK" in out_sec
        assert main(["security-check", "--all", "--json"]) == 0
        sec_all = json.loads(capsys.readouterr().out)
        assert sec_all["all_ok"] is True and sec_all["tenants_count"] >= 1
        assert main(["security-check", "luneng", "--json"]) == 0
        sec_one = json.loads(capsys.readouterr().out)
        assert sec_one["passed"] is True
        assert main(["security-check"]) == 1

        assert main(["watchdog", "luneng"]) == 0
        out_wd = capsys.readouterr().out
        assert "Watchdog" in out_wd

        assert main(["eval", "nonexistent-tenant"]) == 1
    finally:
        get_settings.cache_clear()
