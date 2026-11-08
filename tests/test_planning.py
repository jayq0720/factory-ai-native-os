"""W16 计划与排窑：求解器、排窑建议 → 确认（账本守卫）→ 承诺交期、物料缺口、交期评估、IM、主动推送。"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fos.planning.config import SolverConfig
from fos.planning.solver import KilnIn, PieceIn, Problem, solve, to_int

PHOTO = [{"kind": "photo", "ref": "oss://evidence/1.jpg"}]
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


def today() -> date:
    return datetime.now(TZ).date()


def ev(client, h, type_, payload, object_id=None, who="admin", expect=(201,)):
    body = {"type": type_, "payload": payload, "evidence": PHOTO}
    if object_id:
        body["object_id"] = object_id
    r = client.post("/v1/events", json=body, headers=h(who))
    assert r.status_code in expect, r.text
    return r.json()


def setup_plant(client, h):
    ev(client, h, "sales.customer_registered", {"name": "安徽某钢铁"}, "C1")
    for pid, code, temp, group in (("P1", "QG-1.0", 1300, "低温隔热"), ("P2", "GL-75", 1550, None)):
        ev(client, h, "sales.product_defined", {"code": code, "name": code, "unit": "t"}, pid)
        fire = {"firing_temp": temp} | ({"firing_group": group} if group else {})
        ev(client, h, "pmc.product_firing_set", fire, pid, who="planner")
    ev(client, h, "sales.product_defined", {"code": "QG-0.8", "name": "QG-0.8", "unit": "t"}, "P3")
    ev(
        client,
        h,
        "pmc.product_firing_set",
        {"firing_temp": 1250, "firing_group": "低温隔热"},
        "P3",
        who="planner",
    )
    ev(
        client,
        h,
        "production.kiln_registered",
        {"name": "1号梭式窑", "kiln_type": "shuttle", "access_path": "manual", "max_temp": 1400},
        "K1",
    )
    ev(
        client,
        h,
        "production.kiln_registered",
        {"name": "2号隧道窑", "kiln_type": "tunnel", "access_path": "retrofit", "max_temp": 1650},
        "K2",
    )
    ev(
        client,
        h,
        "production.kiln_registered",
        {"name": "3号窑", "kiln_type": "shuttle", "access_path": "manual"},
        "K3",
    )
    cap = {"capacity_unit": "t", "plan_status": "available"}
    ev(client, h, "pmc.kiln_capacity_set", {"capacity": 20, "cycle_hours": 48, **cap}, "K1", who="planner")
    ev(client, h, "pmc.kiln_capacity_set", {"capacity": 10, "cycle_hours": 24, **cap}, "K2", who="planner")
    ev(
        client,
        h,
        "pmc.kiln_capacity_set",
        {"capacity": 30, "plan_status": "maintenance"},
        "K3",
        who="planner",
    )
    # 配方用量：QG-1.0 每吨用 0.8 t 原料 M1，损耗 5%
    ev(client, h, "warehouse.material_defined", {"name": "高铝矾土", "category": "raw", "unit": "t"}, "M1")
    ev(client, h, "rnd.recipe_released", {"code": "R-QG", "version": 1, "product": "P1"}, "R1", who="rnd")
    ev(
        client,
        h,
        "rnd.recipe_material_set",
        {"recipe": "R1", "material": "M1", "qty_per_unit": 0.8, "unit": "吨", "loss_pct": 5},
        "RM1",
        who="rnd",
    )
    now = datetime.now(TZ).isoformat()
    ev(
        client,
        h,
        "warehouse.lot_received",
        {"material": "M1", "qty": 10, "unit": "t", "received_at": now, "status": "accepted"},
        "L1",
    )


def order(client, h, oid, product, qty, due_days):
    ev(
        client,
        h,
        "sales.order_created",
        {
            "customer": "C1",
            "product": product,
            "qty": qty,
            "unit": "t",
            "status": "open",
            "order_no": oid,
            "due_date": (today() + timedelta(days=due_days)).isoformat(),
        },
        oid,
        who="sales",
    )


def test_solver_packs_and_respects_constraints():
    ks = [KilnIn("A", to_int(20), 48), KilnIn("B", to_int(10), 24, available_from=10)]
    ps = [
        PieceIn("o1#new", "o1", "g1", to_int(35), ("A", "B"), due=96),
        PieceIn("o2#new", "o2", "g2", to_int(15), ("A", "B"), release=48, due=200),
        PieceIn("o3#new", "o3", "g1", to_int(5), ("A",), due=300),
        PieceIn("o4#new", "o4", "g3", to_int(5), ("B",), due=300, firing_hours=500),  # 计划期内烧不完
    ]
    s = solve(Problem(ks, ps, 336, 48, 60, SolverConfig(time_limit_s=5, workers=1)))
    assert s.status == "optimal"
    assert s.short == {"o4#new": to_int(5)}
    for kiln in ("A", "B"):
        mine = sorted((r for r in s.runs if r.kiln == kiln), key=lambda r: r.start)
        assert all(a.end <= b.start for a, b in itertools.pairwise(mine))  # 同一窑不重叠
    for r in s.runs:
        cap = to_int(20) if r.kiln == "A" else to_int(10)
        assert sum(r.loads.values()) <= cap
        assert r.start >= (10 if r.kiln == "B" else 0)
        if "o2#new" in r.loads:
            assert r.start >= 48 and r.group == "g2"
        if "o3#new" in r.loads:
            assert r.kiln == "A"
    assert sum(v for r in s.runs for k, v in r.loads.items() if k == "o1#new") == to_int(35)


def test_draft_confirm_promise_and_shortage(client, h):
    setup_plant(client, h)
    order(client, h, "SO-1", "P1", 30, 10)
    order(client, h, "SO-2", "P2", 15, 6)
    order(client, h, "SO-3", "P3", 8, 12)
    # 采购绑定 IM：确认计划后收到缺料提醒
    r = client.put(
        "/v1/channels/identities",
        json={"channel": "mock", "external_id": "im-buy", "login": "purchaser"},
        headers=h("admin"),
    )
    assert r.status_code == 200

    assert client.post("/v1/planning/draft", headers=h("worker")).status_code == 403
    d = client.post("/v1/planning/draft", headers=h("planner")).json()
    assert d["solver"]["status"] == "optimal"
    assert any("3号窑 在维修" in w for w in d["warnings"])
    orders = {o["order"]: o for o in d["orders"]}
    assert all(o["short"] == 0 for o in orders.values()), orders
    assert orders["SO-1"]["ready_date"] and orders["SO-1"]["ok"]
    for run in d["runs"]:
        assert run["kiln"] in ("K1", "K2") and run["qty"] <= run["capacity"]
        if any(ld["order"] == "SO-2" for ld in run["loads"]):
            assert run["kiln"] == "K2"  # 1550℃ 只能进 1650℃ 的隧道窑
        products = {ld["product"] for ld in run["loads"]}
        assert products <= {"P1", "P3"} or products == {"P2"}  # 只有同烧成组能同窑
    # 物料：30 t × 0.8 × 1.05 = 25.2 t，现存 10 t → 缺 15.2 t；P2、P3 没有配方用量会提示
    m = d["materials"]["shortages"][0]
    assert (m["name"], m["need"], m["on_hand"], m["shortage"]) == ("高铝矾土", 25.2, 10.0, 15.2)
    assert any("没有配方用量" in w and "GL-75" in w for w in d["warnings"])

    body = {
        "horizon_start": d["horizon_start"],
        "horizon_end": d["horizon_end"],
        "runs": [
            {k: r[k] for k in ("kiln", "run_no", "start_at", "end_at")}
            | {"loads": [{"order": ld["order"], "qty": ld["qty"]} for ld in r["loads"]]}
            for r in d["runs"]
        ],
        "promise": ["SO-1", "SO-2"],
        "solver_status": d["solver"]["status"],
    }
    assert client.post("/v1/planning/plans", json=body, headers=h("sales")).status_code == 403
    r = client.post("/v1/planning/plans", json=body, headers=h("planner"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert {p["order"] for p in out["promised"]} == {"SO-1", "SO-2"} and out["superseded"] is None
    assert out["notified"] == 1 and out["shortages"][0]["shortage"] == 15.2
    sent = client.app.state.mock_channel.outbox
    assert any(m["type"] == "text" and "高铝矾土" in m["text"] for m in sent)
    so1 = client.get("/v1/objects/sales_order/SO-1", headers=h("admin")).json()
    assert so1["state"]["promised_date"] == orders["SO-1"]["ready_date"]

    cur = client.get("/v1/planning/plan", headers=h("planner")).json()
    assert cur["plan"]["id"] == out["plan_id"] and len(cur["runs"]) == len(d["runs"])
    mats = client.get("/v1/planning/materials", headers=h("purchaser")).json()
    assert mats["shortages"][0]["material"] == "M1"

    # 账本守卫：超装、窑温不够、维修中的窑都拒绝，且整批回滚（当前计划不变）
    start = datetime.fromisoformat(d["horizon_start"])

    def one(kiln, oid, qty):
        return {
            "horizon_start": d["horizon_start"],
            "horizon_end": d["horizon_end"],
            "runs": [
                {
                    "kiln": kiln,
                    "start_at": start.isoformat(),
                    "end_at": (start + timedelta(hours=48)).isoformat(),
                    "loads": [{"order": oid, "qty": qty}],
                }
            ],
        }

    for kiln, oid, qty, why in (
        ("K1", "SO-1", 25, "超过每窑装量"),
        ("K1", "SO-2", 5, "超过窑炉"),
        ("K3", "SO-1", 5, "维修"),
    ):
        r = client.post("/v1/planning/plans", json=one(kiln, oid, qty), headers=h("planner"))
        assert r.status_code == 422 and why in r.text, r.text
    assert client.get("/v1/planning/plan", headers=h("planner")).json()["plan"]["id"] == out["plan_id"]
    # 同一窑次里两个不同烧成组的产品
    mixed = one("K2", "SO-1", 4)
    mixed["runs"][0]["loads"].append({"order": "SO-2", "qty": 4})
    r = client.post("/v1/planning/plans", json=mixed, headers=h("planner"))
    assert r.status_code == 422 and "烧成组" in r.text

    # 新版本替代旧版本
    r = client.post("/v1/planning/plans", json=one("K1", "SO-1", 20), headers=h("planner"))
    assert r.status_code == 201 and r.json()["superseded"] == out["plan_id"]
    old = client.get(f"/v1/objects/kiln_plan/{out['plan_id']}", headers=h("admin")).json()
    assert old["state"]["status"] == "superseded"


def test_evaluate_im_and_watch(client, h):
    setup_plant(client, h)
    order(client, h, "SO-1", "P1", 30, 10)
    order(client, h, "SO-2", "P2", 15, 6)

    # 已有订单：能按期
    e = client.post("/v1/planning/evaluate", json={"order": "SO-1"}, headers=h("sales")).json()
    assert e["ok"] and e["target"]["order_no"] == "SO-1" and "能赶上交期" in e["verdict"]
    # 假设的大单：3 天交 200 t 排不下（也赶不上）
    big = {"candidate": {"product": "P1", "qty": 200, "due_date": (today() + timedelta(days=3)).isoformat()}}
    e = client.post("/v1/planning/evaluate", json=big, headers=h("sales")).json()
    assert not e["ok"] and e["target"]["order_no"] == "新订单"
    assert e["target"]["short"] > 0 or e["target"]["late_days"] > 0
    assert any(s["material"] == "M1" for s in e["shortages"])
    r = client.post("/v1/planning/evaluate", json={}, headers=h("sales"))
    assert r.status_code == 422

    # IM 快捷查询
    for who, ext in (("planner", "im-p"), ("worker", "im-w")):
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": ext, "login": who},
            headers=h("admin"),
        )

    def say(i, text, sender="im-p"):
        body = {"external_msg_id": f"p{i}", "sender_id": sender, "kind": "text", "text": text}
        return client.post("/v1/channels/mock/messages", json=body, headers=h("admin")).json()

    out = say(1, "排窑")
    assert out["route"] == "planning_plan" and "还没有确认" in out["replies"][0]["card"]["text"]
    out = say(2, "@助手 交期评估 SO-2")
    assert out["route"] == "planning_evaluate"
    assert out["replies"][0]["card"]["title"] == "交期评估 SO-2"
    out = say(3, "缺料")
    assert "还没有确认" in out["replies"][0]["text"]
    out = say(4, "排窑", sender="im-w")
    assert "没有查看排窑计划的权限" in out["replies"][0]["text"]

    # 新订单自动评估，推给计划员；每个订单只推一次
    from fos.planning.notify import evaluate_new_orders

    svc = client.app.state.services
    mock = client.app.state.mock_channel
    before = len(mock.outbox)
    done = client.portal.call(evaluate_new_orders, svc, "luneng", mock)
    assert set(done) == {"SO-1", "SO-2"}
    cards = [m for m in mock.outbox[before:] if m["type"] == "card"]
    assert {c["card"]["title"] for c in cards} == {"交期评估 SO-1", "交期评估 SO-2"}
    assert all(c["to"]["user_id"] == "im-p" for c in cards)
    assert client.portal.call(evaluate_new_orders, svc, "luneng", mock) == []


def test_planner_tools(client, h):
    from fos.governance.principal import Principal
    from fos.tools.registry import REGISTRY, ToolContext, ToolError

    setup_plant(client, h)
    order(client, h, "SO-1", "P1", 30, 10)
    svc = client.app.state.services
    tenant = svc.registry.get("luneng")
    skill = svc.skills["luneng"]["kiln_planner"]

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

    out = client.portal.call(run, ["planner"], "planning.evaluate", {"order": "SO-1"})
    assert out["ok"]
    out = client.portal.call(run, ["sales"], "planning.evaluate", {"product": "QG-1.0", "qty": 5})
    assert out["target"]["order_no"] == "新订单"
    assert client.portal.call(run, ["planner"], "planning.current", {})["plan"] is None
    with pytest.raises(ToolError):
        client.portal.call(run, ["worker"], "planning.shortages", {})
