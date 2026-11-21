"""W24 设备管理（EAM）端到端测试：台账、保养计划、点检自动开单、工单闭环、A 类报废审批与可靠性 KPI。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from fos.eam.metrics import compute_reliability


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def _ev(client, keys, ev_type: str, object_id: str, payload: dict, who: str = "admin"):
    r = client.post(
        "/v1/events",
        json={
            "type": ev_type,
            "object_id": object_id,
            "payload": payload,
            "evidence": [{"kind": "manual", "ref": "test:eam"}],
        },
        headers=_h(keys, who),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_reliability_deterministic_math():
    m = compute_reliability(
        window_days=10,
        planned_hours_per_day=24.0,
        work_orders=[
            {"kind": "breakdown", "status": "completed", "downtime_minutes": 120},
            {"kind": "breakdown", "status": "completed", "downtime_minutes": 240},
            {"kind": "preventive", "status": "completed", "downtime_minutes": 60},
            {"kind": "breakdown", "status": "cancelled", "downtime_minutes": 999},
        ],
    )
    # 计划 14400 min；故障 2 次共停机 360 min -> 可用率 97.5%，MTTR 180 min，MTBF 117.0 h
    assert m.breakdown_count == 2 and m.total_downtime_minutes == 360.0
    assert m.availability_pct == 97.5 and m.mttr_minutes == 180.0 and m.mtbf_hours == 117.0

    with pytest.raises(ValueError, match="有限数字"):
        compute_reliability(
            window_days=10,
            planned_hours_per_day=24.0,
            work_orders=[{"kind": "breakdown", "status": "completed", "downtime_minutes": float("nan")}],
        )


def test_eam_end_to_end_flow(client, keys):
    today = date.today()
    overdue_day = (today - timedelta(days=3)).isoformat()

    # 1. 权限校验：销售无权登记设备；工程师/班组长可登记 A 类设备
    assert (
        client.post(
            "/v1/eam/equipment",
            json={"code": "NOPE", "name": "无权设备"},
            headers=_h(keys, "sales"),
        ).status_code
        == 403
    )

    eq_r = client.post(
        "/v1/eam/equipment",
        json={
            "code": "PRS-01",
            "name": "1号液压压砖机",
            "category": "press",
            "model": "YP-630",
            "location": "成型一车间",
            "criticality": "A",
            "pm_cycle_days": 30,
        },
        headers=_h(keys, "engineer"),
    )
    assert eq_r.status_code == 201, eq_r.text
    eq_id = eq_r.json()["id"]
    assert eq_id == "EQ-PRS-01"

    # 2. 创建已超期保养计划 → 提醒去重 → 倒挂日期被守卫拦 → 正常完工顺延日期
    pm_r = client.post(
        "/v1/eam/pm",
        json={
            "equipment": eq_id,
            "title": "液压油滤芯更换与主缸巡检",
            "cycle_days": 30,
            "next_due_date": overdue_day,
        },
        headers=_h(keys, "engineer"),
    )
    assert pm_r.status_code == 201, pm_r.text
    pm_id = pm_r.json()["id"]

    rem1 = client.post("/v1/eam/pm/remind", headers=_h(keys, "engineer")).json()
    rem2 = client.post("/v1/eam/pm/remind", headers=_h(keys, "engineer")).json()
    assert rem1["count"] == 1 and rem2["count"] == 0

    bad_pm = client.post(
        f"/v1/eam/pm/{pm_id}/complete",
        json={"done_date": today.isoformat(), "next_due_date": overdue_day},
        headers=_h(keys, "shift_lead"),
    )
    assert bad_pm.status_code == 422

    ok_pm = client.post(
        f"/v1/eam/pm/{pm_id}/complete",
        json={"done_date": today.isoformat(), "note": "已更换滤芯并试压正常"},
        headers=_h(keys, "shift_lead"),
    )
    assert ok_pm.status_code == 200, ok_pm.text
    assert client.get("/v1/eam/pm", headers=_h(keys, "engineer")).json()["counts"]["overdue"] == 0

    # 3. 工人日常点检异常 → 自动开启维修工单
    insp_r = client.post(
        "/v1/eam/inspections",
        json={
            "equipment": eq_id,
            "result": "abnormal",
            "items": [{"name": "主缸压力", "ok": False}],
            "note": "主缸回程异响且油温偏高",
        },
        headers=_h(keys, "worker"),
    )
    assert insp_r.status_code == 201, insp_r.text
    assert insp_r.json()["work_order"] is not None

    # 4. 开立紧急故障工单 → 设备自动转 maintenance → 开工 → 完工恢复 running → 重复完工被拒
    wo_r = client.post(
        "/v1/eam/work-orders",
        json={
            "equipment": eq_id,
            "kind": "breakdown",
            "priority": "urgent",
            "symptom": "高压油管渗漏停机",
        },
        headers=_h(keys, "shift_lead"),
    )
    assert wo_r.status_code == 201, wo_r.text
    wo_id = wo_r.json()["id"]

    eq_b1 = client.get("/v1/eam/equipment", headers=_h(keys, "engineer")).json()
    assert eq_b1["counts"]["maintenance"] == 1

    assert (
        client.post(f"/v1/eam/work-orders/{wo_id}/start", json={}, headers=_h(keys, "shift_lead")).status_code
        == 200
    )
    done_r = client.post(
        f"/v1/eam/work-orders/{wo_id}/complete",
        json={
            "root_cause": "高压软管接头老化",
            "action_taken": "更换高压软管总成并补油试机",
            "spare_parts": [{"material": "SP-HOSE-1", "name": "高压软管", "qty": 1, "unit": "根"}],
            "downtime_minutes": 90,
        },
        headers=_h(keys, "shift_lead"),
    )
    assert done_r.status_code == 200, done_r.text

    # 重复完工被 EamGuard 拦截
    assert (
        client.post(
            f"/v1/eam/work-orders/{wo_id}/complete",
            json={"action_taken": "重复提交"},
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 422
    )
    eq_b2 = client.get("/v1/eam/equipment", headers=_h(keys, "engineer")).json()
    assert eq_b2["counts"]["running"] == 1

    # 5. 备件库存联动（仓储 spare_part 物料 + 低库存预警）
    _ev(
        client,
        keys,
        "warehouse.material_defined",
        "SP-HOSE-1",
        {"name": "高压软管总成", "category": "spare_part", "unit": "根", "safety_stock": 5},
    )
    _ev(
        client,
        keys,
        "warehouse.lot_received",
        "LOT-SP-1",
        {
            "material": "SP-HOSE-1",
            "qty": 2,
            "unit": "根",
            "received_at": "2026-11-20T08:00:00+08:00",
            "status": "accepted",
        },
    )
    sp = client.get("/v1/eam/spare-parts", headers=_h(keys, "engineer")).json()
    assert sp["counts"]["total"] == 1 and sp["counts"]["low_stock"] == 1

    # 6. 可靠性指标与三表 Excel 导出
    rel = client.get("/v1/eam/reliability", headers=_h(keys, "engineer")).json()
    assert rel["metrics"]["breakdown_count"] == 1 and rel["metrics"]["total_downtime_minutes"] == 90.0

    exp = client.get("/v1/eam/report/export", headers=_h(keys, "engineer"))
    assert exp.status_code == 200 and exp.content.startswith(b"PK")
    wb = load_workbook(io.BytesIO(exp.content))
    assert wb.sheetnames == ["设备台账", "维修工单", "可靠性KPI"]

    # 7. A 类关键设备报废命中 policy.yaml → 返回 202 → 总经理审批通过后状态变 retired
    ret_r = client.post(
        f"/v1/eam/equipment/{eq_id}/retire",
        json={"note": "主机架变形无法修复，申请报废"},
        headers=_h(keys, "engineer"),
    )
    assert ret_r.status_code == 202, ret_r.text
    ap_id = ret_r.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{ap_id}/decide",
        json={"decision": "approve", "comment": "同意报废"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text
    assert client.get("/v1/eam/equipment", headers=_h(keys, "engineer")).json()["counts"]["retired"] == 1

    # 8. IM Fast Path：设备 / 保养 / 工单 / 备件
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "im-eng", "login": "engineer"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )
    for idx, text, route in (
        ("e1", "设备", "eam_equipment"),
        ("e2", "保养", "eam_pm"),
        ("e3", "工单", "eam_work_orders"),
        ("e4", "备件", "eam_spare_parts"),
    ):
        resp = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": idx, "sender_id": "im-eng", "text": text},
            headers=_h(keys, "admin"),
        ).json()
        assert resp["route"] == route and resp["status"] == "done"
