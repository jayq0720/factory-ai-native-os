"""行业包独立完整性校验、CLI 门禁与运维目录接口回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fos.cli import main
from fos.config import get_settings
from fos.ops.packs import list_packs, validate_pack

REPO = Path(__file__).resolve().parent.parent


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_validate_refractory_pack_and_detect_broken_pack(tmp_path: Path) -> None:
    """验证内置 refractory 行业包 100% 通过，且损坏 YAML / 未知行业包能精准拦截。"""
    rep = validate_pack(REPO / "packs", "refractory")
    assert rep["ok"] is True, rep["errors"]
    assert rep["objects_count"] >= 30
    assert rep["events_count"] >= 60
    assert rep["skills_count"] >= 14
    assert rep["eval_cases"] >= 50
    assert len(rep["modules"]) >= 12

    all_packs = list_packs(REPO / "packs")
    assert len(all_packs) >= 1 and all(p["ok"] for p in all_packs)

    # 不存在的行业包返回 ok=False
    miss = validate_pack(REPO / "packs", "no-such-pack")
    assert miss["ok"] is False and miss["errors"]

    # 构造损坏 YAML 的临时行业包应精准报错
    bad_dir = tmp_path / "broken-pack"
    bad_dir.mkdir()
    (bad_dir / "ontology.yaml").write_text("objects: [\n  unclosed", encoding="utf-8")
    bad_rep = validate_pack(tmp_path, "broken-pack")
    assert bad_rep["ok"] is False and any("YAML" in e or "本体" in e for e in bad_rep["errors"])


def test_cli_validate_pack_and_ops_api(
    client, keys, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证 fos validate-pack CLI 与 GET /v1/ops/packs 权限隔离与目录输出。"""
    assert client.get("/v1/ops/packs", headers=_h(keys, "sales")).status_code == 403
    r = client.get("/v1/ops/packs", headers=_h(keys, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["all_ok"] is True and body["count"] >= 1
    ref = next(it for it in body["items"] if it["pack"] == "refractory")
    assert ref["eval_cases"] >= 50

    monkeypatch.setenv("FOS_PACKS_DIR", str(REPO / "packs"))
    get_settings.cache_clear()
    try:
        assert main(["validate-pack"]) == 0
        out = capsys.readouterr().out
        assert "[OK] pack=refractory" in out
        assert main(["validate-pack", "nonexistent"]) == 1
    finally:
        get_settings.cache_clear()
