"""Policy（审批规则、红线）、审批单、待办与统一收件箱（W8）。"""

from __future__ import annotations

import asyncio
import textwrap
from datetime import timedelta

import pytest
from pydantic import ValidationError

from fos.ontology.loader import load_ontology
from fos.policy import PolicyError, load_policy
from fos.policy.schema import Condition, PolicyLayer, Rule
from fos.tenancy import TenantRegistry
from tests.conftest import REPO

# ---------------- 规则本身 ----------------


def test_condition_matching():
    gt = Condition(field="amount", op="gt", value=50000)
    assert gt.matches({"amount": 50001}, None)
    assert not gt.matches({"amount": 50000}, None)
    assert not gt.matches({"amount": "很多"}, None)  # 不是数字：不命中
    assert not gt.matches({}, None)
    assert gt.matches({}, {"amount": 60000})  # payload 没有就看对象当前状态
    assert not gt.matches({"amount": 10}, {"amount": 60000})  # payload 优先（变更后的值）
    assert Condition(field="status", op="in", value=["a", "b"]).matches({"status": "b"}, None)
    assert Condition(field="status", op="not_in", value=["a"]).matches({"status": "b"}, None)
    assert Condition(field="note", op="exists").matches({"note": "x"}, None)
    assert Condition(field="note", op="exists", value=False).matches({"note": ""}, None)
    with pytest.raises(ValidationError):
        Condition(field="amount", op="gt", value="5万")
    with pytest.raises(ValidationError):
        Condition(field="status", op="in", value="a")


def test_rule_validation():
    with pytest.raises(ValidationError):
        Rule(name="x", events=["a.b"])  # approve 没有审批步骤
    with pytest.raises(ValidationError):
        Rule(name="x", events=["a.b"], effect="deny")  # deny 没有理由
    with pytest.raises(ValidationError):
        Rule(name="x", events=["a.b"], steps=[{"name": "谁都行"}])
    with pytest.raises(ValidationError):
        Rule(name="x", events=["a.b"], steps=[{"name": "s", "permission": "*.*:approve"}])
    with pytest.raises(ValidationError):
        PolicyLayer.model_validate(
            {"rules": {"Bad-Id": {"name": "x", "events": ["a.b"], "effect": "deny", "reason": "r"}}}
        )


def _load(tmp_path, pack_yaml: str | None, tenant_yaml: str | None):
    registry = TenantRegistry.load(REPO / "tenants")
    tenant = registry.get("luneng")
    assert tenant is not None
    packs = tmp_path / "packs"
    (packs / "refractory").mkdir(parents=True)
    (packs / "refractory" / "ontology.yaml").write_text(
        (REPO / "packs/refractory/ontology.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    if pack_yaml:
        (packs / "refractory" / "policy.yaml").write_text(textwrap.dedent(pack_yaml), encoding="utf-8")
    tenants = tmp_path / "tenants"
    (tenants / "luneng").mkdir(parents=True)
    if tenant_yaml:
        (tenants / "luneng" / "policy.yaml").write_text(textwrap.dedent(tenant_yaml), encoding="utf-8")
    onto = load_ontology(tenant, packs_dir=packs, tenants_dir=tenants)
    return load_policy(tenant, packs_dir=packs, tenants_dir=tenants, ontology=onto)


PACK = """
rules:
  po_big:
    name: 大额采购
    events: [purchase.order_created]
    when: [{field: amount, op: gt, value: 100000}]
    steps: [{name: 总经理, role: gm}]
  po_any:
    name: 所有采购
    events: [purchase.order_created]
    steps: [{name: 总经理, role: gm}, {name: 财务, permission: finance.payment:approve}]
"""


def test_layers_override_disable_and_evaluate(tmp_path):
    p = _load(
        tmp_path,
        PACK,
        """
        disable: [po_any]
        rules:
          po_big:
            name: 大额采购（本厂 5 万）
            events: [purchase.order_created]
            when: [{field: amount, op: gt, value: 50000}]
            steps: [{name: 总经理, role: gm}]
          po_ceiling:
            name: 超过 200 万
            events: [purchase.order_created]
            when: [{field: amount, op: gt, value: 2000000}]
            effect: deny
            reason: 超过单笔上限
        """,
    )
    assert p.layers == ["pack:refractory", "tenant:luneng"]
    assert set(p.rules) == {"po_big", "po_ceiling"}
    assert p.evaluate("purchase.order_created", {"amount": 40000}, None).effect == "allow"
    d = p.evaluate("purchase.order_created", {"amount": 60000}, None)
    assert d.effect == "approve" and d.rule_ids == ["po_big"]
    d = p.evaluate("purchase.order_created", {"amount": 3000000}, None)
    assert d.effect == "deny" and d.reason == "超过单笔上限"
    assert p.evaluate("sales.order_created", {"amount": 3000000}, None).effect == "allow"

    # 两条规则同时命中：步骤串联，相同的步骤只保留一个
    both = _load(tmp_path / "b", PACK, None).evaluate("purchase.order_created", {"amount": 200000}, None)
    assert [s["name"] for s in both.steps()] == ["总经理", "财务"]


_STEP = "\n    steps: [{name: s, role: gm}]"
_PO = "events: [purchase.order_created]"


@pytest.mark.parametrize(
    ("rule", "msg"),
    [
        ("events: [nope.event]" + _STEP, "不存在"),
        (_PO + "\n    when: [{field: color, op: eq, value: 1}]" + _STEP, "不是"),
        (_PO + "\n    when: [{field: unit, op: gt, value: 1}]" + _STEP, "不是数字"),
        (_PO + "\n    when: [{field: status, op: eq, value: lost}]" + _STEP, "取值"),
        (_PO + "\n    steps: [{name: s, role: ceo}]", "角色"),
    ],
)
def test_policy_validation_errors(tmp_path, rule, msg):
    with pytest.raises(PolicyError, match=msg):
        _load(tmp_path, None, f"rules:\n  r1:\n    name: r\n    {rule}\n")


def test_disable_unknown_rule(tmp_path):
    with pytest.raises(PolicyError):
        _load(tmp_path, None, "disable: [ghost]\n")


# ---------------- API：审批流 ----------------


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


@pytest.fixture
def post(client, h):
    def _post(who: str, type_: str, payload: dict, expect: int = 201, **kw):
        r = client.post("/v1/events", json={"type": type_, "payload": payload, **kw}, headers=h(who))
        assert r.status_code == expect, r.text
        return r.json()

    return _post


@pytest.fixture
def master(post):
    post("admin", "purchase.supplier_registered", {"name": "淄博某氧化铝厂"}, object_id="S-1")
    post(
        "admin",
        "warehouse.material_defined",
        {"name": "氧化铝粉", "category": "raw", "unit": "t"},
        object_id="M-1",
    )
    post("admin", "sales.customer_registered", {"name": "某钢厂"}, object_id="C-1")
    post(
        "admin",
        "sales.product_defined",
        {"code": "LZ-48", "name": "轻质莫来石砖", "unit": "块"},
        object_id="P-1",
    )


def _po(amount: float) -> dict:
    return {"supplier": "S-1", "material": "M-1", "qty": 10, "unit": "t", "amount": amount}


def _decide(client, h, who, ap_id, decision="approve", expect=200, **kw):
    r = client.post(f"/v1/approvals/{ap_id}/decide", json={"decision": decision, **kw}, headers=h(who))
    assert r.status_code == expect, r.text
    return r.json()


def test_small_purchase_goes_straight_in_large_needs_gm(client, h, post, master):
    assert post("purchaser", "purchase.order_created", _po(30000), object_id="PO-1")["seq"]

    r = post("purchaser", "purchase.order_created", _po(80000), expect=202)
    ap = r["approval"]
    assert r["status"] == "pending_approval" and ap["status"] == "pending"
    assert ap["rules"][0]["id"] == "purchase_order_large" and "gm" in r["message"]
    obj_id = ap["request"]["object_id"]
    assert obj_id  # 审批期间就分配了对象 id
    assert client.get(f"/v1/objects/purchase_order/{obj_id}", headers=h("admin")).status_code == 404

    # 收件箱：总经理看得到，采购员（申请人）在“我提交的”里
    box = client.get("/v1/inbox", headers=h("gm")).json()
    assert box["counts"]["approval"] == 1 and box["items"][0]["id"] == ap["id"]
    assert any(f["label"] == "金额" and f["value"] == "80,000 元" for f in box["items"][0]["fields"])
    assert client.get("/v1/inbox", headers=h("purchaser")).json()["counts"]["approval"] == 0
    sent = client.get("/v1/inbox", params={"box": "sent"}, headers=h("purchaser")).json()["items"]
    assert sent[0]["id"] == ap["id"]

    _decide(client, h, "purchaser", ap["id"], expect=403)  # 不是审批人
    _decide(client, h, "sales_lead", ap["id"], expect=403)
    assert client.get(f"/v1/approvals/{ap['id']}", headers=h("worker")).status_code == 404

    out = _decide(client, h, "gm", ap["id"], comment="同意，注意到货检验")
    assert out["outcome"] == "approved" and out["approval"]["status"] == "approved"
    obj = client.get(f"/v1/objects/purchase_order/{obj_id}", headers=h("admin")).json()
    ev = obj["history"][-1]
    assert obj["state"]["amount"] == 80000
    assert ev["actor"]["name"] == "purchaser"  # 以申请人身份记账
    assert any(e["kind"] == "approval" and e["ref"] == f"approval:{ap['id']}" for e in ev["evidence"])

    _decide(client, h, "gm2", ap["id"], expect=409)  # 已经结束
    assert client.get("/v1/inbox", headers=h("gm")).json()["counts"]["approval"] == 0


def test_two_steps_reject_cancel_and_separation(client, h, post, master):
    order = {"customer": "C-1", "product": "P-1", "qty": 1000, "unit": "块", "amount": 600000}
    ev = [{"kind": "document", "ref": "contract-1"}]
    ap = post("sales", "sales.order_created", order, evidence=ev, expect=202)["approval"]
    assert [s["name"] for s in ap["steps"]] == ["销售负责人审批", "总经理审批"]

    _decide(client, h, "gm", ap["id"], expect=403)  # 还没到总经理这一级
    out = _decide(client, h, "sales_lead", ap["id"])
    assert out["outcome"] == "approved_step" and out["approval"]["current_step"] == 1
    assert client.get("/v1/inbox", headers=h("sales_lead")).json()["counts"]["approval"] == 0
    out = _decide(client, h, "gm", ap["id"])
    assert out["outcome"] == "approved"
    assert [d["name"] for d in out["approval"]["decisions"]] == ["sales_lead", "gm"]

    # 驳回
    ap2 = post("sales", "sales.order_created", order, evidence=ev, expect=202)["approval"]
    out = _decide(client, h, "sales_lead", ap2["id"], "reject", comment="价格低于底价")
    assert out["outcome"] == "rejected" and out["approval"]["status"] == "rejected"
    obj_id = ap2["request"]["object_id"]
    assert client.get(f"/v1/objects/sales_order/{obj_id}", headers=h("admin")).status_code == 404

    # 撤回：只有申请人
    ap3 = post("sales", "sales.order_created", order, evidence=ev, expect=202)["approval"]
    assert client.post(f"/v1/approvals/{ap3['id']}/cancel", headers=h("gm")).status_code == 403
    r = client.post(f"/v1/approvals/{ap3['id']}/cancel", headers=h("sales"))
    assert r.status_code == 200 and r.json()["approval"]["status"] == "cancelled"
    _decide(client, h, "sales_lead", ap3["id"], expect=409)

    # 申请人不能审批自己：总经理自己提交让步接收，只能由另一位总经理审批
    post("warehouse", "warehouse.lot_received",
         {"material": "M-1", "qty": 5, "unit": "t", "received_at": "2026-11-01T08:00:00+08:00"},
         object_id="L-1", evidence=[{"kind": "photo", "ref": "p1"}])  # fmt: skip
    ap4 = post("gm", "quality.lot_dispositioned", {"status": "concession"}, object_id="L-1", expect=202)[
        "approval"
    ]
    detail = client.get(f"/v1/approvals/{ap4['id']}", headers=h("gm")).json()
    assert detail["approvers"] == ["gm2"]
    _decide(client, h, "gm", ap4["id"], expect=403)
    _decide(client, h, "gm2", ap4["id"])
    state = client.get("/v1/objects/material_lot/L-1", headers=h("admin")).json()["state"]
    assert state["status"] == "concession"
    # 非让步的处置不需要审批
    post("gm", "quality.lot_dispositioned", {"status": "accepted"}, object_id="L-1")


def test_approval_expiry_audit_and_isolation(client, h, post, master, settings, keys):
    from sqlalchemy import update

    from fos.db.models import Approval, utcnow
    from fos.db.session import make_engine, make_sessionmaker

    ap = post("purchaser", "purchase.order_created", _po(90000), expect=202)["approval"]
    other = {"Authorization": f"Bearer {keys['other_admin']}"}
    assert client.get(f"/v1/approvals/{ap['id']}", headers=other).status_code == 404
    assert (
        client.post(
            f"/v1/approvals/{ap['id']}/decide", json={"decision": "approve"}, headers=other
        ).status_code
        == 404
    )
    assert client.get("/v1/approvals", headers=h("purchaser")).status_code == 403
    assert [a["id"] for a in client.get("/v1/approvals", headers=h("admin")).json()["items"]] == [ap["id"]]
    assert client.get("/v1/policy", headers=h("worker")).status_code == 403
    pol = client.get("/v1/policy", headers=h("gm")).json()
    assert "purchase_order_large" in pol["rules"] and pol["rules"]["purchase_order_large"]["when"] == [
        "amount > 50000"
    ]

    async def expire():
        engine = make_engine(settings.database_url)
        async with make_sessionmaker(engine)() as s, s.begin():
            await s.execute(
                update(Approval)
                .where(Approval.id == ap["id"])
                .values(expires_at=utcnow() - timedelta(hours=1))
            )
        await engine.dispose()

    asyncio.run(expire())
    assert client.get("/v1/inbox", headers=h("gm")).json()["counts"]["approval"] == 0
    _decide(client, h, "gm", ap["id"], expect=409)
    assert client.get(f"/v1/approvals/{ap['id']}", headers=h("purchaser")).json()["status"] == "expired"


@pytest.fixture
def deny_policy(tenants_dir):
    (tenants_dir / "other" / "policy.yaml").write_text(
        textwrap.dedent(
            """
            rules:
              po_ceiling:
                name: 单笔采购上限
                events: [purchase.order_created]
                when: [{field: amount, op: gt, value: 1000000}]
                effect: deny
                reason: 超过 100 万元需董事会决议
            """
        ),
        encoding="utf-8",
    )


def test_deny_is_a_red_line_even_for_admin(deny_policy, client, keys):
    hh = {"Authorization": f"Bearer {keys['other_admin']}"}
    for body in (
        {"type": "purchase.supplier_registered", "object_id": "S-1", "payload": {"name": "s"}},
        {
            "type": "warehouse.material_defined",
            "object_id": "M-1",
            "payload": {"name": "m", "category": "raw", "unit": "t"},
        },
    ):
        assert client.post("/v1/events", json=body, headers=hh).status_code == 201
    r = client.post(
        "/v1/events", json={"type": "purchase.order_created", "payload": _po(2_000_000)}, headers=hh
    )
    assert r.status_code == 403 and "董事会" in r.json()["detail"]
    r = client.post("/v1/events", json={"type": "purchase.order_created", "payload": _po(80000)}, headers=hh)
    assert r.status_code == 201  # other 租户没有 5 万的审批规则


# ---------------- IM：审批卡片、卡片确认转审批 ----------------


@pytest.fixture
def send(client, h):
    n = {"i": 0}

    def _send(sender: str, **kw):
        n["i"] += 1
        body = {"external_msg_id": f"pm{n['i']}", "sender_id": sender, **kw}
        r = client.post("/v1/channels/mock/messages", json=body, headers=h("admin"))
        assert r.status_code == 200, r.text
        return r.json()

    return _send


@pytest.fixture
def bound(client, h):
    for ext, login in (("im-gm", "gm"), ("im-gm2", "gm2"), ("im-buyer", "purchaser")):
        r = client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": ext, "login": login},
            headers=h("admin"),
        )
        assert r.status_code == 200


def _outbox(client, h):
    return client.get("/v1/channels/mock/outbox", headers=h("admin")).json()["items"]


def test_approval_cards_pushed_to_approvers_and_decided_in_im(client, h, post, master, bound, send):
    ap = post("purchaser", "purchase.order_created", _po(120000), expect=202)["approval"]
    cards = [o for o in _outbox(client, h) if o["type"] == "card"]
    assert sorted(c["to"]["user_id"] for c in cards) == ["im-gm", "im-gm2"]  # 单聊发给每位审批人
    assert all(c["to"]["conversation_type"] == "direct" for c in cards)
    assert {b["id"] for b in cards[0]["card"]["buttons"]} == {"approve", "reject"}
    by_user = {c["to"]["user_id"]: c for c in cards}

    # 别人拿到短码也点不了（卡片只允许本人确认）
    r = send("im-buyer", text=f"同意 {by_user['im-gm']['short_code']}", conversation_type="direct",
             conversation_id="im-buyer")  # fmt: skip
    assert r["result"]["outcome"] == "rejected"

    r = send("im-gm2", text=f"同意 {by_user['im-gm2']['short_code']}", conversation_type="direct",
             conversation_id="im-gm2")  # fmt: skip
    assert r["status"] == "done" and "记账" in r["replies"][0]["text"]
    a = client.get(f"/v1/approvals/{ap['id']}", headers=h("purchaser")).json()
    assert a["status"] == "approved" and a["decisions"][0]["via"] == "mock"
    # 另一位审批人的卡片失效
    c = client.get(f"/v1/cards/{by_user['im-gm']['card_id']}", headers=h("admin")).json()
    assert c["status"] == "superseded"
    click = {"card_id": by_user["im-gm"]["card_id"], "action": "approve"}
    r = send("im-gm", kind="card_action", action=click, conversation_type="direct", conversation_id="im-gm")
    assert r["result"]["outcome"] == "already"
    # 申请人收到结果通知
    texts = [o for o in _outbox(client, h) if o["type"] == "text" and o["to"]["user_id"] == "im-buyer"]
    assert "审批通过" in texts[-1]["text"]


def test_card_confirm_turns_into_approval(client, h, post, master, bound, send):
    body = {
        "to": {"channel": "mock", "conversation_id": "buy-group", "conversation_type": "group"},
        "card": {
            "title": "采购单：氧化铝粉 10 t",
            "buttons": [{"id": "confirm", "label": "确认"}, {"id": "cancel", "label": "取消"}],
        },
        "action": {
            "kind": "ledger.append",
            "request": {"type": "purchase.order_created", "payload": _po(70000)},
        },
    }
    card = client.post("/v1/cards", json=body, headers=h("admin")).json()
    # 卡片出现在采购员的收件箱（有 purchase.order:write），不在工人的
    assert any(i["id"] == card["id"] for i in client.get("/v1/inbox", headers=h("purchaser")).json()["items"])
    assert client.get("/v1/inbox", headers=h("worker")).json()["counts"]["card"] == 0

    r = send("im-buyer", text=f"确认 {card['short_code']}", conversation_id="buy-group")
    assert r["status"] == "done" and "需要审批" in r["replies"][0]["text"]
    ap_id = r["result"]["approval_id"]
    a = client.get(f"/v1/approvals/{ap_id}", headers=h("gm")).json()
    assert a["requester"]["name"] == "purchaser" and a["source"] == f"card:{card['id']}"
    assert client.get(f"/v1/cards/{card['id']}", headers=h("admin")).json()["status"] == "done"

    _decide(client, h, "gm", ap_id)
    obj = client.get(f"/v1/objects/purchase_order/{a['request']['object_id']}", headers=h("admin")).json()
    evidence = obj["history"][-1]["evidence"]
    assert {e["ref"] for e in evidence} >= {f"card:{card['id']}", f"approval:{ap_id}"}  # 两层确认都有证据


# ---------------- 待办 ----------------


def test_todos_in_inbox(client, h, bound):
    r = client.post(
        "/v1/todos", json={"title": "复查 3 号窑温控", "assignee_login": "purchaser"}, headers=h("worker")
    )
    assert r.status_code == 403
    r = client.post(
        "/v1/todos", json={"title": "x", "assignee_login": "gm", "assignee_role": "gm"}, headers=h("gm")
    )
    assert r.status_code == 422
    r = client.post("/v1/todos", json={"title": "x", "assignee_role": "ceo"}, headers=h("gm"))
    assert r.status_code == 422

    r = client.post(
        "/v1/todos",
        json={
            "title": "核对氧化铝粉供应商资质",
            "assignee_login": "purchaser",
            "due_at": "2026-11-05T17:00:00+08:00",
        },
        headers=h("gm"),
    )
    assert r.status_code == 201 and r.json()["notified"] == 1
    mine = r.json()
    assert "新待办：核对氧化铝粉供应商资质" in _outbox(client, h)[-1]["text"]

    role = client.post(
        "/v1/todos", json={"title": "本周巡检", "assignee_role": "gm"}, headers=h("shift_lead")
    ).json()
    assert role["notified"] == 2  # 两位总经理都绑定了 IM

    box = client.get("/v1/inbox", headers=h("purchaser")).json()
    assert box["counts"]["todo"] == 1 and box["items"][0]["actions"] == ["done", "dismiss"]
    assert client.get("/v1/inbox", headers=h("gm2")).json()["counts"]["todo"] == 1

    assert client.post(f"/v1/todos/{mine['id']}/close", json={}, headers=h("worker")).status_code == 403
    r = client.post(f"/v1/todos/{mine['id']}/close", json={"note": "已核对"}, headers=h("purchaser"))
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert client.post(f"/v1/todos/{mine['id']}/close", json={}, headers=h("purchaser")).status_code == 409
    assert client.get("/v1/inbox", headers=h("purchaser")).json()["counts"]["todo"] == 0
    r = client.post(f"/v1/todos/{role['id']}/close", json={"status": "dismissed"}, headers=h("gm2"))
    assert r.json()["status"] == "dismissed"


@pytest.mark.skipif(
    not (__import__("os").environ.get("FOS_TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="需要 FOS_TEST_DATABASE_URL 指向 PostgreSQL",
)
def test_concurrent_decisions_only_one_wins(client, h, post, master, settings):
    """两位总经理同时点同意：只有一个生效，事件只记一次。"""
    from sqlalchemy import func, select

    from fos.db.models import Approval, Event, User
    from fos.governance.principal import principal_for_user
    from fos.services import Services

    ap_id = post("purchaser", "purchase.order_created", _po(99000), expect=202)["approval"]["id"]

    async def run():
        svc = Services.build(settings)
        tenant = svc.registry.get("luneng")

        async def one(login: str):
            async with svc.sessionmaker() as s, s.begin():
                u = (await s.scalars(select(User).where(User.login == login))).one()
                ap = await s.get(Approval, ap_id)
                return (await svc.approvals.decide(s, principal_for_user(u, tenant), ap, "approve")).status

        try:
            res = await asyncio.gather(one("gm"), one("gm2"))
            async with svc.sessionmaker() as s:
                n = await s.scalar(
                    select(func.count()).select_from(Event).where(Event.type == "purchase.order_created")
                )
            return sorted(res), n
        finally:
            await svc.aclose()

    statuses, n = asyncio.run(run())
    assert statuses == ["approved", "closed"] and n == 1
