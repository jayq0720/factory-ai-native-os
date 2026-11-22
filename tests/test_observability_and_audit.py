"""生产可观测性（Prometheus 指标、请求耗时头）与不可变账本审计流水回归测试。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_prometheus_metrics_and_response_time_header(client, keys) -> None:
    """验证 X-Response-Time-Ms 响应头及受权限保护的 Prometheus /v1/ops/metrics 端点。"""
    rh = client.get("/healthz")
    assert rh.status_code == 200
    assert "x-request-id" in rh.headers
    assert float(rh.headers["x-response-time-ms"]) >= 0.0

    # 非管理员禁止抓取运维指标
    assert client.get("/v1/ops/metrics", headers=_h(keys, "sales")).status_code == 403

    rm = client.get("/v1/ops/metrics", headers=_h(keys, "admin"))
    assert rm.status_code == 200
    assert rm.headers["content-type"].startswith("text/plain")
    txt = rm.text
    assert "fos_http_requests_total" in txt
    assert "fos_http_request_duration_seconds_count" in txt
    assert "fos_tenants_total" in txt
    assert 'fos_events_total{tenant="luneng"}' in txt
    assert 'fos_security_baseline_ok{tenant="luneng"} 1' in txt


def test_audit_log_query_and_filtering(client, keys) -> None:
    """验证不可变事件账本审计流水倒序分页、事件前缀与数据模式过滤。"""
    assert client.get("/v1/ops/audit", headers=_h(keys, "sales")).status_code == 403

    close_dt = (date.today() + timedelta(days=15)).isoformat()
    for name, mode in [("宝钢审计测试", "real"), ("模拟审计测试", "mock")]:
        r = client.post(
            "/v1/market/leads",
            json={
                "customer_name": name,
                "industry": "steel",
                "product_interest": "高铝砖",
                "est_amount": 120000.0,
                "owner_name": "审计员",
                "expected_close_date": close_dt,
                "data_mode": mode,
            },
            headers=_h(keys, "sales"),
        )
        assert r.status_code == 201

    ra = client.get("/v1/ops/audit?event_prefix=market.&limit=10", headers=_h(keys, "admin"))
    assert ra.status_code == 200
    data = ra.json()
    assert data["total"] >= 2
    seqs = [it["seq"] for it in data["items"]]
    assert seqs == sorted(seqs, reverse=True)
    assert all(it["type"].startswith("market.") for it in data["items"])
    assert all(it["evidence_count"] >= 1 for it in data["items"])

    ra_real = client.get(
        "/v1/ops/audit?event_prefix=market.&data_mode=real&limit=1", headers=_h(keys, "admin")
    )
    assert ra_real.status_code == 200
    d_real = ra_real.json()
    assert len(d_real["items"]) == 1
    assert d_real["items"][0]["data_mode"] == "real"

    ops_html = (Path(__file__).resolve().parent.parent / "fos/web/static/ops.html").read_text(
        encoding="utf-8"
    )
    assert 'id="auditTable"' in ops_html
