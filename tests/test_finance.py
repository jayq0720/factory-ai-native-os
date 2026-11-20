"""W23 财务集成与成本核算端到端测试：应收/应付、大额付款审批、批次成本、凭证草稿与对账单。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from fos.finance.config import FinanceConfig
from fos.finance.costing import compute_batch_cost, compute_order_margin, split_vat


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def _ev(client, keys, ev_type: str, object_id: str, payload: dict, who: str = "admin", mode: str = "real"):
    r = client.post(
        "/v1/events",
        json={
            "type": ev_type,
            "object_id": object_id,
            "payload": payload,
            "evidence": [{"kind": "manual", "ref": "test:finance"}],
            "data_mode": mode,
        },
        headers=_h(keys, who),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_deterministic_costing_and_vat_split():
    net, tax = split_vat(113000.0, 0.13)
    assert net == 100000.0 and tax == 13000.0 and round(net + tax, 2) == 113000.0
    with pytest.raises(ValueError, match="有限数字"):
        split_vat(float("nan"), 0.13)

    cfg = FinanceConfig()
    res = compute_batch_cost(
        cfg,
        batch_id="B-1",
        batch_state={"product": "P-1", "good_qty": 1000, "unit": "块"},
        product_state={"name": "轻质砖"},
        stock_moves=[
            {"material": "M-1", "kind": "issue", "qty": 5.0, "unit": "吨"},
            {"material": "M-1", "kind": "return", "qty": 1.0, "unit": "吨"},
        ],
        material_names={"M-1": "莫来石粉"},
        material_prices={"M-1": 2000.0},
        kiln_run_state={"energy_kwh": 1000.0, "gas_m3": 100.0},
        kiln_share_ratio=0.5,
    )
    # 直接材料: 4 * 2000 = 8000; 能耗: (1000*0.78 + 100*3.6) * 0.5 = 570; 人工+制造: 1000*(1.2+1.5)=2700
    assert res.material_cost == 8000.0 and res.energy_cost == 570.0
    assert res.labor_cost == 1200.0 and res.overhead_cost == 1500.0
    assert res.total_cost == 11270.0 and res.unit_cost == 11.27

    margin = compute_order_margin(20000.0, [res])
    assert margin["gross_profit"] == 8730.0 and margin["gross_margin_pct"] == 43.65


def test_finance_ar_ap_vouchers_cost_and_im_flow(client, keys):
    today = date.today()
    overdue_day = (today - timedelta(days=15)).isoformat()
    future_day = (today + timedelta(days=30)).isoformat()

    # 准备客户、供应商、产品、原料、采购单、来料与领料、窑次、订单、生产批次
    _ev(client, keys, "sales.customer_registered", "C-F1", {"name": "华东炉窑有限公司"})
    _ev(client, keys, "purchase.supplier_registered", "S-F1", {"name": "河南莫来石厂"})
    _ev(
        client,
        keys,
        "sales.product_defined",
        "P-F1",
        {"code": "LN-1260", "name": "轻质隔热砖", "unit": "块"},
    )
    _ev(
        client,
        keys,
        "warehouse.material_defined",
        "M-F1",
        {"name": "莫来石粉", "category": "raw", "unit": "吨"},
    )
    _ev(
        client,
        keys,
        "purchase.order_created",
        "PO-F1",
        {
            "supplier": "S-F1",
            "material": "M-F1",
            "qty": 20,
            "unit": "吨",
            "unit_price": 2000,
            "amount": 40000,
            "status": "open",
        },
    )
    _ev(
        client,
        keys,
        "sales.order_created",
        "SO-F1",
        {
            "customer": "C-F1",
            "product": "P-F1",
            "qty": 1000,
            "unit": "块",
            "unit_price": 25,
            "amount": 25000,
            "status": "in_production",
        },
    )
    _ev(
        client,
        keys,
        "warehouse.lot_received",
        "LOT-F1",
        {
            "material": "M-F1",
            "supplier": "S-F1",
            "qty": 10,
            "unit": "吨",
            "received_at": "2026-11-18T08:00:00+08:00",
            "status": "accepted",
        },
    )
    _ev(client, keys, "production.kiln_registered", "K-F1", {"name": "1号梭式窑", "kind": "shuttle"})
    _ev(
        client,
        keys,
        "production.kiln_run_started",
        "KR-F1",
        {"kiln": "K-F1", "run_no": "R-01", "started_at": "2026-11-18T09:00:00+08:00", "status": "running"},
    )
    _ev(
        client,
        keys,
        "production.kiln_run_finished",
        "KR-F1",
        {
            "finished_at": "2026-11-19T09:00:00+08:00",
            "energy_kwh": 1000,
            "gas_m3": 100,
            "status": "finished",
        },
    )
    _ev(
        client,
        keys,
        "production.batch_started",
        "B-F1",
        {
            "product": "P-F1",
            "order": "SO-F1",
            "planned_qty": 1000,
            "unit": "块",
            "started_at": "2026-11-18T09:00:00+08:00",
            "status": "completed",
        },
    )
    _ev(client, keys, "production.batch_fired", "B-F1", {"kiln_run": "KR-F1"})
    _ev(
        client,
        keys,
        "warehouse.stock_issued",
        "MV-F1",
        {
            "lot": "LOT-F1",
            "material": "M-F1",
            "kind": "issue",
            "qty": 4,
            "unit": "吨",
            "moved_at": "2026-11-18T10:00:00+08:00",
            "batch": "B-F1",
        },
    )

    # 1. 权限隔离：工人无权访问财务看板
    assert client.get("/v1/finance/ar", headers=_h(keys, "worker")).status_code == 403

    # 2. 应收挂账（关联订单自动带出客户与金额）、开票、部分回款、防超收
    ar_r = client.post(
        "/v1/finance/ar",
        json={"order": "SO-F1", "due_date": overdue_day},
        headers=_h(keys, "finance"),
    )
    assert ar_r.status_code == 201, ar_r.text
    ar_id = ar_r.json()["id"]

    inv_r = client.post(
        f"/v1/finance/ar/{ar_id}/invoice",
        json={"invoice_no": "FP-2026-001", "invoiced_at": today.isoformat()},
        headers=_h(keys, "finance"),
    )
    assert inv_r.status_code == 200, inv_r.text

    rec_r = client.post(
        f"/v1/finance/ar/{ar_id}/receive",
        json={"amount": 10000},
        headers=_h(keys, "finance"),
    )
    assert rec_r.status_code == 200, rec_r.text

    # 尝试超收 20000（累计 30000 > 25000）被账本守卫拦截
    over_r = client.post(
        f"/v1/finance/ar/{ar_id}/receive",
        json={"amount": 20000},
        headers=_h(keys, "finance"),
    )
    assert over_r.status_code == 422

    ar_b = client.get("/v1/finance/ar", headers=_h(keys, "finance")).json()
    assert ar_b["summary"]["total_balance"] == 15000.0 and ar_b["summary"]["overdue_count"] == 1
    assert ar_b["summary"]["aging"]["1_30d"] == 15000.0

    # 3. 应付挂账 + 大额付款（>=50000）自动触发总经理审批
    ap_r = client.post(
        "/v1/finance/ap",
        json={"supplier": "S-F1", "amount": 60000, "due_date": future_day},
        headers=_h(keys, "finance"),
    )
    assert ap_r.status_code == 201, ap_r.text
    ap_id = ap_r.json()["id"]

    pay_big = client.post(
        f"/v1/finance/ap/{ap_id}/pay",
        json={"amount": 50000, "note": "支付大额原料款"},
        headers=_h(keys, "finance"),
    )
    assert pay_big.status_code == 202, pay_big.text
    approval_id = pay_big.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "comment": "同意付款"},
        headers=_h(keys, "gm"),
    )
    assert dec.status_code == 200, dec.text

    ap_b = client.get("/v1/finance/ap", headers=_h(keys, "finance")).json()
    assert ap_b["summary"]["total_paid"] == 50000.0 and ap_b["summary"]["total_balance"] == 10000.0

    # 4. 批次成本与订单毛利（确定性计算）
    bc = client.get("/v1/finance/cost/batches/B-F1", headers=_h(keys, "finance")).json()
    # 材料 4*2000=8000, 能耗 1000*0.78+100*3.6=1140, 人工+制造 1000*2.7=2700 -> 11840
    assert bc["material_cost"] == 8000.0 and bc["energy_cost"] == 1140.0 and bc["total_cost"] == 11840.0

    om = client.get("/v1/finance/cost/orders/SO-F1", headers=_h(keys, "finance")).json()
    assert om["revenue"] == 25000.0 and om["total_cost"] == 11840.0 and om["gross_profit"] == 13160.0

    # 5. 凭证草稿：借贷不平被账本守卫拒绝；由应收单自动生成价税分离凭证 + 金蝶导出 + 过账
    unbal = client.post(
        "/v1/finance/vouchers",
        json={
            "voucher_date": today.isoformat(),
            "summary": "故意不平凭证",
            "lines": [
                {"account_code": "1122", "account_name": "应收账款", "debit": 1000, "credit": 0},
                {"account_code": "6001", "account_name": "主营业务收入", "debit": 0, "credit": 900},
            ],
        },
        headers=_h(keys, "finance"),
    )
    assert unbal.status_code == 422

    v_r = client.post(
        "/v1/finance/vouchers/from-source",
        json={"source_type": "ar", "source_id": ar_id, "voucher_date": today.isoformat()},
        headers=_h(keys, "finance"),
    )
    assert v_r.status_code == 201, v_r.text
    vid = v_r.json()["id"]
    assert v_r.json()["total_debit"] == 25000.0 and v_r.json()["total_credit"] == 25000.0

    exp_r = client.get("/v1/finance/vouchers/export?target=kingdee", headers=_h(keys, "finance"))
    assert exp_r.status_code == 200 and exp_r.content.startswith(b"PK")
    wb = load_workbook(io.BytesIO(exp_r.content))
    assert wb.sheetnames == ["凭证导入", "导出说明"]

    post_r = client.post(
        f"/v1/finance/vouchers/{vid}/post",
        json={"external_system": "kingdee", "external_ref": "KD-2026-11-0018"},
        headers=_h(keys, "finance"),
    )
    assert post_r.status_code == 200, post_r.text

    # 6. 正式客户对账单导出 + 模拟客户禁止导出 + 逾期提醒去重
    rec_x = client.get("/v1/finance/reconciliation/customer/C-F1/export", headers=_h(keys, "finance"))
    assert rec_x.status_code == 200 and rec_x.content.startswith(b"PK")
    _ev(client, keys, "sales.customer_registered", "C-MOCK", {"name": "模拟客户"}, mode="mock")
    assert (
        client.get("/v1/finance/reconciliation/customer/C-MOCK", headers=_h(keys, "finance")).status_code
        == 409
    )

    rem1 = client.post("/v1/finance/remind", headers=_h(keys, "finance")).json()
    rem2 = client.post("/v1/finance/remind", headers=_h(keys, "finance")).json()
    assert rem1["count"] == 1 and rem2["count"] == 0

    # 7. IM Fast Path：应收 / 应付 / 成本 B-F1 / 凭证
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "im-fin", "login": "finance"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )
    for idx, text, route in (
        ("f1", "应收", "finance_ar"),
        ("f2", "应付", "finance_ap"),
        ("f3", "成本 B-F1", "finance_cost"),
        ("f4", "凭证", "finance_vouchers"),
    ):
        resp = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": idx, "sender_id": "im-fin", "text": text},
            headers=_h(keys, "admin"),
        ).json()
        assert resp["route"] == route and resp["status"] == "done"
