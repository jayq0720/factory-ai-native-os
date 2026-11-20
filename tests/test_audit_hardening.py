"""安全与可靠性审计修复回归测试。"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fos.api.app import create_app
from fos.config import LLMMode, Settings
from fos.engineering.service import _parse_area
from fos.excel_safe import excel_cell, excel_row, xml_safe
from fos.governance.principal import Principal
from fos.perception.blobs import LocalBlobStore
from fos.perception.parser import ParseError, check_zip_safety, sniff
from fos.reports.daily import parse_day
from fos.tender.service import parse_money
from fos.tools.builtin import ProposeArgs, propose
from fos.tools.registry import ToolContext, ToolError


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_excel_safe_formula_and_xml_sanitization():
    assert xml_safe("a\x00b\x0bc\td\ne") == "abc\td\ne"
    assert excel_cell("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert excel_cell("+1+1") == "'+1+1"
    assert excel_cell("-cmd") == "'-cmd"
    assert excel_cell("@cmd") == "'@cmd"
    assert excel_cell("\t=1") == "'\t=1"
    assert excel_cell(" =1") == "' =1"
    assert excel_cell(42) == 42
    assert excel_cell(3.14) == 3.14
    assert excel_cell(None) is None
    assert excel_row(["=1+1", "正常\x00文本", 10]) == ["'=1+1", "正常文本", 10]


def test_parse_money_area_and_leap_day():
    assert parse_money("186500元（大写：壹拾捌万陆仟伍佰元）") == pytest.approx(186500.0)
    assert parse_money("186.5万元（大写：壹佰捌拾陆万伍仟元）") == pytest.approx(1865000.0)
    assert _parse_area("120 m2") == pytest.approx(120.0)
    assert _parse_area("120-150 ㎡") == pytest.approx(150.0)
    assert parse_day(None, "2-29", date(2024, 1, 15)) is None
    assert parse_day(None, "12-31", date(2026, 1, 5)) == date(2025, 12, 31)


def test_zip_bomb_and_blob_path_traversal(tmp_path: Path):
    # 条目过多
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(1005):
            zf.writestr(f"f{i}.txt", b"x")
    with pytest.raises(ParseError, match="过多"):
        check_zip_safety(buf.getvalue())

    # 超高压缩比
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\x00" * (2 * 1024 * 1024))
    with pytest.raises(ParseError, match="压缩比"):
        sniff(buf2.getvalue())

    # BlobStore 路径校验
    store = LocalBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError, match="租户"):
        store.get("../etc", "0" * 64)
    with pytest.raises(ValueError, match="摘要"):
        store.get("luneng", "../../secret")
    digest = store.put("luneng", b"hello-blob")
    assert store.get("luneng", digest) == b"hello-blob"


def test_nan_inf_rejected_across_endpoints(client, keys):
    h_admin = {**_h(keys, "admin"), "Content-Type": "application/json"}
    h_worker = {**_h(keys, "worker"), "Content-Type": "application/json"}
    h_edge = {**_h(keys, "edge"), "Content-Type": "application/json"}

    r1 = client.post(
        "/v1/events",
        content=(
            '{"type":"warehouse.material_defined","object_id":"M-NAN",'
            '"payload":{"name":"测试","category":"raw","unit":"kg","safety_stock":NaN}}'
        ),
        headers=h_admin,
    )
    assert r1.status_code == 422

    r2 = client.post(
        "/v1/kiln/readings",
        content='{"point":"k1.panel","value":Infinity}',
        headers=h_worker,
    )
    assert r2.status_code == 422

    r3 = client.post(
        "/v1/edge/uplink",
        content=(
            '{"protocol":1,"tenant_id":"luneng","edge_id":"edge-1","seq":1,'
            '"readings":[{"point":"k1.top","ts_ms":1700000000000,"value":NaN,'
            '"unit":"℃","source":"retrofit","quality":"good"}]}'
        ),
        headers=h_edge,
    )
    assert r3.status_code == 422


def test_edge_id_binding_and_permission_matrix(client, keys):
    # edge 只能走 /v1/edge/uplink 且只能上报自己绑定的 edge_id，不能走人工抄表接口
    assert (
        client.post(
            "/v1/kiln/readings",
            json={"point": "k1.panel", "value": 100},
            headers=_h(keys, "edge"),
        ).status_code
        == 403
    )
    batch_e2 = {"protocol": 1, "tenant_id": "luneng", "edge_id": "edge-2", "seq": 1, "readings": []}
    assert client.post("/v1/edge/uplink", json=batch_e2, headers=_h(keys, "edge")).status_code == 403
    batch_e1 = {"protocol": 1, "tenant_id": "luneng", "edge_id": "edge-1", "seq": 1, "readings": []}
    assert client.post("/v1/edge/uplink", json=batch_e1, headers=_h(keys, "worker")).status_code == 403

    # sales 可读 eng_project；warehouse 具 doc.field:write；worker 保持最小权限（不能浏览文档）
    assert client.get("/v1/objects/eng_project", headers=_h(keys, "sales")).status_code == 200
    wh_me = client.get("/v1/me", headers=_h(keys, "warehouse")).json()
    assert "doc.field:write" in wh_me["permissions"]


def test_prod_mode_blocks_mock_and_debug_chat(tmp_path: Path, tenants_dir: Path, settings: Settings, keys):
    prod_settings = Settings(
        database_url=settings.database_url,
        tenants_dir=tenants_dir,
        packs_dir=settings.packs_dir,
        blob_dir=tmp_path / "prod_blobs",
        llm_mode=LLMMode.mock,
        env="prod",
    )
    h_admin = _h(keys, "admin")
    with TestClient(create_app(prod_settings)) as prod_client:
        hz = prod_client.get("/healthz")
        assert hz.headers["x-content-type-options"] == "nosniff"
        assert hz.headers["x-frame-options"] == "DENY"
        assert hz.headers["referrer-policy"] == "no-referrer"

        assert (
            prod_client.post(
                "/v1/llm/chat",
                json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
                headers=h_admin,
            ).status_code
            == 404
        )
        assert (
            prod_client.post(
                "/v1/events",
                json={
                    "type": "sales.customer_registered",
                    "object_id": "C-MOCK",
                    "payload": {"name": "模拟客户"},
                    "data_mode": "mock",
                },
                headers=h_admin,
            ).status_code
            == 403
        )
        assert (
            prod_client.put(
                "/v1/channels/identities",
                json={"channel": "mock", "external_id": "im-1", "login": "admin"},
                headers=h_admin,
            ).status_code
            == 422
        )


def test_ledger_guards_block_forged_quote_tender_and_po(client, keys):
    h_admin = _h(keys, "admin")
    ev = {"evidence": [{"kind": "manual", "ref": "test"}]}

    # 准备供应商与物料，直接伪造金额不匹配的采购单事件应被 POEventGuard 拦截
    assert (
        client.post(
            "/v1/events",
            json={
                "type": "purchase.supplier_registered",
                "object_id": "SUP-G",
                "payload": {"name": "守卫测试供应商"},
                **ev,
            },
            headers=h_admin,
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/events",
            json={
                "type": "warehouse.material_defined",
                "object_id": "MAT-G",
                "payload": {"name": "守卫测试物料", "category": "raw", "unit": "吨"},
                **ev,
            },
            headers=h_admin,
        ).status_code
        == 201
    )
    bad_po = client.post(
        "/v1/events",
        json={
            "type": "purchase.po_issued",
            "object_id": "PO-FORGED",
            "payload": {
                "po_no": "PO-FORGED",
                "supplier": "SUP-G",
                "material": "MAT-G",
                "qty": 10,
                "unit": "吨",
                "unit_price": 1000,
                "amount": 1,  # 伪造金额试图绕过审批阈值
                "status": "open",
            },
            **ev,
        },
        headers=h_admin,
    )
    assert bad_po.status_code == 422

    # 直接伪造 eng.quote_issued（无合法方案）应被 EngQuoteGuard 拦截
    assert (
        client.post(
            "/v1/events",
            json={
                "type": "eng.project_created",
                "object_id": "EP-G",
                "payload": {"name": "守卫工程", "status": "open"},
                **ev,
            },
            headers=h_admin,
        ).status_code
        == 201
    )
    bad_quote = client.post(
        "/v1/events",
        json={
            "type": "eng.quote_issued",
            "object_id": "EP-G",
            "payload": {
                "quote_no": "Q-FORGED",
                "quote_design": "nonexistent-design",
                "total": 100000,
                "gross_margin_pct": 99.0,
                "status": "quoted",
            },
            **ev,
        },
        headers=h_admin,
    )
    assert bad_quote.status_code == 422

    # 直接伪造 sales.tender_submitted（无合法拆解）应被 TenderSubmitGuard 拦截
    assert (
        client.post(
            "/v1/events",
            json={
                "type": "sales.tender_registered",
                "object_id": "TD-G",
                "payload": {
                    "name": "守卫招标",
                    "bid_deadline": "2027-01-01T09:00:00+08:00",
                    "status": "open",
                },
                **ev,
            },
            headers=h_admin,
        ).status_code
        == 201
    )
    bad_tender = client.post(
        "/v1/events",
        json={
            "type": "sales.tender_submitted",
            "object_id": "TD-G",
            "payload": {
                "analysis": "nonexistent-analysis",
                "star_negative": 0,
                "docs_missing": 0,
                "status": "submitted",
            },
            **ev,
        },
        headers=h_admin,
    )
    assert bad_tender.status_code == 422


def test_cross_conversation_group_card_confirmation_rejected(client, keys):
    h_admin = _h(keys, "admin")
    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": "im-wh", "login": "warehouse"},
            headers=h_admin,
        ).status_code
        == 200
    )
    card_body = {
        "to": {
            "channel": "mock",
            "conversation_type": "group",
            "conversation_id": "group-A",
        },
        "card": {
            "title": "新增物料：防跨群确认测试",
            "buttons": [
                {"id": "confirm", "label": "确认", "style": "primary"},
                {"id": "cancel", "label": "取消"},
            ],
        },
        "action": {
            "kind": "ledger.append",
            "request": {
                "type": "warehouse.material_defined",
                "object_id": "M-XCONV",
                "payload": {"name": "跨群测试棉", "category": "raw", "unit": "kg"},
            },
        },
    }
    card_resp = client.post("/v1/cards", json=card_body, headers=h_admin)
    assert card_resp.status_code == 201, card_resp.text
    code = card_resp.json()["short_code"]

    # 另一群未 @：直接丢弃
    r_drop = client.post(
        "/v1/channels/mock/messages",
        json={
            "external_msg_id": "xc-1",
            "sender_id": "im-wh",
            "conversation_type": "group",
            "conversation_id": "group-B",
            "mentioned": False,
            "text": f"确认 {code}",
        },
        headers=h_admin,
    ).json()
    assert r_drop["ignored"] is True

    # 另一群即使 @机器人 也拒绝跨会话文本确认
    r_rej = client.post(
        "/v1/channels/mock/messages",
        json={
            "external_msg_id": "xc-2",
            "sender_id": "im-wh",
            "conversation_type": "group",
            "conversation_id": "group-B",
            "mentioned": True,
            "text": f"确认 {code}",
        },
        headers=h_admin,
    ).json()
    assert r_rej["status"] == "rejected"
    assert "其他会话" in r_rej["replies"][0]["text"]

    # 原会话无需 @ 即可正常确认
    r_ok = client.post(
        "/v1/channels/mock/messages",
        json={
            "external_msg_id": "xc-3",
            "sender_id": "im-wh",
            "conversation_type": "group",
            "conversation_id": "group-A",
            "mentioned": False,
            "text": f"确认 {code}",
        },
        headers=h_admin,
    ).json()
    assert r_ok["status"] == "done"


def test_autonomy_l1_blocks_ledger_propose():
    principal = Principal("luneng", "u1", "user", "测试", ("admin",), frozenset({"*.*:*"}))
    skill = MagicMock()
    skill.proposes = ["warehouse.material_defined"]
    onto = MagicMock()
    ev_def = MagicMock()
    ev_def.object = "material"
    ev_def.domain = "warehouse"
    ev_def.permission = "warehouse.material:write"
    onto.events = {"warehouse.material_defined": ev_def}

    ctx = ToolContext(
        session=MagicMock(),
        user=principal,
        skill=skill,
        ontology=onto,
        extras={"autonomy": {"warehouse.material": "L1"}},
    )
    args = ProposeArgs(
        type="warehouse.material_defined",
        object_id="M-1",
        payload={"name": "x"},
        title="新增物料",
    )
    with pytest.raises(ToolError, match="L1"):
        asyncio.run(propose(ctx, args))
