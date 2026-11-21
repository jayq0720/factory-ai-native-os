"""W27 研发配方试验端到端测试：确定性配比/成本核算、试验达标门禁、总经理定型审批、导出与 IM Fast Path。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from fos.rnd.formulation import evaluate_formula, evaluate_trial


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_formulation_deterministic_math():
    res = evaluate_formula(
        [
            {"name": "特级高铝矾土熟料", "pct": 70.0, "al2o3_pct": 85.0, "unit_cost": 2200.0},
            {"name": "棕刚玉细粉", "pct": 20.0, "al2o3_pct": 95.0, "unit_cost": 4500.0},
            {"name": "结合粘土", "pct": 10.0, "al2o3_pct": 35.0, "unit_cost": 800.0},
        ]
    )
    # Al2O3 = 0.7*85 + 0.2*95 + 0.1*35 = 59.5 + 19 + 3.5 = 82.0
    # Cost = 0.7*2200 + 0.2*4500 + 0.1*800 = 1540 + 900 + 80 = 2520.0
    assert res["total_pct"] == 100.0 and res["calc_al2o3_pct"] == 82.0 and res["est_cost_per_ton"] == 2520.0

    with pytest.raises(ValueError, match="超出允许范围"):
        evaluate_formula([{"name": "矾土", "pct": 80.0, "al2o3_pct": 80.0, "unit_cost": 2000.0}])
    with pytest.raises(ValueError, match="有限数字"):
        evaluate_formula([{"name": "矾土", "pct": float("nan"), "al2o3_pct": 80.0, "unit_cost": 2000.0}])

    ok, reasons = evaluate_trial(
        {"target_al2o3_min": 75.0, "target_bd_min": 2.65, "target_ccs_min": 65.0},
        al2o3_pct=78.0,
        bulk_density=2.70,
        ccs_mpa=70.0,
    )
    assert ok is True and reasons == []

    bad, reasons2 = evaluate_trial(
        {"target_al2o3_min": 75.0, "target_bd_min": 2.65},
        al2o3_pct=72.0,
        bulk_density=2.60,
        ccs_mpa=None,
    )
    assert bad is False and len(reasons2) == 2


def test_rnd_end_to_end_flow(client, keys):
    today = date.today()
    due = (today + timedelta(days=30)).isoformat()

    # 1. 权限校验：销售无权立项；研发角色可立项与更新
    assert (
        client.post(
            "/v1/rnd/projects",
            json={"name": "无权", "target_product": "LZ-75", "owner_name": "X", "due_date": due},
            headers=_h(keys, "sales"),
        ).status_code
        == 403
    )

    r_proj = client.post(
        "/v1/rnd/projects",
        json={
            "name": "高抗剥落高铝砖降本配方",
            "target_product": "KBL-75",
            "target_al2o3_min": 75.0,
            "target_bd_min": 2.65,
            "target_ccs_min": 65.0,
            "owner_name": "陈总工",
            "due_date": due,
        },
        headers=_h(keys, "rnd"),
    )
    assert r_proj.status_code == 201, r_proj.text
    pid = r_proj.json()["id"]
    assert (
        client.patch(f"/v1/rnd/projects/{pid}", json={"status": "trial"}, headers=_h(keys, "rnd")).status_code
        == 200
    )

    # 2. 配方版本：配比总和不为 100% 被拦截（422）；合法配方自动核算 Al2O3 与吨成本
    assert (
        client.post(
            "/v1/rnd/formulas",
            json={
                "project": pid,
                "version_code": "V0",
                "ingredients": [{"name": "矾土", "pct": 90.0, "al2o3_pct": 80.0, "unit_cost": 2000.0}],
                "firing_temp_c": 1480.0,
            },
            headers=_h(keys, "rnd"),
        ).status_code
        == 422
    )

    r_frm = client.post(
        "/v1/rnd/formulas",
        json={
            "project": pid,
            "version_code": "V1.0",
            "ingredients": [
                {"name": "特级高铝矾土", "pct": 75.0, "al2o3_pct": 85.0, "unit_cost": 2200.0},
                {"name": "锆英石细粉", "pct": 15.0, "al2o3_pct": 0.0, "unit_cost": 9000.0},
                {"name": "结合粘土", "pct": 10.0, "al2o3_pct": 35.0, "unit_cost": 800.0},
            ],
            "firing_temp_c": 1520.0,
            "note": "首轮小试配方",
        },
        headers=_h(keys, "rnd"),
    )
    assert r_frm.status_code == 201, r_frm.text
    fid = r_frm.json()["id"]
    assert r_frm.json()["calculation"]["est_cost_per_ton"] == 3080.0

    # 3. 门禁校验：无合格试验前禁止定型放行（422）
    assert (
        client.post(
            f"/v1/rnd/formulas/{fid}/release",
            json={"note": "跳过试验直接放行"},
            headers=_h(keys, "rnd"),
        ).status_code
        == 422
    )

    # 登记一次不达标小试（Al2O3 70 < 75）-> passed=False -> 仍禁止放行
    r_t1 = client.post(
        "/v1/rnd/trials",
        json={
            "formula": fid,
            "stage": "lab",
            "trial_date": today.isoformat(),
            "al2o3_pct": 70.0,
            "bulk_density": 2.68,
            "ccs_mpa": 68.0,
            "conclusion": "铝含量偏低",
        },
        headers=_h(keys, "rnd"),
    )
    assert r_t1.status_code == 201 and r_t1.json()["passed"] is False
    assert (
        client.post(
            f"/v1/rnd/formulas/{fid}/release",
            json={"note": "不合格后强行放行"},
            headers=_h(keys, "rnd"),
        ).status_code
        == 422
    )

    # 登记一次合格中试 -> passed=True -> 定型放行触发总经理审批（202）
    r_t2 = client.post(
        "/v1/rnd/trials",
        json={
            "formula": fid,
            "stage": "pilot",
            "trial_date": today.isoformat(),
            "al2o3_pct": 76.8,
            "bulk_density": 2.72,
            "ccs_mpa": 74.0,
            "thermal_shock_cycles": 25,
            "conclusion": "各项理化指标均超目标下限",
        },
        headers=_h(keys, "rnd"),
    )
    assert r_t2.status_code == 201 and r_t2.json()["passed"] is True

    r_rel = client.post(
        f"/v1/rnd/formulas/{fid}/release",
        json={"note": "中试合格，申请定型转量产"},
        headers=_h(keys, "rnd"),
    )
    assert r_rel.status_code == 202, r_rel.text
    apr_id = r_rel.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{apr_id}/decide",
        json={"decision": "approve", "comment": "数据扎实，同意定型量产"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text

    # 已定型配方不能再作废（422）
    assert (
        client.post(
            f"/v1/rnd/formulas/{fid}/reject",
            json={"note": "误操作作废"},
            headers=_h(keys, "rnd"),
        ).status_code
        == 422
    )

    # 4. 三表 Excel 导出
    xls = client.get("/v1/rnd/report.xlsx", headers=_h(keys, "rnd"))
    assert xls.status_code == 200
    wb = load_workbook(io.BytesIO(xls.content))
    assert wb.sheetnames == ["研发项目台账", "配方版本与成本", "试验检测记录"]


def test_rnd_im_fast_path(client, keys):
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "rnd_eng_1", "login": "rnd"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )

    cases = [
        ("研发", "rnd_projects", "研发项目"),
        ("配方", "rnd_formulas", "配方版本"),
        ("试验", "rnd_trials", "试验检测"),
    ]
    for idx, (word, expected_route, title_kw) in enumerate(cases):
        r = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": f"rnd_fp_{idx}", "sender_id": "rnd_eng_1", "text": word},
            headers=_h(keys, "admin"),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["route"] == expected_route and data["status"] == "done"
        assert title_kw in data["replies"][0]["card"]["title"]
