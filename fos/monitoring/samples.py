"""窑炉监控演示数据（模拟数据，data_mode=mock）：一座梭式窑、四个测点、一个在烧窑次和最近 30 小时的读数。

演示读数按占位烧成制度加噪声生成，并故意放了一段“低于曲线”的超差，用来看告警和曲线图；
只能演示，不能当正式记录（规则 4）。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from fos.db.models import EdgeBatch, PointReading
from fos.ledger import AppendRequest, Evidence
from fos.monitoring import curve as cv
from fos.monitoring.config import MonitorConfig

KILN = "K-DEMO1"
RUN = "KR-DEMO1"
EDGE = "edge-demo"
CURVE = "QG-1300"
HOURS = 30.0
_T = {"kind": "temperature", "unit": "℃"}
_EDGE = {"source": "retrofit", "edge_id": EDGE, "interval_s": 60}
POINTS = [
    {"code": "demo1.top.temp", "name": "窑顶温度", **_T, **_EDGE, "high_limit": 1340, "curve_role": True},
    {"code": "demo1.bottom.temp", "name": "窑底温度", **_T, **_EDGE, "high_limit": 1340},
    {"code": "demo1.kwh", "name": "窑炉电表", "kind": "electricity", "unit": "kWh", "source": "direct",
     "edge_id": EDGE, "interval_s": 300},
    {"code": "demo1.panel.temp", "name": "仪表盘温度（抄表）", **_T, "source": "manual", "interval_s": 7200},
]  # fmt: skip


def ledger_requests(now: datetime) -> list[AppendRequest]:
    mk = {"data_mode": "mock"}
    out = [
        AppendRequest(
            type="production.kiln_registered",
            object_id=KILN,
            payload={
                "name": "演示 1 号梭式窑",
                "kiln_type": "shuttle",
                "access_path": "retrofit",
                "max_temp": 1400,
            },
            idempotency_key=f"kiln-demo:{KILN}",
            **mk,
        ),
        AppendRequest(
            type="production.kiln_curve_set",
            object_id=KILN,
            payload={"firing_curve": CURVE},
            idempotency_key=f"kiln-demo:{KILN}:curve",
            **mk,
        ),
    ]
    for p in POINTS:
        out.append(
            AppendRequest(
                type="production.point_registered",
                object_id=f"MP-{p['code']}",
                payload={**p, "kiln": KILN, "status": "active"},
                idempotency_key=f"kiln-demo:MP-{p['code']}",
                **mk,
            )
        )
    out.append(
        AppendRequest(
            type="production.kiln_run_started",
            object_id=RUN,
            payload={
                "kiln": KILN,
                "run_no": "演示窑次-01",
                "started_at": (now - timedelta(hours=HOURS)).isoformat(),
                "status": "firing",
            },
            evidence=[Evidence(kind="manual", ref="kiln-demo", note="演示数据")],
            idempotency_key=f"kiln-demo:{RUN}",
            **mk,
        )
    )
    return out


def readings(tenant_id: str, cfg: MonitorConfig, start: datetime, now: datetime, seed: int = 7) -> list:
    """按烧成制度生成读数；第 20–21 小时窑顶温度比曲线低 60℃（演示超差）。"""
    rnd = random.Random(seed)
    curve = cfg.curves.get(CURVE)
    rows: list = []
    t, kwh = start, 52000.0
    while t <= now:
        h = (t - start).total_seconds() / 3600
        target = cv.expected(curve, h) if curve else 20 + 40 * h
        target = target if target is not None else 100
        top = target + rnd.gauss(0, 6) - (60 if 20 <= h <= 21 else 0)
        bottom = target - 35 + rnd.gauss(0, 8)
        kwh += 5 * (0.6 + 0.4 * math.sin(h / 5) ** 2)
        for code, v, unit in (("demo1.top.temp", top, "℃"), ("demo1.bottom.temp", bottom, "℃")):
            rows.append(_row(tenant_id, code, t, round(v, 1), unit, "retrofit"))
        if int(h * 60) % 5 == 0:
            rows.append(_row(tenant_id, "demo1.kwh", t, round(kwh, 1), "kWh", "direct"))
        t += timedelta(minutes=2)
    for i in range(0, int(HOURS), 2):  # 仪表盘每 2 小时抄一次
        ts = start + timedelta(hours=i)
        v = (cv.expected(curve, i) if curve else 100) or 100
        rows.append(_row(tenant_id, "demo1.panel.temp", ts, round(v + rnd.gauss(0, 10)), "℃", "manual"))
    rows.append(
        EdgeBatch(
            tenant_id=tenant_id,
            edge_id=EDGE,
            seq=int(now.timestamp()),
            backfill=False,
            accepted=len(rows),
            rejected={},
            received_by="kiln-demo",
            received_at=now,
        )
    )
    return rows


def _row(tenant_id: str, code: str, ts: datetime, value: float, unit: str, source: str) -> PointReading:
    return PointReading(
        tenant_id=tenant_id,
        point=code,
        ts=ts,
        value=value,
        unit=unit,
        source=source,
        quality="good",
        edge_id=None if source == "manual" else EDGE,
        data_mode="mock",
        received_at=ts,
    )
