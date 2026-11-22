"""W29 黄金数据集评测与 Badcase 回归测试。"""

from __future__ import annotations

from pathlib import Path

from fos.eval.runner import run_golden_eval


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_golden_eval_suite_passes_100_percent(client, keys):
    assert client.get("/v1/ops/eval", headers=_h(keys, "sales")).status_code == 403
    r = client.get("/v1/ops/eval", headers=_h(keys, "admin"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["all_passed"] is True, data["badcases"]
    assert data["pass_rate_pct"] == 100.0 and data["failed"] == 0
    assert data["total"] >= 45
    assert set(data["categories"]) == {
        "fast_path_routing",
        "skill_routing",
        "deterministic_calculators",
        "policy_redlines",
    }


def test_badcase_regression_detection(client, keys, tmp_path: Path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "fast_path_routing:\n  - {id: bad_fp, text: 隐患, expect: router}\n",
        encoding="utf-8",
    )
    res = run_golden_eval(client.app.state.services, "luneng", suite_path=bad_yaml)
    assert res["all_passed"] is False and res["failed"] == 1
    assert res["badcases"][0]["id"] == "bad_fp"
    assert res["badcases"][0]["actual"] == "ehs_hazards"
