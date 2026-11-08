"""W17 采购与仓储：实时库存、领料红线、退料、盘点；
缺料 → 请购建议 → 比价 → 采购单 → 按单到货；供应商评分、IM。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.test_planning import PHOTO, TZ, ev, order, setup_plant


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


def at(days: float = 0) -> str:
    return (datetime.now(TZ) + timedelta(days=days)).isoformat()


def lot(client, h, lid, material, qty, status, days=0, **kw):
    payload = {"material": material, "qty": qty, "unit": "t", "received_at": at(days), "status": status} | kw
    return ev(client, h, "warehouse.lot_received", payload, lid, who="warehouse")


def bind(client, h, who, ext):
    r = client.put(
        "/v1/channels/identities",
        json={"channel": "mock", "external_id": ext, "login": who},
        headers=h("admin"),
    )
    assert r.status_code == 200


def stock_of(client, h, material, who="warehouse"):
    r = client.get(f"/v1/stock?material={material}", headers=h(who))
    assert r.status_code == 200, r.text
    return r.json()["materials"][0]


def test_stock_issue_return_count(client, h):
    ev(client, h, "purchase.supplier_registered", {"name": "某矾土厂"}, "S1")
    ev(client, h, "warehouse.material_defined", {"name": "高铝矾土", "category": "raw", "unit": "t"}, "M1")
    ev(client, h, "purchase.material_sourcing_set", {"safety_stock": 8}, "M1", who="purchaser")
    lot(client, h, "L0", "M1", 3, "accepted", days=-5, location="A-01")
    lot(client, h, "L1", "M1", 10, "accepted", days=-1)
    lot(client, h, "L2", "M1", 5, "pending_inspection")
    lot(client, h, "L3", "M1", 2, "rejected")

    s = stock_of(client, h, "M1")
    assert (s["usable"], s["incoming"], s["blocked"], s["below_safety"]) == (13, 5, 2, False)
    assert [x["lot"] for x in s["lots"]][:2] == ["L0", "L1"]  # 先进先出
    assert client.get("/v1/stock", headers=h("worker")).status_code == 403

    # 红线：待检、不合格不能领；不能超过现存。从 /v1/events 直接写也绕不过去
    r = client.post("/v1/stock/issues", json={"lot": "L2", "qty": 1}, headers=h("warehouse"))
    assert r.status_code == 422 and "待检" in r.text
    mv = {"lot": "L3", "material": "M1", "kind": "issue", "qty": 1, "unit": "t", "moved_at": at()}
    ev(client, h, "warehouse.stock_issued", mv, who="warehouse", expect=(422,))
    ev(client, h, "warehouse.stock_issued", mv | {"lot": "L1", "qty": 11}, who="warehouse", expect=(422,))
    ev(
        client,
        h,
        "warehouse.stock_issued",
        mv | {"lot": "L1", "kind": "return"},
        who="warehouse",
        expect=(422,),
    )
    r = client.post("/v1/stock/issues", json={"material": "M1", "qty": 14}, headers=h("warehouse"))
    assert r.status_code == 422 and "可用现存 13" in r.text
    assert (
        client.post("/v1/stock/issues", json={"lot": "L1", "qty": 1}, headers=h("worker")).status_code == 403
    )

    # 按物料领 6 t：先领完 L0 的 3 t，再从 L1 领 3 t；领后 7 t < 安全库存 8 → 推给仓管和采购
    bind(client, h, "warehouse", "im-wh")
    bind(client, h, "purchaser", "im-buy")
    r = client.post("/v1/stock/issues", json={"material": "M1", "qty": 6}, headers=h("warehouse"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["allocation"] == [{"lot": "L0", "qty": 3}, {"lot": "L1", "qty": 3}]
    assert out["stock"][0]["usable"] == 7 and out["stock"][0]["below_safety"]
    assert out["notified"] == 2
    sent = client.app.state.mock_channel.outbox
    assert any(
        m["type"] == "text" and "低于安全库存" in m["text"] and m["to"]["user_id"] == "im-buy" for m in sent
    )

    # 退料：不能超过已领未退
    r = client.post("/v1/stock/returns", json={"lot": "L1", "qty": 4}, headers=h("warehouse"))
    assert r.status_code == 422 and "已领未退 3" in r.text
    r = client.post("/v1/stock/returns", json={"lot": "L1", "qty": 1}, headers=h("warehouse"))
    assert r.status_code == 201, r.text
    assert stock_of(client, h, "M1")["usable"] == 8

    # 盘点：L1 账面 8，实盘 7.5 → 盘亏 0.5；L0 账面 0、实盘 0 无差异
    body = {"lines": [{"lot": "L1", "counted": 7.5}, {"lot": "L0", "counted": 0}]}
    pv = client.post("/v1/stock/counts/preview", json=body, headers=h("warehouse")).json()
    assert (
        pv["differences"] == 1 and pv["lines"][0]["diff"] == -0.5 and pv["lines"][0]["kind"] == "count_loss"
    )
    assert client.post("/v1/stock/counts", json=body, headers=h("warehouse")).status_code == 422  # 要附照片
    r = client.post("/v1/stock/counts", json=body | {"evidence": PHOTO}, headers=h("warehouse"))
    assert r.status_code == 201, r.text
    assert len(r.json()["events"]) == 1 and r.json()["count_no"].startswith("PD-")
    assert stock_of(client, h, "M1")["usable"] == 7.5

    # 投影可以从账本重放：库存仍然一致
    from fos.ledger.projection import rebuild

    svc = client.app.state.services

    async def replay():
        async with svc.sessionmaker() as s, s.begin():
            await rebuild(s, svc.ontologies["luneng"], "luneng")

    client.portal.call(replay)
    assert stock_of(client, h, "M1")["usable"] == 7.5


def test_shortage_to_requisition_po_receipt(client, h):
    setup_plant(client, h)  # M1 高铝矾土：现存 10 t（L1）
    order(client, h, "SO-1", "P1", 30, 10)
    ev(client, h, "purchase.supplier_registered", {"name": "甲矾土厂"}, "S1")
    ev(client, h, "purchase.supplier_registered", {"name": "乙矾土厂"}, "S2")
    ev(
        client,
        h,
        "purchase.material_sourcing_set",
        {"min_order_qty": 20, "default_supplier": "S2"},
        "M1",
        who="purchaser",
    )
    d = client.post("/v1/planning/draft", headers=h("planner")).json()
    body = {
        "horizon_start": d["horizon_start"],
        "horizon_end": d["horizon_end"],
        "runs": [
            {k: r[k] for k in ("kiln", "run_no", "start_at", "end_at")}
            | {"loads": [{"order": ld["order"], "qty": ld["qty"]} for ld in r["loads"]]}
            for r in d["runs"]
        ],
        "solver_status": d["solver"]["status"],
    }
    assert client.post("/v1/planning/plans", json=body, headers=h("planner")).status_code == 201

    # 请购建议：缺 15.2 t，不足起订量 20 → 20；没有报价时按常用供应商
    assert client.get("/v1/purchase/suggestions", headers=h("warehouse")).status_code == 403
    s = client.get("/v1/purchase/suggestions", headers=h("purchaser")).json()
    (sg,) = s["suggestions"]
    assert (sg["material"], sg["qty"], sg["suggested_supplier"]) == ("M1", 20, "S2")
    assert "缺 15.2" in sg["reason"] and sg["need_date"] and any("起订量" in n for n in sg["notes"])

    # 报价：乙便宜但 30 天才到，赶不上；甲 3 天到 → 推荐甲
    q = {"material": "M1", "unit": "吨", "quoted_at": at()[:10]}
    ev(
        client,
        h,
        "purchase.quote_recorded",
        q | {"supplier": "S1", "unit_price": 800, "lead_days": 3},
        "Q1",
        who="purchaser",
    )
    ev(
        client,
        h,
        "purchase.quote_recorded",
        q | {"supplier": "S2", "unit_price": 750, "lead_days": 30},
        "Q2",
        who="purchaser",
    )
    old = q | {"supplier": "S1", "unit_price": 500, "quoted_at": at(-60)[:10], "valid_until": at(-30)[:10]}
    ev(client, h, "purchase.quote_recorded", old, "Q0", who="purchaser")
    c = client.get(
        f"/v1/purchase/compare?material=M1&qty=20&need_date={sg['need_date']}", headers=h("purchaser")
    ).json()
    assert c["recommended"] == "Q1" and "取最快到货" in c["reason"]  # 计划今天就要，都赶不上
    c5 = client.get(f"/v1/purchase/compare?material=M1&qty=20&need_date={at(5)[:10]}", headers=h("purchaser"))
    assert c5.json()["recommended"] == "Q1" and "单价最低" in c5.json()["reason"]
    c40 = client.get(
        f"/v1/purchase/compare?material=M1&qty=20&need_date={at(40)[:10]}", headers=h("purchaser")
    )
    assert c40.json()["recommended"] == "Q2"  # 都来得及就取最便宜
    assert {r["quote"] for r in c["quotes"]} == {"Q1", "Q2"} and any("过有效期" in n for n in c["notes"])
    assert next(r for r in c["quotes"] if r["quote"] == "Q1")["amount"] == 16000
    sg = client.get("/v1/purchase/suggestions", headers=h("purchaser")).json()["suggestions"][0]
    assert (sg["suggested_supplier"], sg["quote"]) == ("S1", "Q1")

    # 生成请购单 → 建议里扣掉，不重复请购
    line = {k: sg[k] for k in ("material", "qty", "need_date", "source", "reason", "suggested_supplier")}
    assert (
        client.post("/v1/purchase/requisitions", json={"lines": [line]}, headers=h("planner")).status_code
        == 403
    )
    r = client.post("/v1/purchase/requisitions", json={"lines": [line]}, headers=h("purchaser"))
    assert r.status_code == 201, r.text
    rid = r.json()["events"][0]["object_id"]
    assert client.get("/v1/purchase/suggestions", headers=h("purchaser")).json()["suggestions"] == []

    # 采购单：金额由代码算 20 × 800 = 16000；请购单变为已下单，不能再下
    po = {
        "supplier": "S1",
        "material": "M1",
        "qty": 20,
        "unit_price": 800,
        "requisitions": [rid],
        "quote": "Q1",
    }
    r = client.post("/v1/purchase/orders", json=po, headers=h("purchaser"))
    assert r.status_code == 201, r.text
    out = r.json()
    poid = out["object_id"]
    assert out["amount"] == 16000 and any("交货周期 3 天" in w for w in out["warnings"])
    reqs = client.get("/v1/purchase/requisitions", headers=h("purchaser")).json()["requisitions"]
    assert reqs[0]["stage"] == "ordered" and reqs[0]["purchase_order"] == poid
    assert client.post("/v1/purchase/orders", json=po, headers=h("purchaser")).status_code == 409

    # 大额采购单（> 5 万）走总经理审批
    big = {"supplier": "S2", "material": "M1", "qty": 100, "unit_price": 750}
    r = client.post("/v1/purchase/orders", json=big, headers=h("purchaser"))
    assert r.status_code == 202 and r.json()["amount"] == 75000

    # 计划物料：采购单 20 t 按交期计入在途；今天就要用，3 天后才到，缺口还在（如实反映）
    mp = client.get("/v1/planning/materials", headers=h("planner")).json()
    assert mp["materials"][0]["on_order"] == 20 and mp["materials"][0]["shortage"] == 15.2

    # 按单到货：仓管登记，批次待检；在途从采购单挪到待检，不重复算
    rc = {"qty": 12, "supplier_lot_no": "GY-01", "location": "B-02", "evidence": PHOTO}
    r = client.post(f"/v1/purchase/orders/{poid}/receipts", json=rc, headers=h("warehouse"))
    assert r.status_code == 201, r.text
    assert r.json()["order"]["received"] == 12 and r.json()["order"]["remaining"] == 8
    mp = client.get("/v1/planning/materials", headers=h("planner")).json()["materials"][0]
    assert (mp["incoming"], mp["on_order"]) == (12, 8)
    s = stock_of(client, h, "M1", who="purchaser")
    assert s["incoming"] == 12 and s["usable"] == 10

    # 超订：12 + 10 > 20 × 1.05；直接写事件、物料不一致也被守卫挡住
    r = client.post(f"/v1/purchase/orders/{poid}/receipts", json=rc | {"qty": 10}, headers=h("warehouse"))
    assert r.status_code == 409 and "超订" in r.text
    ev(client, h, "warehouse.material_defined", {"name": "硅微粉", "category": "raw", "unit": "t"}, "M2")
    bad = {"material": "M2", "qty": 1, "unit": "t", "received_at": at(), "purchase_order": poid}
    ev(client, h, "warehouse.lot_received", bad, who="warehouse", expect=(422,))
    r = client.post(f"/v1/purchase/orders/{poid}/receipts", json=rc | {"qty": 8}, headers=h("warehouse"))
    assert r.status_code == 201
    assert any("请采购" in n for n in r.json()["notes"])  # 仓管没有采购单权限，提示采购关单
    rows = client.get("/v1/purchase/orders?status=open", headers=h("purchaser")).json()["orders"]
    assert next(o for o in rows if o["id"] == poid)["remaining"] == 0

    # 质检判定后算供应商评分：甲 2 批，1 合格 1 不合格 → 合格率 50%；按期到 → 准时率 100%
    lots = r.json()["order"]["lots"]
    ev(client, h, "quality.lot_dispositioned", {"status": "accepted"}, lots[0], who="gm")
    ev(client, h, "quality.lot_dispositioned", {"status": "rejected"}, lots[1], who="gm")
    sup = {
        x["supplier"]: x
        for x in client.get("/v1/purchase/suppliers", headers=h("purchaser")).json()["suppliers"]
    }
    assert (sup["S1"]["quality_rate"], sup["S1"]["on_time_rate"], sup["S1"]["score"]) == (50, 100, 70)
    assert sup["S2"]["score"] is None

    # 取消请购单
    r = client.post("/v1/purchase/requisitions", json={"lines": [line | {"qty": 5}]}, headers=h("purchaser"))
    rid2 = r.json()["events"][0]["object_id"]
    assert (
        client.post(
            f"/v1/purchase/requisitions/{rid}/cancel", json={"note": "x"}, headers=h("purchaser")
        ).status_code
        == 409
    )
    r = client.post(
        f"/v1/purchase/requisitions/{rid2}/cancel", json={"note": "计划变更"}, headers=h("purchaser")
    )
    assert r.status_code == 200, r.text


def test_supply_im_and_tools(client, h):
    ev(
        client,
        h,
        "warehouse.material_defined",
        {"name": "高铝矾土", "category": "raw", "unit": "t", "safety_stock": 5},
        "M1",
    )
    ev(client, h, "warehouse.material_defined", {"name": "硅微粉", "category": "raw", "unit": "t"}, "M2")
    lot(client, h, "L1", "M1", 3, "accepted", location="A-01")
    for who, ext in (("warehouse", "im-wh"), ("worker", "im-w"), ("purchaser", "im-buy")):
        bind(client, h, who, ext)

    def say(i, text, sender="im-wh"):
        body = {"external_msg_id": f"s{i}", "sender_id": sender, "kind": "text", "text": text}
        return client.post("/v1/channels/mock/messages", json=body, headers=h("admin")).json()

    out = say(1, "库存")
    assert out["route"] == "supply_stock" and out["replies"][0]["card"]["title"] == "库存预警"
    assert out["replies"][0]["card"]["fields"][0]["label"] == "高铝矾土"
    out = say(2, "库存 高铝矾土")
    card = out["replies"][0]["card"]
    assert card["title"] == "库存 高铝矾土" and "A-01" in card["fields"][0]["value"]
    assert "没有找到" in say(3, "库存 碳化硅")["replies"][0]["text"]
    assert say(4, "库存还有多少高铝矾土？")["route"] != "supply_stock"  # 像一句话，交给 Agent
    assert "没有查看库存的权限" in say(5, "库存", sender="im-w")["replies"][0]["text"]
    assert "没有查看请购的权限" in say(6, "请购建议")["replies"][0]["text"]
    out = say(7, "请购建议", sender="im-buy")
    card = out["replies"][0]["card"]
    assert out["route"] == "supply_suggest" and card["fields"][0]["label"] == "高铝矾土"
    assert "请购 2" in card["fields"][0]["value"]  # 安全库存 5 − 现存 3

    # 采购员 Skill 的只读工具：权限 = Skill 上限 ∩ 提问人
    from fos.governance.principal import Principal
    from fos.tools.registry import REGISTRY, ToolContext, ToolError

    svc = client.app.state.services
    tenant = svc.registry.get("luneng")
    skill = svc.skills["luneng"]["purchaser"]

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

    out = client.portal.call(run, ["purchaser"], "stock.query", {"material": "高铝矾土"})
    assert out["materials"][0]["usable"] == 3
    out = client.portal.call(run, ["purchaser"], "purchase.suggestions", {})
    assert out["suggestions"][0]["qty"] == 2
    out = client.portal.call(run, ["purchaser"], "purchase.compare", {"material": "高铝矾土", "qty": 2})
    assert out["recommended"] is None and out["notes"]
    with pytest.raises(ToolError):
        client.portal.call(run, ["worker"], "stock.query", {})
