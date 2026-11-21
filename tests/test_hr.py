"""W26 人力排班与薪酬端到端测试：花名册、特种资质复审、考勤计件、请假审批、确定性薪酬与 IM Fast Path。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from fos.hr.config import HrConfig
from fos.hr.payroll import compute_payroll


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_payroll_deterministic_math():
    cfg = HrConfig(standard_work_days=20, overtime_multiplier=1.5, default_piece_rate=10.0)
    res = compute_payroll(
        cfg,
        "2026-11",
        employees=[
            {"id": "E001", "emp_no": "E001", "name": "王师傅", "department": "成形", "base_salary": 4000.0},
            {"id": "E002", "emp_no": "E002", "name": "离职员", "status": "resigned", "base_salary": 3000.0},
        ],
        rosters=[
            {
                "employee": "E001",
                "work_date": "2026-11-01",
                "attendance": "present",
                "hours": 8.0,
                "overtime_hours": 2.0,
                "piece_qty": 15.0,
                "piece_rate": 12.0,
            },
            {
                "employee": "E001",
                "work_date": "2026-11-02",
                "attendance": "present",
                "hours": 8.0,
                "overtime_hours": 0.0,
                "piece_qty": 10.0,
                "piece_rate": None,
            },
            {"employee": "E001", "work_date": "2026-10-30", "attendance": "present", "hours": 8.0},
        ],
    )
    # E001: 2/20 * 4000 = 400; hourly = 4000/160 = 25; ot = 2 * 25 * 1.5 = 75; piece = 15*12 + 10*10 = 280
    assert res["totals"]["headcount"] == 1
    it = res["items"][0]
    assert it["present_days"] == 2 and it["base_pay"] == 400.0
    assert it["overtime_pay"] == 75.0 and it["piece_pay"] == 280.0 and it["gross_pay"] == 755.0

    with pytest.raises(ValueError, match="有限数字"):
        compute_payroll(
            cfg,
            "2026-11",
            employees=[{"id": "E1", "base_salary": float("nan")}],
            rosters=[],
        )


def test_hr_end_to_end_flow(client, keys):
    today = date.today()
    month = today.strftime("%Y-%m")
    exp_soon = (today + timedelta(days=10)).isoformat()
    issued = (today - timedelta(days=300)).isoformat()

    # 1. 权限校验：销售无权建档；班组长/财务可建档与更新
    assert (
        client.post(
            "/v1/hr/employees",
            json={
                "emp_no": "NOPE",
                "name": "无权",
                "department": "一车间",
                "position": "操作工",
                "hire_date": issued,
            },
            headers=_h(keys, "sales"),
        ).status_code
        == 403
    )

    r_emp = client.post(
        "/v1/hr/employees",
        json={
            "emp_no": "EMP-101",
            "name": "张建国",
            "department": "烧成车间",
            "position": "司炉工",
            "shift_group": "A",
            "base_salary": 4400.0,
            "hire_date": issued,
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_emp.status_code == 201, r_emp.text
    assert (
        client.patch(
            "/v1/hr/employees/EMP-101",
            json={"position": "高级司炉工"},
            headers=_h(keys, "finance"),
        ).status_code
        == 200
    )

    # 2. 特种资质：到期早于发证被守卫拦；登记临期证照 -> 去重提醒 -> 换证
    assert (
        client.post(
            "/v1/hr/certs",
            json={
                "cert_no": "BAD-01",
                "employee": "EMP-101",
                "kind": "kiln_operator",
                "issued_date": today.isoformat(),
                "expires_date": issued,
            },
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 422
    )

    r_cert = client.post(
        "/v1/hr/certs",
        json={
            "cert_no": "KILN-2026-01",
            "employee": "EMP-101",
            "kind": "kiln_operator",
            "issued_date": issued,
            "expires_date": exp_soon,
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_cert.status_code == 201, r_cert.text
    cid = r_cert.json()["id"]

    rem1 = client.post("/v1/hr/certs/remind", headers=_h(keys, "shift_lead"))
    assert rem1.status_code == 200 and rem1.json()["created"] == 1
    rem2 = client.post("/v1/hr/certs/remind", headers=_h(keys, "shift_lead"))
    assert rem2.status_code == 200 and rem2.json()["created"] == 0

    new_exp = (today + timedelta(days=365)).isoformat()
    assert (
        client.post(
            f"/v1/hr/certs/{cid}/renew",
            json={"expires_date": new_exp, "note": "复审合格"},
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 200
    )

    # 3. 排班与计件考勤：单班超 16h 被守卫拦截；正常出勤入账
    assert (
        client.post(
            "/v1/hr/rosters",
            json={
                "work_date": today.isoformat(),
                "shift_name": "day",
                "employee": "EMP-101",
                "attendance": "present",
                "hours": 18.0,
            },
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 422
    )
    r_rst = client.post(
        "/v1/hr/rosters",
        json={
            "work_date": today.isoformat(),
            "shift_name": "day",
            "employee": "EMP-101",
            "attendance": "present",
            "hours": 8.0,
            "overtime_hours": 2.0,
            "piece_qty": 20.0,
            "piece_rate": 15.0,
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_rst.status_code == 201, r_rst.text

    # 4. 请假：1 天直接批准并支持撤销；3 天及以上触发总经理审批（202）
    r_lv1 = client.post(
        "/v1/hr/leaves",
        json={
            "employee": "EMP-101",
            "kind": "personal",
            "start_date": today.isoformat(),
            "end_date": today.isoformat(),
            "days": 1.0,
            "reason": "家事处理",
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_lv1.status_code == 201, r_lv1.text
    lid1 = r_lv1.json()["id"]
    assert client.post(f"/v1/hr/leaves/{lid1}/cancel", headers=_h(keys, "shift_lead")).status_code == 200
    assert client.post(f"/v1/hr/leaves/{lid1}/cancel", headers=_h(keys, "shift_lead")).status_code == 422

    r_lv3 = client.post(
        "/v1/hr/leaves",
        json={
            "employee": "EMP-101",
            "kind": "annual",
            "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=3)).isoformat(),
            "days": 4.0,
            "reason": "探亲年假",
        },
        headers=_h(keys, "shift_lead"),
    )
    assert r_lv3.status_code == 202, r_lv3.text
    apr_id = r_lv3.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{apr_id}/decide",
        json={"decision": "approve", "comment": "已安排替班，同意休假"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text

    # 5. 月度确定性薪酬核算与四表 Excel 导出
    pay = client.get(f"/v1/hr/payroll?month={month}", headers=_h(keys, "finance")).json()
    assert pay["totals"]["headcount"] == 1
    # base = 4400/22 = 200; hourly = 4400/(22*8) = 25; ot = 2*25*1.5 = 75; piece = 20*15 = 300 -> 575
    assert pay["items"][0]["gross_pay"] == 575.0

    xls = client.get(f"/v1/hr/report.xlsx?month={month}", headers=_h(keys, "finance"))
    assert xls.status_code == 200
    wb = load_workbook(io.BytesIO(xls.content))
    assert wb.sheetnames == ["员工花名册", "特种资质台账", "排班考勤与计件", "月度薪酬核算"]


def test_hr_im_fast_path(client, keys):
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "hr_lead_1", "login": "shift_lead"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )

    cases = [
        ("排班", "hr_roster", "排班"),
        ("资质", "hr_certs", "特种作业资质"),
        ("工资", "hr_payroll", "薪酬"),
    ]
    for idx, (word, expected_route, title_kw) in enumerate(cases):
        r = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": f"hr_fp_{idx}", "sender_id": "hr_lead_1", "text": word},
            headers=_h(keys, "admin"),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["route"] == expected_route and data["status"] == "done"
        assert title_kw in data["replies"][0]["card"]["title"]
