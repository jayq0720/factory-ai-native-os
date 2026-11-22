"""W27c 经营决策驾驶舱与市场商机端到端测试：漏斗预测、大额丢单复盘审批、跨模块驾驶舱、导出与 IM Fast Path。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from fos.market.analytics import compute_funnel


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_funnel_deterministic_math():
    m = compute_funnel(
        [
            {"stage": "quoted", "est_amount": 200000.0, "probability": 50},
            {"stage": "negotiation", "est_amount": 400000.0, "probability": 75},
            {"stage": "won", "est_amount": 300000.0, "probability": 100},
            {"stage": "lost", "est_amount": 100000.0, "probability": 0},
        ]
    )
    assert m["open_count"] == 2 and m["raw_pipeline_amount"] == 600000.0
    assert m["weighted_pipeline_amount"] == 400000.0
    assert m["won_amount"] == 300000.0 and m["win_rate_pct"] == 50.0

    with pytest.raises(ValueError, match="有限数字"):
        compute_funnel([{"stage": "new", "est_amount": float("nan"), "probability": 10}])


def test_market_and_cockpit_end_to_end(client, keys):
    today = date.today()
    close_dt = (today + timedelta(days=30)).isoformat()

    # 1. 权限校验：仓库无权建商机；销售可新建/跟进商机
    assert (
        client.post(
            "/v1/market/leads",
            json={
                "customer_name": "无权",
                "industry": "steel",
                "product_interest": "镁碳砖",
                "est_amount": 100000.0,
                "owner_name": "X",
                "expected_close_date": close_dt,
            },
            headers=_h(keys, "warehouse"),
        ).status_code
        == 403
    )

    r_l1 = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "宝武马钢炼钢厂",
            "industry": "steel",
            "source": "existing_customer",
            "product_interest": "钢包镁碳砖",
            "est_amount": 300000.0,
            "owner_name": "刘销售",
            "stage": "quoted",
            "expected_close_date": close_dt,
        },
        headers=_h(keys, "sales"),
    )
    assert r_l1.status_code == 201, r_l1.text
    lid1 = r_l1.json()["id"]

    # 更新阶段至 negotiation（自动按配置更新概率至 75%），随后赢单结案；已结案禁止再修改
    assert (
        client.patch(
            f"/v1/market/leads/{lid1}",
            json={"stage": "negotiation"},
            headers=_h(keys, "sales"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/market/leads/{lid1}/close",
            json={"stage": "won", "note": "技术商务双中标，签订年度供货协议"},
            headers=_h(keys, "sales"),
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/v1/market/leads/{lid1}",
            json={"note": "已结案修改"},
            headers=_h(keys, "sales"),
        ).status_code
        == 422
    )

    # 2. 50 万元及以上大额商机丢单关闭触发销售负责人复盘确认（202）-> sales_lead 批准
    r_l2 = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "华润水泥熟料线",
            "industry": "cement",
            "source": "tender_portal",
            "product_interest": "镁铝尖晶石砖",
            "est_amount": 800000.0,
            "owner_name": "刘销售",
            "stage": "quoted",
            "expected_close_date": close_dt,
        },
        headers=_h(keys, "sales"),
    )
    lid2 = r_l2.json()["id"]
    r_lost = client.post(
        f"/v1/market/leads/{lid2}/close",
        json={"stage": "lost", "note": "竞争对手低价 8% 抢标"},
        headers=_h(keys, "sales"),
    )
    assert r_lost.status_code == 202, r_lost.text
    apr_id = r_lost.json()["approval"]["id"]
    dec = client.post(
        f"/v1/approvals/{apr_id}/decide",
        json={"decision": "approve", "comment": "已复盘报价成本，归档丢单"},
        headers=_h(keys, "sales_lead"),
    )
    assert dec.status_code == 200, dec.text

    # 3. 竞品情报登记
    r_int = client.post(
        "/v1/market/intel",
        json={
            "competitor": "某耐材大厂",
            "product_category": "镁铝尖晶石砖",
            "quoted_price": 5600.0,
            "region": "华东",
            "recorded_date": today.isoformat(),
            "summary": "四季度促销价下调 200 元/吨",
        },
        headers=_h(keys, "sales"),
    )
    assert r_int.status_code == 201, r_int.text

    # 4. 跨部门经营决策驾驶舱（GM 只读查看）与三表 Excel 导出
    ck = client.get("/v1/market/cockpit", headers=_h(keys, "gm")).json()
    assert ck["kpis"]["market_won_amount"] == 300000.0 and ck["kpis"]["market_win_rate_pct"] == 50.0
    xls = client.get("/v1/market/report.xlsx", headers=_h(keys, "gm"))
    assert xls.status_code == 200
    wb = load_workbook(io.BytesIO(xls.content))
    assert wb.sheetnames == ["经营决策驾驶舱", "市场商机漏斗", "竞品行情情报"]


def test_market_im_fast_path(client, keys):
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "gm_exec_1", "login": "gm"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )

    cases = [
        ("驾驶舱", "market_cockpit", "经营决策驾驶舱"),
        ("商机", "market_funnel", "商机漏斗"),
        ("竞品", "market_intel", "竞品行情"),
    ]
    for idx, (word, expected_route, title_kw) in enumerate(cases):
        r = client.post(
            "/v1/channels/mock/messages",
            json={"external_msg_id": f"mkt_fp_{idx}", "sender_id": "gm_exec_1", "text": word},
            headers=_h(keys, "admin"),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["route"] == expected_route and data["status"] == "done"
        assert title_kw in data["replies"][0]["card"]["title"]
