"""W14–W15 订单交付：合同 → 订单行 → 批次 → 入库 → 出厂检验 → 发货（账本守卫）→ 签收；看板与 IM。"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

PHOTO = [{"kind": "photo", "ref": "oss://evidence/1.jpg"}]
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


def today() -> date:
    return datetime.now(TZ).date()


def ev(client, h, type_, payload, object_id=None, who="admin", expect=(201,), evidence=None, mode="real"):
    body = {"type": type_, "payload": payload, "evidence": evidence or PHOTO, "data_mode": mode}
    if object_id:
        body["object_id"] = object_id
    r = client.post("/v1/events", json=body, headers=h(who))
    assert r.status_code in expect, r.text
    return r.json()


def master(client, h, mode="real"):
    ev(client, h, "sales.customer_registered", {"name": "安徽某钢铁"}, "C1", mode=mode)
    ev(
        client,
        h,
        "sales.product_defined",
        {"code": "QG-1.0", "name": "轻质隔热砖", "unit": "t"},
        "P1",
        mode=mode,
    )
    ev(client, h, "sales.product_defined", {"code": "LN-FB", "name": "纤维毯", "unit": "t"}, "P2", mode=mode)


def contract(client, h, amount=300000, who="sales", due_days=5, expect=201):
    body = {
        "contract_no": "HT2026-018",
        "customer": "C1",
        "signed_date": today().isoformat(),
        "amount": amount,
        "due_date": (today() + timedelta(days=due_days)).isoformat(),
        "payment_terms": "预付 30%，发货前付清",
        "delivery_address": "马鞍山",
        "evidence": [{"kind": "document", "ref": "document:contract-scan", "page": 1}],
    }
    r = client.post("/v1/contracts", json=body, headers=h(who))
    assert r.status_code == expect, r.text
    return r.json()


def lines(client, h, cid, who="sales", expect=201):
    body = {
        "lines": [
            {"product": "P1", "qty": 100, "unit": "吨", "unit_price": 2500},
            {"product": "P2", "qty": 10, "unit": "t", "amount": 30000},
        ]
    }
    r = client.post(f"/v1/contracts/{cid}/orders", json=body, headers=h(who))
    assert r.status_code == expect, r.text
    return r.json()


def batch(client, h, bid, order, planned, good=None, stock=True, inspect=None):
    started = datetime.now(TZ).isoformat()
    ev(
        client,
        h,
        "production.batch_started",
        {
            "product": "P1",
            "order": order,
            "planned_qty": planned,
            "unit": "t",
            "started_at": started,
            "status": "in_progress",
        },
        bid,
    )
    if good is not None:
        ev(
            client,
            h,
            "production.batch_completed",
            {"good_qty": good, "completed_at": started, "status": "completed"},
            bid,
        )
    if stock:
        ev(client, h, "warehouse.batch_stocked", {"status": "in_stock"}, bid)
    if inspect:
        ev(
            client,
            h,
            "quality.inspection_recorded",
            {
                "kind": "final",
                "batch": bid,
                "inspected_at": started,
                "result": inspect,
                "report_no": f"R-{bid}",
            },
        )


def test_contract_to_delivery_flow(client, h):
    master(client, h)
    c = contract(client, h)
    assert c["object_type"] == "contract" and c["notes"] == []
    cid = c["object_id"]
    out = lines(client, h, cid)
    assert len(out["orders"]) == 2 and out["lines_total"] == 280000.0 and not out["warnings"]
    so = out["orders"][0]["object_id"]
    assert out["orders"][0]["order_no"] == "HT2026-018-1"
    # 重复提交：幂等，不会多出订单
    again = lines(client, h, cid)
    assert [o["object_id"] for o in again["orders"]] == [o["object_id"] for o in out["orders"]]
    assert all(o["duplicate"] for o in again["orders"])

    # 刚下单：待排产，5 天后交期而计划量为 0 → 交期风险
    p = client.get(f"/v1/orders/{so}", headers=h("sales")).json()
    assert p["stage"] == "not_planned" and p["contract"]["contract_no"] == "HT2026-018"
    assert p["unit"] == "吨" and p["due_date"] == (today() + timedelta(days=5)).isoformat()
    assert [r["code"] for r in p["risks"]] == ["under_planned"]

    # 两个批次：B1 入库未检，B2 只开工；吨 / t 视为同一单位
    batch(client, h, "B1", so, 60, good=58)
    batch(client, h, "B2", so, 45, stock=False)
    p = client.get(f"/v1/orders/{so}", headers=h("sales")).json()
    q = p["quantities"]
    assert (q["planned"], q["produced"], q["stocked"], q["shipped"]) == (105, 58, 58, 0)
    assert p["stage"] == "in_production"
    assert {r["code"] for r in p["risks"]} == {"final_missing"}

    # 发货预检：没有出厂检验 → 阻断；B2 未入库 → 阻断
    r = client.post(
        f"/v1/orders/{so}/shipments/check", json={"batches": ["B1", "B2"], "qty": 40}, headers=h("warehouse")
    )
    chk = r.json()
    assert not chk["ok"]
    assert any("B1 没有出厂检验" in x for x in chk["problems"])
    assert any("B2 还没有入库" in x for x in chk["problems"])
    ship = {"order": so, "batches": ["B1"], "qty": 40, "vehicle_no": "皖N12345", "evidence": PHOTO}
    r = client.post("/v1/shipments", json=ship, headers=h("warehouse"))
    assert r.status_code == 422 and "没有出厂检验" in r.json()["detail"]["message"]

    # 绕过接口直接写事件也不行：账本守卫
    raw = {
        "shipment_no": "X1",
        "order": so,
        "batches": ["B1"],
        "qty": 40,
        "unit": "吨",
        "shipped_at": datetime.now(TZ).isoformat(),
        "status": "in_transit",
    }
    r = client.post(
        "/v1/events",
        json={"type": "warehouse.shipment_dispatched", "payload": raw, "evidence": PHOTO},
        headers=h("warehouse"),
    )
    assert r.status_code == 422 and "没有出厂检验" in r.json()["detail"]

    # 出厂检验合格后可以发；销售不能发货
    ev(
        client,
        h,
        "quality.inspection_recorded",
        {
            "kind": "final",
            "batch": "B1",
            "inspected_at": datetime.now(TZ).isoformat(),
            "result": "pass",
            "report_no": "R-0001",
        },
    )
    assert client.post("/v1/shipments", json=ship, headers=h("sales")).status_code == 403
    r = client.post("/v1/shipments", json=ship, headers=h("warehouse"))
    assert r.status_code == 201, r.text
    sh = r.json()
    assert sh["object_type"] == "shipment" and sh["warnings"] == []

    # 批次可发数量：58 − 40 = 18，再发 20 → 阻断
    r = client.post("/v1/shipments", json={**ship, "qty": 20}, headers=h("warehouse"))
    assert r.status_code == 422 and "可发数量不足" in r.json()["detail"]["message"]
    # 超发：订单 100 吨 + 3% 容差
    batch(client, h, "B3", so, 80, good=80, inspect="pass")
    r = client.post("/v1/shipments", json={**ship, "batches": ["B3"], "qty": 64}, headers=h("warehouse"))
    assert r.status_code == 422 and "超发" in r.json()["detail"]["message"]

    p = client.get(f"/v1/orders/{so}", headers=h("sales")).json()
    assert p["stage"] == "partly_shipped" and p["quantities"]["shipped"] == 40
    assert p["shipments"][0]["status"] == "in_transit"

    # 签收（仓管不能登记签收）
    receipt = {
        "status": "delivered",
        "receiver": "张工",
        "received_qty": 40,
        "evidence": [{"kind": "photo", "ref": "oss://sign.jpg"}],
    }
    path = f"/v1/shipments/{sh['object_id']}/receipt"
    assert client.post(path, json=receipt, headers=h("warehouse")).status_code == 403
    r = client.post(path, json=receipt, headers=h("sales"))
    assert r.status_code == 200, r.text
    assert client.post(path, json=receipt, headers=h("sales")).status_code == 409  # 不能重复登记
    p = client.get(f"/v1/orders/{so}", headers=h("sales")).json()
    assert p["quantities"]["delivered"] == 40 and p["quantities"]["remaining"] == 60

    # 发货单 Excel：批号、检验报告编号来自账本
    x = client.get(f"/v1/shipments/{sh['object_id']}/export", headers=h("warehouse"))
    assert x.status_code == 200
    ws = load_workbook(io.BytesIO(x.content))["发货单"]
    cells = [c for row in ws.iter_rows(values_only=True) for c in row if c is not None]
    assert "HT2026-018" in cells and "B1" in cells and "R-0001" in cells and "安徽某钢铁" in cells

    # 看板：有风险的在前；租户隔离；工人没有订单权限
    board = client.get("/v1/orders", headers=h("sales")).json()
    assert board["count"] == 2
    assert client.get(f"/v1/orders/{so}", headers=h("other_admin")).status_code == 404
    assert client.get("/v1/orders", headers=h("worker")).status_code == 403
    # 追溯：从批次能找到发货
    rep = client.get("/v1/trace", params={"q": "HT2026-018"}, headers=h("admin")).json()
    assert rep["report"]["root"]["object_type"] == "contract"


def test_failed_inspection_and_refusal(client, h):
    master(client, h)
    cid = contract(client, h)["object_id"]
    so = lines(client, h, cid)["orders"][0]["object_id"]
    batch(client, h, "B1", so, 50, good=50, inspect="fail")
    batch(client, h, "B2", so, 50, good=50, inspect="conditional")
    r = client.post(
        f"/v1/orders/{so}/shipments/check", json={"batches": ["B1"], "qty": 10}, headers=h("warehouse")
    ).json()
    assert not r["ok"] and any("不合格" in x for x in r["problems"])
    p = client.get(f"/v1/orders/{so}", headers=h("sales")).json()
    assert "final_fail" in {x["code"] for x in p["risks"]}

    # 有条件放行可以发，但提示
    ship = {"order": so, "batches": ["B2"], "qty": 30, "evidence": PHOTO}
    r = client.post("/v1/shipments", json=ship, headers=h("warehouse"))
    assert r.status_code == 201 and any("有条件放行" in w for w in r.json()["warnings"])
    shid = r.json()["object_id"]
    # 拒收必须写原因；拒收后数量退回可发
    bad = {"status": "refused", "evidence": PHOTO}
    assert client.post(f"/v1/shipments/{shid}/receipt", json=bad, headers=h("sales")).status_code == 422
    bad["note"] = "外观缺角超标"
    assert client.post(f"/v1/shipments/{shid}/receipt", json=bad, headers=h("sales")).status_code == 200
    p = client.get(f"/v1/orders/{so}", headers=h("sales")).json()
    assert p["quantities"]["shipped"] == 0 and p["quantities"]["refused"] == 30
    assert "refused" in {x["code"] for x in p["risks"]}
    chk = client.post(
        f"/v1/orders/{so}/shipments/check", json={"batches": ["B2"], "qty": 50}, headers=h("warehouse")
    ).json()
    assert chk["ok"], chk


def test_large_contract_needs_approval_lines_do_not(client, h):
    master(client, h)
    r = contract(client, h, amount=800000, expect=202)
    ap = r["approval"]
    assert [s["name"] for s in ap["steps"]] == ["销售负责人审批", "总经理审批"]
    for who in ("sales_lead", "gm"):
        d = client.post(f"/v1/approvals/{ap['id']}/decide", json={"decision": "approve"}, headers=h(who))
        assert d.status_code == 200, d.text
    cs = client.get("/v1/objects/contract", headers=h("sales")).json()
    items = cs.get("items") or cs.get("objects")
    cid = items[0]["object_id"]
    # 合同拆出的大额订单行不再重复审批
    body = {"lines": [{"product": "P1", "qty": 300, "unit": "t", "unit_price": 2600}]}
    r = client.post(f"/v1/contracts/{cid}/orders", json=body, headers=h("sales"))
    assert r.status_code == 201, r.text
    # 没有合同的大额散单仍要审批
    big = {"customer": "C1", "product": "P1", "qty": 300, "unit": "t", "amount": 780000, "status": "open"}
    ev(client, h, "sales.order_created", big, who="sales", expect=(202,))
    # 交期变更要销售负责人确认
    so = r.json()["orders"][0]["object_id"]
    resched = {"due_date": (today() + timedelta(days=30)).isoformat(), "note": "客户来函同意延期"}
    ev(client, h, "sales.order_rescheduled", resched, so, who="sales", expect=(202,))


def test_quote_to_contract_draft(client, keys, h):
    from tests.test_engineering import _accept_all, _ready_design, _seed_kb

    eng = h("engineer")
    _seed_kb(client, keys)
    pid, a = _ready_design(client, keys)
    a = client.get(f"/v1/eng/designs/{a['id']}", headers=eng).json()
    _accept_all(client, eng, a)
    ap = client.post(f"/v1/eng/designs/{a['id']}/publish", headers=eng).json()["approval"]
    client.post(f"/v1/approvals/{ap['id']}/decide", json={"decision": "approve"}, headers=h("sales_lead"))

    d = client.get("/v1/contracts/draft", params={"eng_project": pid}, headers=h("sales")).json()
    assert d["event"] == "sales.contract_signed_from_quote" and d["data_mode"] == "real"
    assert d["contract"]["amount"] == a["total"] and d["contract"]["eng_project"] == pid
    assert d["lines"] and all(ln["amount"] is not None for ln in d["lines"])
    assert any("没有关联客户" in x for x in d["problems"])  # 样例技术文件只有客户名称

    master(client, h)
    body = {
        "contract_no": "HT-Q-1",
        "customer": "C1",
        "signed_date": today().isoformat(),
        "amount": min(d["contract"]["amount"], 400000),
        "eng_project": pid,
        "quote_no": d["contract"]["quote_no"],
        "evidence": [{"kind": "document", "ref": "document:x", "page": 1}],
    }
    r = client.post("/v1/contracts", json=body, headers=h("sales"))
    assert r.status_code == 201, r.text
    assert r.json()["notes"] == ["工程项目状态还是“已报价”，请工程技术改为赢单"]  # 销售没有工程写权限
    c = client.get(f"/v1/objects/contract/{r.json()['object_id']}", headers=h("sales")).json()
    assert c["state"]["eng_project"] == pid
    # 管理员登记时直接把项目标为赢单
    body["contract_no"] = "HT-Q-2"
    r = client.post("/v1/contracts", json=body, headers=h("admin"))
    assert r.json()["notes"] == ["工程项目已标为赢单"]
    proj = client.get(f"/v1/objects/eng_project/{pid}", headers=h("admin")).json()
    assert proj["state"]["status"] == "won"


def test_order_fast_path_and_mock_export(client, h):
    master(client, h)
    cid = contract(client, h, due_days=-2)["object_id"]
    so = lines(client, h, cid)["orders"][0]["object_id"]
    r = client.put(
        "/v1/channels/identities",
        json={"channel": "mock", "external_id": "im-s", "login": "sales"},
        headers=h("admin"),
    )
    assert r.status_code == 200

    def say(i, text):
        body = {"external_msg_id": f"o{i}", "sender_id": "im-s", "kind": "text", "text": text}
        return client.post("/v1/channels/mock/messages", json=body, headers=h("admin")).json()

    out = say(1, "@助手 订单 HT2026-018-1")
    assert out["route"] == "order" and out["status"] == "done"
    card = out["replies"][0]["card"]
    assert card["title"] == "订单 HT2026-018-1" and "已逾期 2 天" in card["text"]
    out = say(2, "交期风险")
    assert out["route"] == "order_board"
    assert {f["label"] for f in out["replies"][0]["card"]["fields"]} == {"HT2026-018-1", "HT2026-018-2"}
    out = say(3, "订单 HT2026-018")  # 合同编号 → 合同下的订单行
    assert out["replies"][0]["card"]["title"] == "合同 HT2026-018"
    out = say(4, "订单 NOPE-1")
    assert "没有找到" in out["replies"][0]["text"]
    assert so

    # 模拟数据可以发货演示，但不能导出正式发货单
    ev(client, h, "sales.customer_registered", {"name": "演示客户"}, "MC", mode="mock")
    ev(client, h, "sales.product_defined", {"code": "M", "name": "演示砖", "unit": "t"}, "MP", mode="mock")
    mo = ev(
        client,
        h,
        "sales.order_created",
        {"customer": "MC", "product": "MP", "qty": 5, "unit": "t", "status": "open"},
        "MO",
        mode="mock",
    )
    assert mo["object_id"] == "MO"
    cfg = client.app.state.orders["luneng"]
    cfg.shipping.require_final_inspection = False
    cfg.shipping.require_in_stock = False
    try:
        r = client.post(
            "/v1/shipments", json={"order": "MO", "qty": 5, "evidence": PHOTO}, headers=h("warehouse")
        )
        assert r.status_code == 201, r.text
        assert any("没有填发货批次" in w for w in r.json()["warnings"])
        x = client.get(f"/v1/shipments/{r.json()['object_id']}/export", headers=h("warehouse"))
        assert x.status_code == 409
    finally:
        cfg.shipping.require_final_inspection = True
        cfg.shipping.require_in_stock = True


def test_agent_tools(client, h):

    from fos.governance.principal import Principal
    from fos.tools.registry import REGISTRY, ToolContext, ToolError

    master(client, h)
    cid = contract(client, h)["object_id"]
    lines(client, h, cid)
    svc = client.app.state.services
    tenant = svc.registry.get("luneng")
    skill = svc.skills["luneng"]["order_desk"]

    async def run(roles, name, args):
        p = Principal(
            tenant_id="luneng",
            subject_id="u",
            kind="user",
            display_name="u",
            roles=tuple(roles),
            permissions=tenant.permissions_for(roles),
        )
        async with svc.sessionmaker() as s:
            ctx = ToolContext(
                session=s,
                user=p,
                skill=skill,
                ontology=svc.ontologies["luneng"],
                extras=svc.runtime.tool_extras["luneng"],
            )
            return await REGISTRY[name].call(ctx, args)

    out = client.portal.call(run, ["sales"], "orders.progress", {"order": "HT2026-018-2"})
    assert out["product"]["code"] == "LN-FB" and out["stage"] == "not_planned"
    board = client.portal.call(run, ["sales"], "orders.board", {"risky_only": True})
    assert board["count"] == 2
    with pytest.raises(ToolError):
        client.portal.call(run, ["worker"], "orders.board", {})
