"""厂端网关车队在线状态 API（GET /v1/ops/edge-fleet）与窑炉/网关 Prometheus 指标回归测试。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fos.db.models import EdgeBatch, EdgeCommandRecord, KilnAlarm, utcnow
from fos.db.session import make_engine, make_sessionmaker


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_edge_fleet_api_and_prometheus_metrics(client, keys) -> None:
    """验证 /v1/ops/edge-fleet 权限隔离、在线/离线统计、未闭环告警及 Prometheus 导出。"""
    assert client.get("/v1/ops/edge-fleet", headers=_h(keys, "sales")).status_code == 403

    now = utcnow()
    stale = now - timedelta(minutes=20)
    db_url = client.app.state.settings.database_url
    rows = [
        EdgeBatch(
            tenant_id="luneng",
            edge_id="gw-kiln-01",
            seq=11,
            backfill=False,
            accepted=8,
            rejected={},
            received_by="edge-user",
            received_at=now - timedelta(seconds=30),
        ),
        EdgeBatch(
            tenant_id="luneng",
            edge_id="gw-kiln-02",
            seq=5,
            backfill=False,
            accepted=4,
            rejected={},
            received_by="edge-user",
            received_at=stale,
        ),
        KilnAlarm(
            tenant_id="luneng",
            key="high:kiln1_top",
            kind="high",
            point="kiln1_top",
            edge_id="gw-kiln-01",
            status="open",
            message="1号窑顶部超温",
            detail={"value": 1410},
            started_at=now - timedelta(minutes=3),
            last_seen_at=now,
        ),
        KilnAlarm(
            tenant_id="luneng",
            key="low:kiln2_bot",
            kind="low",
            point="kiln2_bot",
            edge_id="gw-kiln-02",
            status="closed",
            message="已恢复",
            detail={},
            started_at=stale,
            last_seen_at=stale,
            closed_at=stale,
        ),
        EdgeCommandRecord(
            tenant_id="luneng",
            edge_id="gw-kiln-01",
            command={"type": "status"},
            status="pending",
            created_by="admin",
            created_by_name="管理员",
            expires_at=now + timedelta(minutes=10),
        ),
    ]

    async def _seed() -> None:
        eng = make_engine(db_url)
        try:
            sm = make_sessionmaker(eng)
            async with sm() as s, s.begin():
                s.add_all(rows)
        finally:
            await eng.dispose()

    asyncio.run(_seed())

    rf = client.get("/v1/ops/edge-fleet?online_window_s=300", headers=_h(keys, "admin"))
    assert rf.status_code == 200
    data = rf.json()
    assert data["gateways_total"] == 2
    assert data["gateways_online"] == 1
    assert data["open_alarms_total"] == 1

    by_id = {g["edge_id"]: g for g in data["gateways"]}
    assert by_id["gw-kiln-01"]["online"] is True
    assert by_id["gw-kiln-01"]["pending_commands"] == 1
    assert by_id["gw-kiln-01"]["open_alarms"] == 1
    assert by_id["gw-kiln-02"]["online"] is False
    assert by_id["gw-kiln-02"]["open_alarms"] == 0

    rm = client.get("/v1/ops/metrics", headers=_h(keys, "admin"))
    assert rm.status_code == 200
    assert 'fos_edge_gateways_total{tenant="luneng"} 2' in rm.text
    assert 'fos_edge_gateways_online{tenant="luneng"} 1' in rm.text
    assert 'fos_kiln_open_alarms{tenant="luneng"} 1' in rm.text
