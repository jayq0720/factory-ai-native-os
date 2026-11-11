"""W19 窑炉监控：曲线比对（纯函数）、fos-edge 上行去重、告警判定与推送、抄表、结束窑次入账、能耗、演示。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fos.monitoring import curve as cv
from fos.monitoring.config import Curve

T0 = datetime(2026, 11, 8, tzinfo=UTC)
CURVE = Curve(name="测试", tolerance=30, points=[(0, 20), (10, 1020), (20, 1020)])


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


def ev(client, h, type_, payload, object_id, who="admin"):
    body = {"type": type_, "payload": payload, "object_id": object_id}
    body["evidence"] = [{"kind": "manual", "ref": "test"}]
    r = client.post("/v1/events", json=body, headers=h(who))
    assert r.status_code == 201, r.text
    return r.json()


def test_curve_math():
    assert cv.expected(CURVE, 5) == 520
    assert cv.expected(CURVE, 15) == 1020
    assert cv.expected(CURVE, 25) is None
    with pytest.raises(ValueError):
        Curve(name="x", tolerance=1, points=[(1, 20), (2, 30)])
    ss = [cv.Sample(T0 + timedelta(minutes=i), 520 + (50 if 3 <= i <= 8 else 0)) for i in range(12)]
    start = T0 - timedelta(hours=5)
    ss = [
        cv.Sample(s.ts, (cv.expected(CURVE, (s.ts - start).total_seconds() / 3600) or 0) + s.value - 520)
        for s in ss
    ]
    ex = cv.curve_excess(CURVE, start, ss)
    sp = cv.spans(ss, ex, timedelta(minutes=3))
    assert len(sp) == 1 and sp[0].minutes == 5 and sp[0].worst == pytest.approx(50)
    sm = cv.summarize(CURVE, start, ss, min_minutes=5, max_gap=timedelta(minutes=3))
    assert sm.deviation and sm.deviation_minutes == 5
    assert not cv.summarize(CURVE, start, ss, min_minutes=6, max_gap=timedelta(minutes=3)).deviation
    # 读数中断超过 max_gap 视为两段
    gap = [cv.Sample(T0, 0), cv.Sample(T0 + timedelta(minutes=10), 0)]
    assert len(cv.spans(gap, [1, 1], timedelta(minutes=3))) == 2
    # 电表：换表回零不算负数；质量差的读数跳过
    m = [
        cv.Sample(T0, 100),
        cv.Sample(T0, 150),
        cv.Sample(T0, 9999, "bad"),
        cv.Sample(T0, 5),
        cv.Sample(T0, 25),
    ]
    assert cv.meter_usage(m) == 70
    lim = cv.limit_excess(10, 100, [cv.Sample(T0, 5), cv.Sample(T0, 50), cv.Sample(T0, 120)])
    assert lim == [-5, None, 20]


def _setup(client, h, now):
    ev(client, h, "production.kiln_registered", {"name": "1号梭式窑", "kiln_type": "shuttle",
       "access_path": "retrofit", "max_temp": 1400}, "K1")  # fmt: skip
    ev(client, h, "production.kiln_curve_set", {"firing_curve": "QG-1300"}, "K1")
    base = {"kiln": "K1", "unit": "℃", "kind": "temperature", "status": "active"}
    pts = [
        (
            "k1.top",
            "窑顶温度",
            {"source": "retrofit", "edge_id": "edge-1", "interval_s": 60, "curve_role": True},
        ),
        (
            "k1.bottom",
            "窑底温度",
            {"source": "retrofit", "edge_id": "edge-1", "interval_s": 60, "high_limit": 300},
        ),
        ("k1.door", "窑门温度", {"source": "direct", "edge_id": "edge-2", "interval_s": 60}),
        ("k1.flue", "烟道温度", {"source": "direct", "edge_id": "edge-1", "interval_s": 60}),
        ("k1.panel", "仪表盘温度", {"source": "manual", "interval_s": 3600}),
    ]
    for code, name, extra in pts:
        ev(
            client,
            h,
            "production.point_registered",
            {**base, "code": code, "name": name, **extra},
            f"MP-{code}",
        )
    meter = {"kiln": "K1", "code": "k1.kwh", "name": "电表", "kind": "electricity", "unit": "kWh"}
    meter |= {"source": "direct", "edge_id": "edge-1", "interval_s": 60, "status": "active"}
    ev(client, h, "production.point_registered", meter, "MP-k1.kwh")
    start = now - timedelta(hours=10)
    run = {"kiln": "K1", "run_no": "K1-001", "started_at": start.isoformat(), "status": "firing"}
    ev(client, h, "production.kiln_run_started", run, "KR1")
    return start


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _batch(seq, readings, edge="edge-1", tenant="luneng"):
    return {"protocol": 1, "tenant_id": tenant, "edge_id": edge, "seq": seq, "readings": readings}


def _r(point, t, value, unit="℃", source="retrofit", quality="good"):
    return {
        "point": point,
        "ts_ms": _ms(t),
        "value": value,
        "unit": unit,
        "source": source,
        "quality": quality,
    }


def test_kiln_monitoring_flow(client, h, keys):
    from fos.monitoring.watch import watch_once

    svc = client.app.state.services
    adapter = client.app.state.mock_channel
    now = datetime.now(UTC).replace(microsecond=0)
    start = _setup(client, h, now)

    # —— fos-edge 上行：窑顶按曲线走，最后 6 分钟比曲线低 80℃；窑底超上限 300；电表累计 ——
    cfg = svc.monitoring["luneng"]
    curve = cfg.curves["QG-1300"]
    rs = []
    for i in range(30, -1, -1):
        t = now - timedelta(minutes=i)
        exp = cv.expected(curve, (t - start).total_seconds() / 3600)
        rs.append(_r("k1.top", t, round(exp - (80 if i <= 6 else 0), 1)))
        rs.append(_r("k1.bottom", t, 380 if i <= 3 else 280))
        rs.append(_r("k1.kwh", t, 1000 + 2 * (30 - i), unit="kWh", source="direct"))
    rs.append(_r("k1.unknown", now, 1))
    rs.append(_r("k1.door", now, 1))  # 登记的网关是 edge-2
    rs.append(_r("k1.flue", now, 1, unit="°F"))
    rs.append({**_r("k1.top", now, 1300), "mock": True})  # 模拟器读数不能进正式测点
    body = _batch(1, rs)
    assert client.post("/v1/edge/uplink", json=body, headers=h("sales")).status_code == 403
    assert client.post("/v1/edge/uplink", json={**body, "protocol": 9}, headers=h("edge")).status_code == 422
    other = _batch(1, rs[:1], tenant="other")
    assert client.post("/v1/edge/uplink", json=other, headers=h("edge")).status_code == 403
    r = client.post("/v1/edge/uplink", json=body, headers=h("edge")).json()
    assert r["accepted"] == 93 and set(r["rejected"]) == {"k1.unknown", "k1.door", "k1.flue", "k1.top"}
    assert "模拟" in r["rejected"]["k1.top"]
    dup = client.post("/v1/edge/uplink", json=body, headers=h("edge")).json()
    assert dup == {
        "duplicate": True,
        "seq": 1,
        "accepted": 0,
        "rejected": {},
        "commands": [],
    }  # 断网补传 / 重发不重复入库

    # —— 判定一轮：超差、超上限、断网（edge-2）、设备故障（k1.flue）、抄表提醒（k1.panel） ——
    for ext, login in (("im-lead", "shift_lead"), ("im-w", "worker")):
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": ext, "login": login},
            headers=h("admin"),
        )
    n0 = len(adapter.outbox)
    res = client.portal.call(watch_once, svc, "luneng", adapter, now + timedelta(seconds=10))
    kinds = sorted(a.key for a in res.opened)
    assert kinds == ["deviation:k1.top", "edge_offline:edge-2", "high:k1.bottom", "point_fault:k1.flue"]
    assert [x.point.code for x in res.reminders] == ["k1.panel"]
    sent = [str(m) for m in adapter.outbox[n0:]]
    assert sum("窑炉告警" in m for m in sent) == 4  # 告警只推班组长
    assert sum("抄表提醒" in m for m in sent) == 2  # 抄表提醒推班组长和工人
    dev = next(a for a in res.opened if a.kind == "deviation")
    assert "QG-1300" in dev.message and "低 80" in dev.message
    # 同一时刻再判一轮：不重复推送、不重复提醒
    n1 = len(adapter.outbox)
    res2 = client.portal.call(watch_once, svc, "luneng", adapter, now + timedelta(seconds=40))
    assert not res2.opened and not res2.push and not res2.reminders and len(adapter.outbox) == n1

    # —— 窑况 ——
    b = client.get("/v1/kiln/board", headers=h("worker")).json()
    k1 = b["kilns"][0]
    assert k1["run"]["run_no"] == "K1-001" and k1["run"]["curve"] == "QG-1300"
    top = next(p for p in k1["points"] if p["point"] == "k1.top")
    assert top["deviation"] == pytest.approx(-80, abs=0.2)
    assert len(k1["alarms"]) == 3 and [a["kind"] for a in b["alarms"]] == ["edge_offline"]
    assert client.get("/v1/kiln/board", headers=h("sales")).status_code == 403
    assert client.get("/v1/kiln/board", headers=h("other_admin")).json()["kilns"] == []  # 租户隔离

    # —— IM：窑况、抄表 ——
    def say(i, text, sender="im-w"):
        body = {"external_msg_id": f"k{i}", "sender_id": sender, "kind": "text", "text": text}
        return client.post("/v1/channels/mock/messages", json=body, headers=h("admin")).json()

    card = say(1, "窑况")["replies"][0]["card"]
    assert card["title"] == "窑况" and "4 条未关闭告警" in card["text"]
    assert say(2, "窑况 1号梭式窑")["replies"][0]["card"]["title"] == "窑况 · 1号梭式窑"
    o = say(3, "抄表 k1.panel 405")
    assert o["route"] == "kiln_meter" and "已记录 仪表盘温度" in o["replies"][0]["text"]
    assert "没有找到" in say(4, "抄表 不存在 1")["replies"][0]["text"]
    assert "超上限" not in say(5, "抄表 仪表盘温度 404", sender="im-lead")["replies"][0]["text"]

    # —— 读数恢复正常 → 告警自动关闭；确认告警 ——
    t = now + timedelta(minutes=2)
    ok = [
        _r("k1.top", t, round(cv.expected(curve, (t - start).total_seconds() / 3600), 1)),
        _r("k1.bottom", t, 250),
    ]
    ok.append(_r("k1.flue", t, 600, source="direct"))
    assert client.post("/v1/edge/uplink", json=_batch(2, ok), headers=h("edge")).json()["accepted"] == 3
    res3 = client.portal.call(watch_once, svc, "luneng", adapter, t + timedelta(seconds=5))
    assert sorted(a.key for a in res3.closed) == ["deviation:k1.top", "high:k1.bottom", "point_fault:k1.flue"]
    left = client.get("/v1/kiln/alarms", headers=h("worker")).json()["alarms"]
    assert [a["kind"] for a in left] == ["edge_offline"]
    assert client.post(f"/v1/kiln/alarms/{left[0]['id']}/ack", headers=h("worker")).status_code == 403
    acked = client.post(f"/v1/kiln/alarms/{left[0]['id']}/ack", headers=h("shift_lead")).json()
    assert acked["acked_by_name"] == "shift_lead"
    assert (
        len(client.get("/v1/kiln/alarms", params={"status": "closed"}, headers=h("worker")).json()["alarms"])
        == 3
    )

    # —— 窑次曲线与结束窑次：最高温、超差时长、能耗由读数算出并入账 ——
    c = client.get("/v1/kiln/runs/KR1/curve", headers=h("worker")).json()
    assert c["curve"]["from"] == "窑炉默认" and c["deviation"] and c["deviation_minutes"] == 6
    assert c["energy"]["electricity_kwh"] == 60
    end = (t + timedelta(seconds=30)).isoformat()
    assert (
        client.post("/v1/kiln/runs/KR1/finish", json={"ended_at": end}, headers=h("worker")).status_code
        == 403
    )
    f = client.post("/v1/kiln/runs/KR1/finish", json={"ended_at": end}, headers=h("shift_lead"))
    assert f.status_code == 200, f.text
    st = client.get("/v1/objects/kiln_run/KR1", headers=h("admin")).json()["state"]
    assert st["status"] == "finished" and st["deviation"] is True and st["deviation_minutes"] == 6
    assert st["electricity_kwh"] == 60 and st["firing_curve"] == "QG-1300" and st["max_temp"] > 300
    evs = client.get("/v1/events", params={"type": "production.kiln_run_finished"}, headers=h("admin")).json()
    item = evs["events"][0]
    assert any(e["kind"] == "sensor" and e["ref"].startswith("k1.top@") for e in item["evidence"])
    again = client.post("/v1/kiln/runs/KR1/finish", json={}, headers=h("shift_lead"))
    assert again.status_code == 409

    # —— 能耗 ——
    e = client.get(
        "/v1/kiln/energy",
        params={"start": (now - timedelta(hours=1)).isoformat(), "end": t.isoformat()},
        headers=h("worker"),
    ).json()
    assert e["rows"][0]["name"] == "1号梭式窑" and e["rows"][0]["electricity_kwh"] == 60

    # —— 人工抄表 API：未来时间拒绝 ——
    fut = {"point": "k1.panel", "value": 1, "ts": (now + timedelta(hours=1)).isoformat()}
    assert client.post("/v1/kiln/readings", json=fut, headers=h("worker")).status_code == 422
    assert (
        client.post(
            "/v1/kiln/readings", json={"point": "仪表盘", "value": 400}, headers=h("worker")
        ).status_code
        == 201
    )


def test_demo_data_is_mock(client, h):
    r = client.post("/v1/kiln/demo", headers=h("admin")).json()
    assert r["created"] and r["readings"] > 1000
    assert client.post("/v1/kiln/demo", headers=h("admin")).json()["created"] is False
    assert client.post("/v1/kiln/demo", headers=h("worker")).status_code == 403
    b = client.get("/v1/kiln/board", headers=h("admin")).json()
    assert b["data_mode"] == "mock" and b["kilns"][0]["run"]["curve"] == "QG-1300"
    # fos-edge 连仪表模拟器（edge.sim.toml）上行的模拟读数可以写进演示测点
    now = datetime.now(UTC)
    sim = [
        {**_r("demo1.top.temp", now, 1290.5), "mock": True},
        {**_r("demo1.kwh", now, 52100, "kWh", "direct"), "mock": True},
    ]
    up = client.post("/v1/edge/uplink", json=_batch(7, sim, edge="edge-demo"), headers=h("edge")).json()
    assert up["accepted"] == 2 and up["rejected"] == {}
    c = client.get("/v1/kiln/runs/KR-DEMO1/curve", headers=h("admin")).json()
    assert c["data_mode"] == "mock" and c["deviation"] and c["lines"][0]["spans"]
    f = client.post("/v1/kiln/runs/KR-DEMO1/finish", json={}, headers=h("admin")).json()
    ev = client.get("/v1/objects/kiln_run/KR-DEMO1", headers=h("admin")).json()
    assert ev["data_mode"] == "mock" and f["summary"]["data_mode"] == "mock"
