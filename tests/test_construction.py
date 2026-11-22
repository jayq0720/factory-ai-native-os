"""W27b 筑炉施工与烘炉验收端到端测试：进度重算、烘炉曲线告警、验收门禁与条件验收审批、导出与 IM Fast Path。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from fos.construction.config import ConstructionConfig
from fos.construction.metrics import compute_site_progress, evaluate_dryout


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_construction_deterministic_math():
    prog = compute_site_progress(100.0, [{"tons_installed": 40.0}, {"tons_installed": 56.5}])
    assert prog["completed_tons"] == 96.5 and prog["progress_pct"] == 96.5

    cfg = ConstructionConfig(max_temp_deviation_c=25.0, max_ramp_rate_c_per_h=35.0)
    ok = evaluate_dryout(cfg, target_temp_c=300.0, actual_temp_c=310.0, ramp_rate_c_per_h=20.0)
    assert ok["within_spec"] is True and ok["deviation_c"] == 10.0

    bad = evaluate_dryout(cfg, target_temp_c=300.0, actual_temp_c=340.0, ramp_rate_c_per_h=45.0)
    assert bad["within_spec"] is False and len(bad["reasons"]) == 2

    with pytest.raises(ValueError, match="有限数字"):
        compute_site_progress(float("nan"), [])


def test_construction_end_to_end_flow(client, keys):
    today = date.today()
    due = (today + timedelta(days=20)).isoformat()

    # 1. 权限校验：销售无权立项；工程师可立项
    assert (
        client.post(
            "/v1/construction/sites",
            json={
                "name": "无权",
                "customer_name": "X",
                "furnace_type": "回转窑",
                "location": "合肥",
                "supervisor": "张",
                "planned_tons": 50.0,
                "start_date": today.isoformat(),
                "due_date": due,
            },
            headers=_h(keys, "sales"),
        ).status_code
        == 403
    )

    r_s1 = client.post(
        "/v1/construction/sites",
        json={
            "name": "海螺水泥 3# 回转窑大修砌筑",
            "customer_name": "海螺水泥",
            "furnace_type": "回转窑",
            "location": "芜湖",
            "supervisor": "王工",
            "planned_tons": 100.0,
            "start_date": today.isoformat(),
            "due_date": due,
        },
        headers=_h(keys, "engineer"),
    )
    assert r_s1.status_code == 201, r_s1.text
    sid1 = r_s1.json()["id"]

    # 2. 日报登记自动重算进度；进度不足 95% 时正常验收被守卫拦（422）
    r_lg1 = client.post(
        "/v1/construction/logs",
        json={
            "site": sid1,
            "log_date": today.isoformat(),
            "crew_size": 12,
            "tons_installed": 45.0,
            "weather": "晴",
            "safety_ok": True,
            "summary": "烧成带镁铬砖砌筑 45 吨",
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_lg1.status_code == 201 and r_lg1.json()["progress"]["progress_pct"] == 45.0

    assert (
        client.post(
            f"/v1/construction/sites/{sid1}/accept",
            json={"acceptance_type": "normal", "handover_note": "进度未满强行验收"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 422
    )

    # 3. 烘炉升温记录：正常与超速告警（自动派发待办）
    assert (
        client.patch(
            f"/v1/construction/sites/{sid1}",
            json={"status": "drying"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 200
    )
    r_d1 = client.post(
        "/v1/construction/dryout",
        json={
            "site": sid1,
            "stage_name": "低温脱水段",
            "target_temp_c": 150.0,
            "actual_temp_c": 155.0,
            "ramp_rate_c_per_h": 15.0,
        },
        headers=_h(keys, "engineer"),
    )
    assert r_d1.status_code == 201 and r_d1.json()["evaluation"]["within_spec"] is True

    r_d2 = client.post(
        "/v1/construction/dryout",
        json={
            "site": sid1,
            "stage_name": "中温升温段",
            "target_temp_c": 350.0,
            "actual_temp_c": 395.0,
            "ramp_rate_c_per_h": 42.0,
            "note": "燃气阀开度偏大",
        },
        headers=_h(keys, "engineer"),
    )
    assert r_d2.status_code == 201 and r_d2.json()["evaluation"]["within_spec"] is False

    # 4. 补齐砌筑进度至 98% -> 正常竣工验收直接通过（200），重复验收被拦（422）
    r_lg2 = client.post(
        "/v1/construction/logs",
        json={
            "site": sid1,
            "log_date": today.isoformat(),
            "crew_size": 14,
            "tons_installed": 53.0,
            "safety_ok": True,
            "summary": "余量砌筑与锁砖完工",
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_lg2.status_code == 201 and r_lg2.json()["progress"]["progress_pct"] == 98.0
    assert (
        client.post(
            f"/v1/construction/sites/{sid1}/accept",
            json={"acceptance_type": "normal", "handover_note": "客户三方签字验收合格"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/construction/sites/{sid1}/accept",
            json={"acceptance_type": "normal", "handover_note": "重复验收"},
            headers=_h(keys, "engineer"),
        ).status_code
        == 422
    )

    # 5. 带尾项条件验收触发总经理审批（202）-> GM 批准
    r_s2 = client.post(
        "/v1/construction/sites",
        json={
            "name": "马钢加热炉修补",
            "customer_name": "马钢",
            "furnace_type": "加热炉",
            "location": "马鞍山",
            "supervisor": "赵工",
            "planned_tons": 30.0,
            "start_date": today.isoformat(),
            "due_date": due,
        },
        headers=_h(keys, "engineer"),
    )
    sid2 = r_s2.json()["id"]
    r_cond = client.post(
        f"/v1/construction/sites/{sid2}/accept",
        json={"acceptance_type": "conditional", "handover_note": "炉门保温棉尾项 3 日内补齐"},
        headers=_h(keys, "engineer"),
    )
    assert r_cond.status_code == 202, r_cond.text
    apr_id = r_cond.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{apr_id}/decide",
        json={"decision": "approve", "comment": "同意带尾项先投产，限期闭环"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text

    # 6. 三表 Excel 导出
    xls = client.get("/v1/construction/report.xlsx", headers=_h(keys, "engineer"))
    assert xls.status_code == 200
    wb = load_workbook(io.BytesIO(xls.content))
    assert wb.sheetnames == ["筑炉施工项目", "施工日报台账", "烘炉升温记录"]


def test_construction_im_fast_path(client, keys):
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "site_eng_1", "login": "engineer"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )

    cases = [
        ("施工", "construction_sites", "筑炉施工项目"),
        ("施工日报", "construction_logs", "施工日报"),
        ("烘炉", "construction_dryout", "烘炉升温"),
    ]
    for idx, (word, expected_route, title_kw) in enumerate(cases):
        r = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": f"cst_fp_{idx}", "sender_id": "site_eng_1", "text": word},
            headers=_h(keys, "admin"),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["route"] == expected_route and data["status"] == "done"
        assert title_kw in data["replies"][0]["card"]["title"]
