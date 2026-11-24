"""Prometheus 抓取/告警规则包与 Ops 控制台 Doctor 面板回归测试。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_prometheus_configs_and_alert_metric_alignment(client, keys) -> None:
    """验证 deploy/monitoring YAML 合法，且每条告警引用的 fos_* 指标均在 /v1/ops/metrics 真实导出。"""
    prom_path = REPO_ROOT / "deploy" / "monitoring" / "prometheus.yml"
    alerts_path = REPO_ROOT / "deploy" / "monitoring" / "alerts.yml"
    prom = yaml.safe_load(prom_path.read_text(encoding="utf-8"))
    alerts = yaml.safe_load(alerts_path.read_text(encoding="utf-8"))

    assert "alerts.yml" in prom["rule_files"]
    jobs = {j["job_name"]: j for j in prom["scrape_configs"]}
    assert "fos-api" in jobs
    assert jobs["fos-api"]["metrics_path"] == "/v1/ops/metrics"

    rules = alerts["groups"][0]["rules"]
    names = {r["alert"] for r in rules}
    expected = {
        "FosHigh5xxRate",
        "FosHighAvgLatency",
        "FosTenantBudgetWarning",
        "FosTenantBudgetCircuitBroken",
        "FosSecurityBaselineFailed",
        "FosIndustryPackUnhealthy",
        "FosPendingApprovalsBacklog",
        "FosKilnOpenAlarms",
        "FosEdgeGatewayOffline",
    }
    assert expected.issubset(names)

    # 先触发一次健康请求确保 http_metrics 计数器已产出样本
    assert client.get("/healthz").status_code == 200
    rm = client.get("/v1/ops/metrics", headers=_h(keys, "admin"))
    assert rm.status_code == 200
    emitted = set(re.findall(r"^# TYPE (fos_[a-z0-9_]+)\s+", rm.text, flags=re.MULTILINE))

    for r in rules:
        assert r.get("expr") and r.get("for")
        assert r["labels"]["severity"] in {"warning", "critical"}
        assert r["annotations"].get("summary")
        referenced = set(re.findall(r"\bfos_[a-z0-9_]+\b", r["expr"]))
        assert referenced, f"告警 {r['alert']} 未引用任何 fos_* 指标"
        missing = referenced - emitted
        assert not missing, f"告警 {r['alert']} 引用了未导出指标：{missing}"


def test_grafana_dashboard_json_and_metric_alignment(client, keys) -> None:
    """验证 Grafana 大盘 JSON 合法，且全部面板引用的 fos_* 指标均在 /v1/ops/metrics 真实导出。"""
    dash_path = REPO_ROOT / "deploy" / "monitoring" / "grafana-dashboard.json"
    dash = json.loads(dash_path.read_text(encoding="utf-8"))
    assert dash["uid"] == "fos-prod-sre"
    panels = dash.get("panels") or []
    assert len(panels) >= 10

    assert client.get("/healthz").status_code == 200
    rm = client.get("/v1/ops/metrics", headers=_h(keys, "admin"))
    assert rm.status_code == 200
    emitted = set(re.findall(r"^# TYPE (fos_[a-z0-9_]+)\s+", rm.text, flags=re.MULTILINE))

    referenced_all: set[str] = set()
    for p in panels:
        targets = p.get("targets") or []
        assert targets, f"面板 {p.get('title')} 缺少 PromQL target"
        for t in targets:
            refs = set(re.findall(r"\bfos_[a-z0-9_]+\b", t.get("expr", "")))
            assert refs, f"面板 {p.get('title')} 未引用 fos_* 指标"
            missing = refs - emitted
            assert not missing, f"面板 {p.get('title')} 引用了未导出指标：{missing}"
            referenced_all |= refs
    assert len(referenced_all) >= 12


def test_ops_web_doctor_panel_hooks() -> None:
    """验证 ops.html 与 ops.js 正确挂载控制面 Doctor 与 Edge Fleet 面板。"""
    html = (REPO_ROOT / "fos" / "web" / "static" / "ops.html").read_text(encoding="utf-8")
    js = (REPO_ROOT / "fos" / "web" / "static" / "ops.js").read_text(encoding="utf-8")
    for marker in ("btnDoctor", "doctorSummary", "doctorTable", "edgeFleetSummary", "edgeFleetTable"):
        assert marker in html
        assert marker in js
    assert "/v1/ops/doctor" in js
    assert "/v1/ops/edge-fleet" in js
