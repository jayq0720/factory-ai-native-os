"""事件账本端到端：用六安绿能的真实角色走一遍 来料 → 检验 → 配料 → 烧成 → 出厂检验 → 追溯。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from fos.db.models import ObjectState
from fos.db.session import make_engine, make_sessionmaker
from fos.ledger.projection import rebuild
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry

T0 = "2026-11-02T08:00:00+08:00"
PHOTO = [{"kind": "photo", "ref": "oss://luneng/im/2026-11-02/0001.jpg", "note": "送货单"}]
REPORT = [{"kind": "document", "ref": "doc:JC-2026-1101", "page": 1, "quote": "体积密度 0.79"}]


@pytest.fixture
def post(client, keys):
    def _post(who: str, type_: str, payload: dict, *, object_id: str | None = None, expect: int = 201, **kw):
        body = {"type": type_, "payload": payload, **kw}
        if object_id:
            body["object_id"] = object_id
        r = client.post("/v1/events", json=body, headers={"Authorization": f"Bearer {keys[who]}"})
        assert r.status_code == expect, r.text
        return r.json()

    return _post


@pytest.fixture
def get(client, keys):
    def _get(who: str, path: str, expect: int = 200):
        r = client.get(path, headers={"Authorization": f"Bearer {keys[who]}"})
        assert r.status_code == expect, r.text
        return r.json()

    return _get


@pytest.fixture
def story(post):
    """主数据 + 一条完整的生产链。返回创建的对象 id。"""
    post("admin", "purchase.supplier_registered", {"name": "某某矿业（示例）"}, object_id="S-KY")
    post(
        "admin",
        "warehouse.material_defined",
        {"name": "矾土熟料 88#", "category": "raw", "unit": "t"},
        object_id="M-AL88",
    )
    post(
        "admin",
        "sales.product_defined",
        {"code": "LN-ML26", "name": "莫来石轻质隔热砖", "unit": "块"},
        object_id="P-ML26",
    )
    post("admin", "sales.customer_registered", {"name": "华东某陶瓷有限公司（示例）"}, object_id="C-HD")
    post(
        "admin",
        "production.kiln_registered",
        {"name": "1# 隧道窑", "kiln_type": "tunnel", "access_path": "direct", "max_temp": 1450},
        object_id="K1",
    )

    post(
        "sales",
        "sales.order_created",
        {
            "customer": "C-HD",
            "product": "P-ML26",
            "qty": 38770,
            "unit": "块",
            "due_date": "2026-11-25",
            "status": "open",
        },
        object_id="SO-0412",
        evidence=[{"kind": "document", "ref": "doc:contract-0412", "page": 2}],
    )
    post(
        "warehouse",
        "warehouse.lot_received",
        {
            "material": "M-AL88",
            "supplier": "S-KY",
            "supplier_lot_no": "KF-1019",
            "qty": 32.46,
            "unit": "t",
            "received_at": T0,
            "purchase_order": "PO-0371",
            "status": "pending_inspection",
        },
        object_id="L-KF1019",
        evidence=PHOTO,
    )
    post(
        "qc",
        "quality.inspection_recorded",
        {"kind": "incoming", "material_lot": "L-KF1019", "inspected_at": T0, "result": "pass"},
        object_id="I-IN-1019",
        evidence=REPORT,
    )
    post("gm", "quality.lot_dispositioned", {"status": "accepted"}, object_id="L-KF1019")
    post(
        "rnd",
        "rnd.recipe_released",
        {"code": "R-ML26", "version": 7, "product": "P-ML26"},
        object_id="R-ML26-v7",
        evidence=[{"kind": "approval", "ref": "approval:rnd-77"}],
    )
    post(
        "worker",
        "production.batch_started",
        {
            "product": "P-ML26",
            "order": "SO-0412",
            "material_lots": ["L-KF1019"],
            "planned_qty": 9000,
            "unit": "块",
            "started_at": T0,
            "status": "in_progress",
        },
        object_id="B2611-03",
    )
    post(
        "worker",
        "production.batch_formed",
        {"recipe": "R-ML26-v7", "forming": "pressing"},
        object_id="B2611-03",
    )
    post(
        "shift_lead",
        "production.kiln_run_started",
        {"kiln": "K1", "run_no": "1031-14", "started_at": T0, "status": "firing"},
        object_id="KR-1031-14",
    )
    post("worker", "production.batch_fired", {"kiln_run": "KR-1031-14"}, object_id="B2611-03")
    post(
        "shift_lead",
        "production.kiln_run_finished",
        {"ended_at": "2026-11-03T02:00:00+08:00", "max_temp": 1305, "deviation": True, "status": "finished"},
        object_id="KR-1031-14",
        evidence=[{"kind": "sensor", "ref": "kiln1.zone18.temp"}],
    )
    post(
        "qc",
        "quality.inspection_recorded",
        {
            "kind": "final",
            "batch": "B2611-03",
            "inspected_at": "2026-11-03T09:00:00+08:00",
            "result": "pass",
            "report_no": "JC-2026-1101",
        },
        object_id="I-FN-0311",
        evidence=REPORT,
    )
    post(
        "qc",
        "quality.final_inspection_recorded",
        {"bulk_density": 0.79, "cold_crushing_strength": 3.1},
        object_id="I-FN-0311",
        evidence=REPORT,
    )
    post(
        "worker",
        "production.batch_completed",
        {"good_qty": 8820, "completed_at": "2026-11-03T10:00:00+08:00", "status": "completed"},
        object_id="B2611-03",
        evidence=[{"kind": "photo", "ref": "oss://luneng/report/b2611-03.jpg"}],
    )


def test_story_projection_and_history(story, get):
    b = get("admin", "/v1/objects/production_batch/B2611-03")
    assert b["version"] == 4
    assert b["state"]["kiln_run"] == "KR-1031-14"
    assert b["state"]["recipe"] == "R-ML26-v7"
    assert b["state"]["good_qty"] == 8820
    assert [h["type"] for h in b["history"]] == [
        "production.batch_started",
        "production.batch_formed",
        "production.batch_fired",
        "production.batch_completed",
    ]
    assert b["history"][0]["actor"]["name"] == "worker"
    lot = get("admin", "/v1/objects/material_lot/L-KF1019")
    assert lot["state"]["status"] == "accepted"
    assert lot["history"][0]["evidence"][0]["ref"].startswith("oss://")

    ev = get("admin", "/v1/events?limit=500")["events"]
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))  # 租户内连续序号


def test_trace_batch_reaches_whole_chain(story, get):
    t = get("admin", "/v1/objects/production_batch/B2611-03/trace?depth=3")
    found = {(n["object_type"], n["object_id"]) for n in t["nodes"]}
    for node in [
        ("material_lot", "L-KF1019"),
        ("material", "M-AL88"),
        ("supplier", "S-KY"),
        ("inspection", "I-IN-1019"),  # 原料的来料检验（经由原料批）
        ("inspection", "I-FN-0311"),  # 本批出厂检验（反向引用）
        ("kiln_run", "KR-1031-14"),
        ("kiln", "K1"),
        ("recipe", "R-ML26-v7"),
        ("sales_order", "SO-0412"),
        ("customer", "C-HD"),
    ]:
        assert node in found, node
    assert {
        "from_type": "production_batch",
        "from_id": "B2611-03",
        "rel": "material_lots",
        "to_type": "material_lot",
        "to_id": "L-KF1019",
    } in t["edges"]


def test_trace_redacts_what_caller_cannot_read(story, get):
    t = get("worker", "/v1/objects/production_batch/B2611-03/trace?depth=2")
    nodes = {(n["object_type"], n["object_id"]): n for n in t["nodes"]}
    assert nodes[("kiln_run", "KR-1031-14")]["state"]["max_temp"] == 1305  # 生产域可读
    assert nodes[("sales_order", "SO-0412")]["redacted"] is True  # 销售订单对工人隐去
    assert nodes[("sales_order", "SO-0412")]["state"] is None
    get("worker", "/v1/objects/sales_order/SO-0412", expect=403)
    worker_events = get("worker", "/v1/events?limit=500")["events"]
    assert {e["object_type"] for e in worker_events} <= {"production_batch", "kiln", "kiln_run"}


def test_permissions_and_evidence(story, post):
    post(
        "worker",
        "warehouse.lot_received",
        {"material": "M-AL88", "qty": 1, "unit": "t", "received_at": T0},
        evidence=PHOTO,
        expect=403,
    )
    post(
        "warehouse",
        "warehouse.lot_received",
        {"material": "M-AL88", "qty": 1, "unit": "t", "received_at": T0},
        expect=422,
    )  # 缺证据
    post("qc", "quality.lot_dispositioned", {"status": "rejected"}, object_id="L-KF1019", expect=403)


def test_validation_errors(story, post):
    base = {"material": "M-AL88", "qty": 1, "unit": "t", "received_at": T0}
    post("warehouse", "warehouse.lot_received", {**base, "material": "M-NOPE"}, evidence=PHOTO, expect=422)
    post("warehouse", "warehouse.lot_received", {**base, "qty": "很多"}, evidence=PHOTO, expect=422)
    post("warehouse", "warehouse.lot_received", {**base, "colour": "red"}, evidence=PHOTO, expect=422)
    post("warehouse", "no.such_event", base, evidence=PHOTO, expect=422)


def test_conflicts_and_versions(story, post):
    post("admin", "sales.customer_registered", {"name": "重复"}, object_id="C-HD", expect=409)
    post("gm", "quality.lot_dispositioned", {"status": "accepted"}, object_id="L-NOPE", expect=404)
    post(
        "gm",
        "quality.lot_dispositioned",
        {"status": "rejected"},
        object_id="L-KF1019",
        expected_version=1,
        expect=409,
    )
    r = post(
        "gm", "quality.lot_dispositioned", {"status": "rejected"}, object_id="L-KF1019", expected_version=2
    )
    assert r["version"] == 3


def test_idempotency(story, post):
    body = {"material": "M-AL88", "qty": 5, "unit": "t", "received_at": T0}
    a = post("warehouse", "warehouse.lot_received", body, evidence=PHOTO, idempotency_key="im:msg-889")
    b = post(
        "warehouse", "warehouse.lot_received", body, evidence=PHOTO, idempotency_key="im:msg-889", expect=200
    )
    assert b["duplicate"] is True and b["seq"] == a["seq"] and b["object_id"] == a["object_id"]
    post("admin", "sales.customer_registered", {"name": "x"}, idempotency_key="im:msg-889", expect=409)


def test_mock_and_real_do_not_mix(story, post):
    post(
        "warehouse",
        "warehouse.lot_received",
        {"material": "M-AL88", "qty": 1, "unit": "t", "received_at": T0},
        object_id="L-MOCK",
        evidence=PHOTO,
        data_mode="mock",
    )
    post(
        "worker",
        "production.batch_started",
        {"product": "P-ML26", "material_lots": ["L-MOCK"], "unit": "块", "started_at": T0},
        expect=422,
    )
    post(
        "gm",
        "quality.lot_dispositioned",
        {"status": "accepted"},
        object_id="L-KF1019",
        data_mode="mock",
        expect=409,
    )


def test_tenant_isolation(story, get, post):
    get("other_admin", "/v1/objects/production_batch/B2611-03", expect=404)
    assert get("other_admin", "/v1/events")["events"] == []
    r = post("other_admin", "sales.customer_registered", {"name": "另一家的客户"}, object_id="C-HD")
    assert r["seq"] == 1  # 序号按租户独立；同一个 id 在不同租户互不冲突


def _db(settings):
    engine = make_engine(settings.database_url)
    return engine, make_sessionmaker(engine)


def test_events_are_append_only(story, settings):
    async def run():
        engine, sm = _db(settings)
        try:
            for stmt in ("UPDATE events SET payload = '{}'", "DELETE FROM events"):
                async with sm() as s:
                    with pytest.raises(Exception, match="只允许追加"):
                        await s.execute(text(stmt))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rebuild_reproduces_projection(story, settings, tenants_dir):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    tenant = TenantRegistry.load(tenants_dir).get("luneng")
    assert tenant
    onto = load_ontology(tenant, packs_dir=repo / "packs", tenants_dir=tenants_dir)

    async def run():
        engine, sm = _db(settings)
        try:
            async with sm() as s:
                before = (
                    await s.execute(
                        ObjectState.__table__.select()
                        .where(ObjectState.tenant_id == "luneng")
                        .order_by("object_type", "object_id")
                    )
                ).all()
                links_before = (
                    await s.execute(
                        text("SELECT * FROM object_links WHERE tenant_id='luneng' ORDER BY 2,3,4,5,6")
                    )
                ).all()
            async with sm() as s, s.begin():
                n = await rebuild(s, onto, "luneng")
            async with sm() as s:
                after = (
                    await s.execute(
                        ObjectState.__table__.select()
                        .where(ObjectState.tenant_id == "luneng")
                        .order_by("object_type", "object_id")
                    )
                ).all()
                links_after = (
                    await s.execute(
                        text("SELECT * FROM object_links WHERE tenant_id='luneng' ORDER BY 2,3,4,5,6")
                    )
                ).all()
            return n, before, after, links_before, links_after
        finally:
            await engine.dispose()

    n, before, after, lb, la = asyncio.run(run())
    assert n == 18
    assert before == after and lb == la and len(la) > 10
