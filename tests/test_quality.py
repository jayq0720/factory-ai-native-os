"""W22 质量体系端到端测试：判定、NCR/8D、让步审批、质保书、校准、ISO 包、IM。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest

from fos.quality.config import QualityConfig, SpecLimit
from fos.quality.judge import evaluate_measurements


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def _ev(client, keys, ev_type: str, object_id: str, payload: dict, who: str = "admin", mode: str = "real"):
    r = client.post(
        "/v1/events",
        json={
            "type": ev_type,
            "object_id": object_id,
            "payload": payload,
            "evidence": [{"kind": "manual", "ref": "test:quality"}],
            "data_mode": mode,
        },
        headers=_h(keys, who),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_judge_deterministic_math():
    cfg = QualityConfig(
        standards={
            "default": {
                "bulk_density": SpecLimit(name="体积密度", unit="g/cm³", min=0.4, max=1.0),
                "cold_crushing_strength": SpecLimit(name="常温耐压强度", unit="MPa", min=1.0),
                "thermal_conductivity": SpecLimit(name="导热系数", unit="W/(m·K)", max=0.30),
            }
        },
        density_tolerance_pct=10.0,
    )
    ok = evaluate_measurements(
        cfg,
        {"bulk_density": 0.82, "cold_crushing_strength": 1.8, "thermal_conductivity": 0.25},
        product_state={"bulk_density": 0.8},
    )
    assert ok.passed is True and ok.result == "pass" and len(ok.checks) == 3

    bad = evaluate_measurements(
        cfg,
        {"bulk_density": 0.95, "cold_crushing_strength": 0.6, "thermal_conductivity": 0.4},
        product_state={"bulk_density": 0.8},
    )
    assert bad.passed is False and bad.result == "fail" and len(bad.failed_items) == 3

    with pytest.raises(ValueError, match="有限数字"):
        evaluate_measurements(cfg, {"bulk_density": float("nan")})


def test_quality_end_to_end_flow(client, keys):
    # 准备产品、原料、来料批次、生产批次
    _ev(
        client,
        keys,
        "sales.product_defined",
        "P-B1",
        {"code": "LN-1260B", "name": "轻质隔热砖", "unit": "块"},
    )
    _ev(
        client,
        keys,
        "sales.product_specified",
        "P-B1",
        {"category": "insulating_brick", "classification_temp": 1260, "bulk_density": 0.8},
    )
    _ev(
        client,
        keys,
        "warehouse.material_defined",
        "M-Q1",
        {"name": "莫来石粉", "category": "raw", "unit": "吨"},
    )
    _ev(
        client,
        keys,
        "warehouse.lot_received",
        "LOT-Q1",
        {
            "material": "M-Q1",
            "qty": 5,
            "unit": "吨",
            "received_at": "2026-11-18T08:00:00+08:00",
            "status": "pending_inspection",
        },
    )
    _ev(
        client,
        keys,
        "production.batch_started",
        "B-Q1",
        {
            "product": "P-B1",
            "planned_qty": 1000,
            "unit": "块",
            "started_at": "2026-11-18T09:00:00+08:00",
            "status": "completed",
        },
    )
    _ev(
        client,
        keys,
        "production.batch_started",
        "B-Q2",
        {
            "product": "P-B1",
            "planned_qty": 500,
            "unit": "块",
            "started_at": "2026-11-18T10:00:00+08:00",
            "status": "completed",
        },
    )

    # 1. 待检任务队列：1 个待检来料 + 2 个待出厂检批次
    pend = client.get("/v1/quality/pending", headers=_h(keys, "qc")).json()
    assert len(pend["incoming"]) == 1 and len(pend["final"]) == 2
    assert client.get("/v1/quality/pending", headers=_h(keys, "worker")).status_code == 403

    # 2. 检验超标不允许强行标合格
    forced = client.post(
        "/v1/quality/inspections",
        json={
            "kind": "final",
            "batch": "B-Q1",
            "cold_crushing_strength": 0.4,
            "result": "pass",
        },
        headers=_h(keys, "qc"),
    )
    assert forced.status_code == 422

    # 3. 出厂检验不合格 → 自动判定 fail + 自动开启 major NCR
    fail_r = client.post(
        "/v1/quality/inspections",
        json={
            "kind": "final",
            "batch": "B-Q1",
            "bulk_density": 0.82,
            "cold_crushing_strength": 0.5,
            "thermal_conductivity": 0.28,
            "severity": "major",
        },
        headers=_h(keys, "qc"),
    )
    assert fail_r.status_code == 201, fail_r.text
    fail_j = fail_r.json()
    assert fail_j["result"] == "fail" and fail_j["ncr"] is not None
    ncr_id = fail_j["ncr"]["id"]

    # 未合格批次不能签发质保书
    assert client.get("/v1/quality/batches/B-Q1/certificate", headers=_h(keys, "qc")).status_code == 409

    # 4. 8D 追溯草稿（L1）
    d8 = client.get(f"/v1/quality/ncrs/{ncr_id}/8d-draft", headers=_h(keys, "qc"))
    assert d8.status_code == 200 and d8.json()["ncr_id"] == ncr_id

    # 5. 未处置 & 未完成 8D 时直接关闭 NCR 被账本守卫拦截
    assert (
        client.post(
            f"/v1/quality/ncrs/{ncr_id}/close",
            json={"verification": "尝试跳过关闭"},
            headers=_h(keys, "qc"),
        ).status_code
        == 422
    )

    # 6. 让步放行命中 policy.yaml → 返回 202，需 gm 审批
    disp = client.post(
        f"/v1/quality/ncrs/{ncr_id}/disposition",
        json={"disposition": "concession", "disposition_note": "客户同意降级使用"},
        headers=_h(keys, "qc"),
    )
    assert disp.status_code == 202, disp.text
    ap_id = disp.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{ap_id}/decide",
        json={"decision": "approve", "comment": "同意让步"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text

    # 7. 补齐 8D 整改后成功关闭 NCR；关闭后不能再修改
    assert (
        client.patch(
            f"/v1/quality/ncrs/{ncr_id}/8d",
            json={
                "containment": "隔离同窑次 1000 块",
                "root_cause": "烧成保温时间不足 20 分钟",
                "corrective_action": "延长保温段并复检耐压强度",
                "preventive_action": "曲线偏差超 15 分钟自动锁窑次",
                "d8_status": "completed",
            },
            headers=_h(keys, "qc"),
        ).status_code
        == 200
    )
    closed = client.post(
        f"/v1/quality/ncrs/{ncr_id}/close",
        json={"verification": "复检三批强度均 ≥ 1.4 MPa"},
        headers=_h(keys, "qc"),
    )
    assert closed.status_code == 200, closed.text
    assert (
        client.patch(
            f"/v1/quality/ncrs/{ncr_id}/8d",
            json={"containment": "篡改已关闭记录"},
            headers=_h(keys, "qc"),
        ).status_code
        == 409
    )

    # 8. B-Q2 出厂检验合格 → 生成质保书（JSON + Word + Excel）
    ok_r = client.post(
        "/v1/quality/inspections",
        json={
            "kind": "final",
            "batch": "B-Q2",
            "bulk_density": 0.81,
            "cold_crushing_strength": 1.6,
            "thermal_conductivity": 0.24,
        },
        headers=_h(keys, "qc"),
    )
    assert ok_r.status_code == 201 and ok_r.json()["result"] == "pass" and ok_r.json()["ncr"] is None
    coa = client.get("/v1/quality/batches/B-Q2/certificate", headers=_h(keys, "qc")).json()
    assert coa["certificate_no"] == "COA-B-Q2" and len(coa["checks"]) == 3

    docx_r = client.get("/v1/quality/batches/B-Q2/certificate/export?format=docx", headers=_h(keys, "qc"))
    assert docx_r.status_code == 200 and docx_r.content.startswith(b"PK")
    xlsx_r = client.get("/v1/quality/batches/B-Q2/certificate/export?format=xlsx", headers=_h(keys, "qc"))
    assert xlsx_r.status_code == 200 and xlsx_r.content.startswith(b"PK")

    # 模拟批次不能签发质保书
    _ev(
        client,
        keys,
        "production.batch_started",
        "B-MOCK",
        {
            "product": "P-B1",
            "planned_qty": 100,
            "unit": "块",
            "started_at": "2026-11-18T11:00:00+08:00",
            "status": "completed",
        },
        mode="mock",
    )
    assert client.get("/v1/quality/batches/B-MOCK/certificate", headers=_h(keys, "qc")).status_code == 409


def test_calibration_iso_and_im_fast_path(client, keys):
    today = date.today()
    overdue_date = (today - timedelta(days=5)).isoformat()
    ok_date = (today + timedelta(days=120)).isoformat()

    # 1. 登记两台计量器具（1 台已超期，1 台正常）
    r1 = client.post(
        "/v1/quality/instruments",
        json={
            "code": "TC-01",
            "name": "1号窑热电偶",
            "category": "thermocouple",
            "location": "1号梭式窑",
            "next_due_date": overdue_date,
        },
        headers=_h(keys, "qc"),
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/v1/quality/instruments",
        json={
            "code": "BAL-01",
            "name": "化验室电子天平",
            "category": "balance",
            "next_due_date": ok_date,
        },
        headers=_h(keys, "qc"),
    )
    assert r2.status_code == 201, r2.text

    board = client.get("/v1/quality/instruments", headers=_h(keys, "qc")).json()
    assert board["counts"]["overdue"] == 1 and board["counts"]["ok"] == 1

    # 校准到期提醒去重
    rem1 = client.post("/v1/quality/instruments/remind", headers=_h(keys, "qc")).json()
    rem2 = client.post("/v1/quality/instruments/remind", headers=_h(keys, "qc")).json()
    assert rem1["count"] == 1 and rem2["count"] == 0

    # 校准日期倒挂被 CalibrationGuard 拦截
    bad_cal = client.post(
        "/v1/quality/instruments/MI-TC-01/calibrate",
        json={
            "last_calibrated_on": today.isoformat(),
            "next_due_date": overdue_date,
            "certificate_no": "CAL-BAD",
        },
        headers=_h(keys, "qc"),
    )
    assert bad_cal.status_code == 422

    # 正常校准后恢复 ok
    good_cal = client.post(
        "/v1/quality/instruments/MI-TC-01/calibrate",
        json={
            "last_calibrated_on": today.isoformat(),
            "certificate_no": "CAL-2026-001",
            "agency": "省计量院",
        },
        headers=_h(keys, "qc"),
    )
    assert good_cal.status_code == 200, good_cal.text
    board2 = client.get("/v1/quality/instruments", headers=_h(keys, "qc")).json()
    assert board2["counts"]["overdue"] == 0 and board2["counts"]["ok"] == 2

    # 2. ISO 9001 审核证据包（JSON + Excel）
    iso = client.get("/v1/quality/iso-audit", headers=_h(keys, "qc")).json()
    assert iso["kpis"]["instruments_active"] == 2 and iso["kpis"]["calibration_on_time_rate"] == 100.0
    iso_x = client.get("/v1/quality/iso-audit/export", headers=_h(keys, "qc"))
    assert iso_x.status_code == 200 and iso_x.content.startswith(b"PK")
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(iso_x.content))
    assert wb.sheetnames == ["质量KPI汇总", "不合格品NCR", "计量器具校准", "账本证据索引"]

    # 3. 跨租户隔离：other_admin 看不到 luneng 的计量器具
    other_board = client.get("/v1/quality/instruments", headers=_h(keys, "other_admin")).json()
    assert other_board["counts"]["total"] == 0

    # 4. IM Fast Path：待检 / 不合格 / 校准
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "im-qc", "login": "qc"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )
    for idx, text, route in (
        ("q1", "待检", "quality_pending"),
        ("q2", "NCR", "quality_ncr"),
        ("q3", "校准", "quality_calibration"),
    ):
        resp = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": idx, "sender_id": "im-qc", "text": text},
            headers=_h(keys, "admin"),
        ).json()
        assert resp["route"] == route and resp["status"] == "done"
