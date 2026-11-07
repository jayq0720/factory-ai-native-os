"""W9：拍照录单（照片 → 识别 → 录单卡片 → 确认入账）与批次追溯。"""

from __future__ import annotations

import base64

import pytest

from fos.entry.samples import MASTER, PHOTO_DIR, SAMPLES


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


@pytest.fixture
def send(client, h):
    n = {"i": 0}

    def _send(sender: str, **kw):
        n["i"] += 1
        body = {"external_msg_id": f"e{n['i']}", "sender_id": sender, **kw}
        r = client.post("/v1/channels/mock/messages", json=body, headers=h("admin"))
        assert r.status_code == 200, r.text
        return r.json()

    return _send


@pytest.fixture
def bind(client, h):
    def _bind(external_id: str, login: str):
        r = client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": external_id, "login": login},
            headers=h("admin"),
        )
        assert r.status_code == 200, r.text

    return _bind


@pytest.fixture
def master(client, h):
    for ev, oid, payload in MASTER:
        r = client.post(
            "/v1/events",
            json={"type": ev, "object_id": oid, "payload": payload, "data_mode": "mock"},
            headers=h("admin"),
        )
        assert r.status_code == 201, r.text


def photo(send, who: str, idx: int, text: str = ""):
    name = SAMPLES[idx][0]
    data = (PHOTO_DIR / name).read_bytes()
    att = {"kind": "image", "ref": name, "filename": name}
    return send(
        who, kind="image", text=text, attachments=[att], files={name: base64.b64encode(data).decode()}
    )


def confirm(send, who: str, r):
    card = next(x for x in r["replies"] if x.get("card_id"))
    return send(who, text=f"确认 {card['short_code']}")


def test_photo_entry_full_chain_and_trace(client, send, bind, h, master):
    for ext, login in [("im-wh", "warehouse"), ("im-worker", "worker"), ("im-qc", "qc"), ("im-gm", "gm")]:
        bind(ext, login)

    # 1. 送货单 → 来料入库卡片，字段带页码，查到物料和供应商
    r = photo(send, "im-wh", 0)
    assert r["route"] == "doc_intake" and r["status"] == "done"
    card = r["replies"][0]
    assert card["card"]["title"] == "录单确认：原料送货单" and card["card"]["data_mode"] == "mock"
    vals = {f["label"]: f["value"] for f in card["card"]["fields"]}
    assert vals["物料"].startswith("莫来石粉（M-MLS）") and vals["数量"] == "12.5"
    assert vals["单位"] == "t" and vals["到货时间"].startswith("2026-11-05")
    assert r["result"]["documents"][0]["entry"]["ok"] is True

    # 工人没有入库权限：确认不生效，卡片仍可由仓管确认
    c = confirm(send, "im-worker", r)
    assert c["status"] == "rejected" and "没有确认这张卡片的权限" in c["replies"][0]["text"]
    c = confirm(send, "im-wh", r)
    assert c["status"] == "done", c
    lot_id = c["result"]["object_id"]
    lot = client.get(f"/v1/objects/material_lot/{lot_id}", headers=h("admin")).json()
    assert lot["state"]["supplier_lot_no"] == "JY261105" and lot["state"]["status"] == "pending_inspection"
    assert lot["data_mode"] == "mock"
    ev = lot["history"][0]
    assert ev["actor"]["name"] == "warehouse" and ev["occurred_at"].startswith("2026-11-0")
    assert {e["kind"] for e in ev["evidence"]} >= {"photo", "approval"}
    assert any(e.get("page") == 1 and "JY261105" in (e.get("quote") or "") for e in ev["evidence"])

    # 同一张照片再发：不重复入账
    again = photo(send, "im-wh", 0)
    assert "已经录过了" in again["replies"][0]["text"]

    # 2–4. 生产任务单 / 装窑记录 / 报工单（工人）
    for idx, title in [(1, "生产任务单"), (2, "装窑记录"), (3, "报工单")]:
        r = photo(send, "im-worker", idx)
        assert r["replies"][0]["card"]["title"] == f"录单确认：{title}", r["replies"]
        c = confirm(send, "im-worker", r)
        assert c["status"] == "done", c
    b = client.get("/v1/objects/production_batch/B261106-01", headers=h("admin")).json()["state"]
    assert b["material_lots"] == [lot_id] and b["kiln_run"] == "KR-2-1107"
    assert b["good_qty"] == 2870 and b["status"] == "completed" and b["product"] == "P-QG10"

    # 5. 检验报告（质检）
    r = photo(send, "im-qc", 4)
    c = confirm(send, "im-qc", r)
    assert c["status"] == "done", c
    insp = client.get("/v1/objects/inspection/QC-2611-021", headers=h("admin")).json()["state"]
    assert (
        insp["result"] == "pass" and insp["bulk_density"] == 1.02 and insp["permanent_linear_change"] == -0.6
    )
    assert insp["batch"] == "B261106-01" and insp["kind"] == "final"

    # 追溯：IM 快捷指令，零模型成本
    t = send("im-gm", text="追溯 B261106-01")
    assert t["route"] == "trace" and t["status"] == "done"
    assert t["result"]["elapsed_ms"] < 10_000
    tc = t["replies"][0]["card"]
    groups = {f["label"]: f["value"] for f in tc["fields"]}
    assert "JY261105" in groups["原料批次"] and "2-1107" in groups["窑次"]
    assert "合格" in groups["下游·检验"] and "来料未检" in tc["text"]  # 来料还没检验，标出来
    assert "供应商" in groups and "窑炉" in groups

    # 按窑次号也能追，并看到同窑的批次
    t = send("im-gm", text="追溯：2-1107")
    assert t["result"]["root"] == {"object_type": "kiln_run", "object_id": "KR-2-1107"}

    # 接口：报告结构
    rep = client.get("/v1/objects/production_batch/B261106-01/trace/report", headers=h("gm")).json()
    dirs = {(n["object_type"], n["direction"]) for n in rep["nodes"]}
    assert ("material_lot", "upstream") in dirs and ("inspection", "downstream") in dirs
    assert rep["alerts"] == [{"object_type": "material_lot", "object_id": lot_id, "text": "来料未检"}]

    # 没有读权限的节点隐去内容（工人看不到供应商）
    rep = client.get("/v1/objects/production_batch/B261106-01/trace/report", headers=h("worker")).json()
    sup = next(n for n in rep["nodes"] if n["object_type"] == "supplier")
    assert sup["redacted"] and sup["state"] is None

    # 没找到
    t = send("im-gm", text="追溯 X999")
    assert "没有找到" in t["replies"][0]["text"]


def test_entry_problems_and_console_commit(client, send, bind, h):
    """主数据还没登记：卡片不给确认按钮，列出问题；控制台补登后可以提交。"""
    bind("im-wh", "warehouse")
    r = photo(send, "im-wh", 0)
    card = r["replies"][0]["card"]
    assert card["title"] == "录单待补全：原料送货单" and not card["buttons"]
    assert "没有找到物料“莫来石粉”" in card["text"]
    doc_id = r["result"]["documents"][0]["document_id"]

    d = client.get(f"/v1/documents/{doc_id}/entry", headers=h("warehouse")).json()
    assert d["ok"] is False and d["schema_id"] == "entry_delivery_note"
    assert client.post(f"/v1/documents/{doc_id}/entry", headers=h("warehouse")).status_code == 422

    for ev, oid, payload in MASTER[:2]:
        client.post(
            "/v1/events",
            json={"type": ev, "object_id": oid, "payload": payload, "data_mode": "mock"},
            headers=h("admin"),
        )
    d = client.get(f"/v1/documents/{doc_id}/entry", headers=h("warehouse")).json()
    assert d["ok"] is True, d["problems"]
    # 没有事件权限的人不能提交
    assert client.post(f"/v1/documents/{doc_id}/entry", headers=h("qc")).status_code == 403
    c = client.post(f"/v1/documents/{doc_id}/entry", headers=h("warehouse"))
    assert c.status_code == 201, c.text
    again = client.post(f"/v1/documents/{doc_id}/entry", headers=h("warehouse"))
    assert again.status_code == 200 and again.json()["duplicate"]


def test_unknown_photo_and_caption_detection(client, send, bind, h):
    from tests import samples

    bind("im-wh", "warehouse")
    png = samples.png()
    att = {"kind": "image", "ref": "p", "filename": "x.png"}
    r = send("im-wh", kind="image", attachments=[att], files={"p": base64.b64encode(png).decode()})
    assert r["replies"][0]["card"]["title"] == "已收到并识别"
    assert "没有认出是哪种单据" in r["replies"][0]["card"]["text"]


def test_entry_config_checked_against_ontology(settings):
    from fos.entry.spec import EntrySpec, check_entry
    from fos.ontology.loader import load_ontology
    from fos.tenancy import TenantRegistry

    t = TenantRegistry.load(settings.tenants_dir).get("luneng")
    onto = load_ontology(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
    spec = EntrySpec.model_validate(
        {"event": "production.batch_completed", "fields": {"good_qty": {"from": "nope"}, "bad": {"const": 1}}}
    )
    errs = check_entry("x", {"batch_no"}, spec, onto)
    assert any("nope" in e for e in errs) and any("bad" in e for e in errs)
    assert any("object_id" in e for e in errs)  # 更新事件要指向已有对象


def test_parse_datetime_and_convert():
    from zoneinfo import ZoneInfo

    from fos.entry.draft import convert, parse_datetime
    from fos.ontology.schema import PropertyDef

    tz = ZoneInfo("Asia/Shanghai")
    assert parse_datetime("2026年11月5日 下午3:30", tz).isoformat() == "2026-11-05T15:30:00+08:00"
    assert parse_datetime("2026-02-30", tz) is None
    num = PropertyDef(type="number", name="n")
    assert convert(num, "1,250.5 kg", tz) == (1250.5, None)
    assert convert(num, "无", tz)[1]
    assert convert(PropertyDef(type="bool", name="b"), "是", tz) == (True, None)
