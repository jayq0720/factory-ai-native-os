"""W25 EHS 安全环保端到端测试：隐患闭环、作业票审批与守卫、事故闭环、环保超标建单与 IM Fast Path。"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from openpyxl import load_workbook

from fos.ehs.compliance import compute_ehs_kpis, evaluate_env_reading
from fos.ehs.config import EhsConfig, EnvLimit


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_ehs_compliance_deterministic_math():
    cfg = EhsConfig(env_limits={"dust": EnvLimit(name="粉尘", unit="mg/m³", max=20.0)})
    ok = evaluate_env_reading(cfg, "dust", 15.0)
    assert ok["passed"] is True and ok["exceed_ratio"] == 0.75

    bad = evaluate_env_reading(cfg, "dust", 25.0)
    assert bad["passed"] is False and bad["exceed_ratio"] == 1.25

    with pytest.raises(ValueError, match="未配置"):
        evaluate_env_reading(cfg, "unknown", 1.0)
    with pytest.raises(ValueError, match="有限数字"):
        evaluate_env_reading(cfg, "dust", float("nan"))
    with pytest.raises(ValueError, match="负数"):
        evaluate_env_reading(cfg, "dust", -1.0)

    kpis = compute_ehs_kpis(
        hazards=[
            {"status": "verified", "level": "general", "overdue": False},
            {"status": "open", "level": "major", "overdue": True},
        ],
        permits=[
            {"kind": "hot_work", "status": "active"},
            {"kind": "high_altitude", "status": "closed"},
        ],
        incidents=[{"status": "closed", "injured_count": 0}, {"status": "open", "injured_count": 1}],
        env_readings=[{"passed": True}, {"passed": True}, {"passed": False}],
    )
    assert kpis["hazards_total"] == 2 and kpis["hazard_closure_rate_pct"] == 50.0
    assert kpis["hazards_overdue"] == 1 and kpis["hazards_major_open"] == 1
    assert kpis["permits_active"] == 1 and kpis["permits_high_risk"] == 1
    assert kpis["total_injured"] == 1 and kpis["env_compliance_rate_pct"] == 66.67


def test_ehs_end_to_end_flow(client, keys):
    today = date.today()
    overdue_day = (today - timedelta(days=2)).isoformat()
    future_day = (today + timedelta(days=5)).isoformat()

    # 1. 权限校验：销售无权上报隐患；工程师可上报
    assert (
        client.post(
            "/v1/ehs/hazards",
            json={
                "location": "一车间",
                "category": "dust",
                "level": "general",
                "description": "无权测试",
                "due_date": future_day,
            },
            headers=_h(keys, "sales"),
        ).status_code
        == 403
    )

    r_hz = client.post(
        "/v1/ehs/hazards",
        json={
            "location": "混炼车间 2# 布袋除尘",
            "category": "dust",
            "level": "general",
            "description": "除尘管道法兰漏风扬尘",
            "due_date": future_day,
        },
        headers=_h(keys, "engineer"),
    )
    assert r_hz.status_code == 201, r_hz.text
    hid = r_hz.json()["id"]

    # 2. 守卫校验：未登记整改措施直接复查销项被拦（422）
    assert (
        client.post(
            f"/v1/ehs/hazards/{hid}/verify",
            json={"verified_note": "直接销项"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 422
    )

    # 登记整改 -> 复查销项 -> 重复销项被拦
    assert (
        client.post(
            f"/v1/ehs/hazards/{hid}/rectify",
            json={"rectification_plan": "更换耐高温密封垫并紧固法兰"},
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/ehs/hazards/{hid}/verify",
            json={"verified_note": "现场复查无漏风，粉尘浓度恢复正常"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/ehs/hazards/{hid}/verify",
            json={"verified_note": "重复销项"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 422
    )

    # 3. 超期隐患去重提醒
    r_ov = client.post(
        "/v1/ehs/hazards",
        json={
            "location": "配电室",
            "category": "electrical",
            "level": "major",
            "description": "配电柜接地线松动",
            "due_date": overdue_day,
        },
        headers=_h(keys, "engineer"),
    )
    assert r_ov.status_code == 201
    rem1 = client.post("/v1/ehs/hazards/remind", headers=_h(keys, "engineer"))
    assert rem1.status_code == 200 and rem1.json()["created"] == 1
    rem2 = client.post("/v1/ehs/hazards/remind", headers=_h(keys, "engineer"))
    assert rem2.status_code == 200 and rem2.json()["created"] == 0

    # 4. 特种作业票：超过 72h 被守卫拦截
    t0 = datetime.now(UTC).replace(microsecond=0)
    assert (
        client.post(
            "/v1/ehs/permits",
            json={
                "kind": "high_altitude",
                "location": "窑顶平台",
                "description": "检修烟道阀",
                "worker_name": "张三",
                "safety_measures": "佩戴双钩安全带、设监护人",
                "valid_from": t0.isoformat(),
                "valid_until": (t0 + timedelta(hours=80)).isoformat(),
            },
            headers=_h(keys, "engineer"),
        ).status_code
        == 422
    )

    # 普通高处作业票直接签发并完工关闭
    r_pt = client.post(
        "/v1/ehs/permits",
        json={
            "kind": "high_altitude",
            "location": "窑顶平台",
            "description": "检修烟道阀",
            "worker_name": "张三",
            "safety_measures": "佩戴双钩安全带、设监护人",
            "valid_from": t0.isoformat(),
            "valid_until": (t0 + timedelta(hours=4)).isoformat(),
        },
        headers=_h(keys, "engineer"),
    )
    assert r_pt.status_code == 201, r_pt.text
    pt_id = r_pt.json()["id"]
    assert (
        client.post(
            f"/v1/ehs/permits/{pt_id}/close",
            json={"status": "closed", "closed_note": "完工清场，无遗留火种与工具"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/ehs/permits/{pt_id}/close",
            json={"status": "closed", "closed_note": "重复关闭"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 422
    )

    # 动火作业票触发总经理审批（202）-> 总经理批准 -> 作业票生效
    r_hot = client.post(
        "/v1/ehs/permits",
        json={
            "kind": "hot_work",
            "location": "2# 隧道窑燃气支管",
            "description": "支架补焊",
            "worker_name": "李四",
            "safety_measures": "气体检测合格、配 2 具干粉灭火器、专人看火",
            "valid_from": t0.isoformat(),
            "valid_until": (t0 + timedelta(hours=4)).isoformat(),
        },
        headers=_h(keys, "engineer"),
    )
    assert r_hot.status_code == 202, r_hot.text
    apr_id = r_hot.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{apr_id}/decide",
        json={"decision": "approve", "note": "现场安全措施到位，同意动火"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text
    permits = client.get("/v1/ehs/permits", headers=_h(keys, "engineer")).json()
    assert permits["counts"]["high_risk"] == 1 and permits["counts"]["active"] == 1

    # 5. 安全事故上报与闭环
    r_inc = client.post(
        "/v1/ehs/incidents",
        json={
            "kind": "near_miss",
            "location": "原料库",
            "description": "叉车转弯视线盲区险些碰撞托盘",
            "injured_count": 0,
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_inc.status_code == 201, r_inc.text
    inc_id = r_inc.json()["id"]
    assert (
        client.post(
            f"/v1/ehs/incidents/{inc_id}/close",
            json={
                "root_cause": "通道转角未安装广角凸面镜且限速标识模糊",
                "corrective_action": "加装凸面镜并重刷限速 5km/h 地面标线",
            },
            headers=_h(keys, "engineer"),
        ).status_code
        == 200
    )

    # 6. 环保排放监测：达标不建隐患；超标自动开启重大隐患与待办
    r_ok = client.post(
        "/v1/ehs/env",
        json={"point_name": "1# 烟囱排口", "kind": "dust", "value": 12.5},
        headers=_h(keys, "engineer"),
    )
    assert r_ok.status_code == 201 and r_ok.json()["evaluation"]["passed"] is True
    assert r_ok.json()["auto_hazard_id"] is None

    r_bad = client.post(
        "/v1/ehs/env",
        json={"point_name": "2# 除尘排口", "kind": "dust", "value": 35.0, "note": "布袋破损"},
        headers=_h(keys, "engineer"),
    )
    assert r_bad.status_code == 201 and r_bad.json()["evaluation"]["passed"] is False
    assert r_bad.json()["auto_hazard_id"] is not None

    # 7. 合规总览与四表 Excel 导出
    summary = client.get("/v1/ehs/summary", headers=_h(keys, "engineer")).json()
    assert summary["kpis"]["env_exceeded"] == 1 and summary["kpis"]["hazards_major_open"] == 2
    xls = client.get("/v1/ehs/report.xlsx", headers=_h(keys, "engineer"))
    assert xls.status_code == 200
    wb = load_workbook(io.BytesIO(xls.content))
    assert wb.sheetnames == ["EHS合规汇总", "安全隐患台账", "特种作业票", "环保监测记录"]


def test_ehs_im_fast_path(client, keys):
    me = client.get("/v1/me", headers=_h(keys, "engineer")).json()
    assert (
        client.post(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "ehs_eng_1", "user_id": me["subject_id"]},
            headers=_h(keys, "admin"),
        ).status_code
        == 201
    )

    cases = [
        ("隐患", "ehs_hazards", "安全隐患"),
        ("作业票", "ehs_permits", "特种作业"),
        ("环保", "ehs_env", "环保"),
    ]
    for idx, (word, expected_route, title_kw) in enumerate(cases):
        r = client.post(
            "/v1/channels/mock/inbound",
            json={
                "message_id": f"ehs_fp_{idx}",
                "sender_id": "ehs_eng_1",
                "conversation_id": "c_ehs",
                "conversation_type": "dm",
                "text": word,
            },
            headers=_h(keys, "admin"),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["route"] == expected_route
        assert title_kw in data["replies"][0]["card"]["title"]
