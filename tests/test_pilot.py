"""W13 试点上线支持：主数据 Excel 导入、生产日报（接口 / IM / 定时推送）。"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from fos.channels.mock import MockChannel
from fos.reports.daily import parse_day
from fos.reports.schedule import next_fire, send_daily

DAY = "2026-11-06"


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


def _template(client, h):
    r = client.get("/v1/import/template", headers=h("admin"))
    assert r.status_code == 200, r.text
    return load_workbook(io.BytesIO(r.content))


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fill(wb, sheet: str, rows: list[dict]):
    ws = wb[sheet]
    head = [str(c.value).rstrip("*") for c in ws[1]]
    for row in rows:
        ws.append([row.get(hd) for hd in head])


def _post(client, h, path: str, data: bytes, who: str = "admin"):
    files = {"file": ("主数据.xlsx", data, "application/octet-stream")}
    return client.post(path, files=files, headers=h(who))


def _good_book(client, h) -> bytes:
    wb = _template(client, h)
    _fill(
        wb,
        "供应商",
        [
            {"名称": "安徽金岩高岭土有限公司", "联系人": "王经理"},
            {"名称": "六安耐材辅料厂", "电话": 13800000000},
        ],
    )
    _fill(wb, "物料", [{"编号": "M-MLS", "名称": "莫来石粉", "规格": "M60", "类别": "原料", "计量单位": "t"}])
    _fill(
        wb,
        "产品",
        [
            {
                "牌号": "QG-1.0",
                "名称": "轻质莫来石隔热砖",
                "计量单位": "块",
                "类别": "隔热砖",
                "分类温度": 1260,
            }
        ],
    )
    _fill(wb, "设备", [{"名称": "2号隧道窑本体", "状态": "running"}])
    _fill(
        wb,
        "窑炉",
        [
            {
                "编号": "KILN-2",
                "名称": "2号隧道窑",
                "窑型": "隧道窑",
                "接入路径": "人工抄表",
                "最高温度": "1,350",
                "对应设备台账": "2号隧道窑本体",
            }
        ],
    )
    return _save(wb)


def test_template_lists_sheets_and_required_columns(client, h):
    wb = _template(client, h)
    assert wb.sheetnames == ["说明", "供应商", "客户", "物料", "产品", "设备", "窑炉"]
    head = [c.value for c in wb["产品"][1]]
    assert head[:5] == ["编号", "牌号*", "名称*", "规格", "计量单位*"]
    assert "分类温度" in head  # 更新事件 sales.product_specified 的字段也在同一张表
    assert "可选：隔热砖" in wb["产品"]["F1"].comment.text  # 枚举中文选项
    # 没有导入权限
    assert client.get("/v1/import/template", headers=h("warehouse")).status_code == 403


def test_import_preview_then_commit_idempotent(client, h):
    data = _good_book(client, h)
    r = _post(client, h, "/v1/import/preview", data)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["ok"] and not rep["committed"]
    assert rep["counts"] == {"created": 6, "exists": 0, "error": 0}
    # 预览不写入
    assert client.get("/v1/objects/supplier", headers=h("admin")).json()["objects"] == []

    r = _post(client, h, "/v1/import/commit", data)
    assert r.status_code == 201, r.text
    assert r.json()["committed"] and r.json()["counts"]["created"] == 6

    kiln = client.get("/v1/objects/kiln/KILN-2", headers=h("admin")).json()
    st = kiln["state"]
    assert st["kiln_type"] == "tunnel" and st["access_path"] == "manual" and st["max_temp"] == 1350
    eq = client.get(f"/v1/objects/equipment/{st['equipment']}", headers=h("admin")).json()
    assert eq["state"]["name"] == "2号隧道窑本体"
    prods = client.get("/v1/objects/product", headers=h("admin")).json()["objects"]
    assert prods[0]["state"]["category"] == "insulating_brick"
    assert prods[0]["state"]["classification_temp"] == 1260
    sup = client.get("/v1/objects/supplier", headers=h("admin")).json()["objects"]
    assert {s["state"].get("phone") for s in sup} == {None, "13800000000"}  # 数字单元格按文字存
    # 证据：原件 + 工作表 + 行号
    evs = client.get("/v1/events", params={"type": "production.kiln_registered"}, headers=h("admin")).json()
    ev = evs["events"][0]
    assert ev["evidence"][0]["ref"].startswith("blob://luneng/")
    assert "窑炉 第 2 行" in ev["evidence"][0]["note"]

    # 再导一次：全部已存在，不重复写
    r = _post(client, h, "/v1/import/commit", data)
    assert r.status_code == 201
    assert r.json()["counts"] == {"created": 0, "exists": 6, "error": 0}


def test_import_errors_block_whole_file(client, h):
    wb = _template(client, h)
    _fill(wb, "供应商", [{"名称": "好供应商"}])
    _fill(wb, "物料", [{"名称": "氧化铝粉", "类别": "原材料"}])  # 类别不对 + 缺单位
    _fill(wb, "窑炉", [{"名称": "1号梭式窑", "窑型": "梭式窑", "接入路径": "直连", "对应设备台账": "不存在"}])
    data = _save(wb)
    rep = _post(client, h, "/v1/import/preview", data).json()
    assert not rep["ok"] and rep["counts"] == {"created": 1, "exists": 0, "error": 2}
    mat = next(s for s in rep["sheets"] if s["id"] == "material")["rows"][0]
    assert mat["row"] == 2 and "不是可选值" in mat["message"] and "缺少必填：计量单位" in mat["message"]
    kiln = next(s for s in rep["sheets"] if s["id"] == "kiln")["rows"][0]
    assert "找不到设备“不存在”" in kiln["message"]

    r = _post(client, h, "/v1/import/commit", data)
    assert r.status_code == 422 and not r.json()["committed"]
    assert client.get("/v1/objects/supplier", headers=h("admin")).json()["objects"] == []  # 一条也没写


def test_import_rejects_bad_files_and_checks_event_permissions(client, h, keys):
    r = _post(client, h, "/v1/import/preview", b"not an excel")
    assert r.status_code == 422 and "xlsx" in r.json()["detail"]
    wb = _template(client, h)
    for name in wb.sheetnames[1:]:
        del wb[name]
    wb.create_sheet("随便")
    assert _post(client, h, "/v1/import/preview", _save(wb)).status_code == 422


# ---------------- 生产日报 ----------------


def _ev(client, h, typ, oid, payload, at, *, mode="real", evidence=True, who="admin"):
    body = {"type": typ, "object_id": oid, "payload": payload, "occurred_at": at, "data_mode": mode}
    if evidence:
        body["evidence"] = [{"kind": "manual", "ref": "test"}]
    r = client.post("/v1/events", json=body, headers=h(who))
    assert r.status_code == 201, r.text


def _day_events(client, h, mode="real"):
    s = "" if mode == "real" else "m"
    at = f"{DAY}T09:00:00+08:00"
    _ev(client, h, "purchase.supplier_registered", f"S1{s}", {"name": "金岩"}, at, mode=mode)
    _ev(
        client,
        h,
        "warehouse.material_defined",
        f"M1{s}",
        {"name": "莫来石粉", "category": "raw", "unit": "t"},
        at,
        mode=mode,
    )
    _ev(
        client,
        h,
        "sales.product_defined",
        f"P1{s}",
        {"code": "QG-1.0", "name": "隔热砖", "unit": "块"},
        at,
        mode=mode,
    )
    for i, qty in enumerate([12.5, 7.5]):
        _ev(
            client,
            h,
            "warehouse.lot_received",
            f"L{i}{s}",
            {
                "material": f"M1{s}",
                "qty": qty,
                "unit": "t",
                "received_at": at,
                "status": "pending_inspection",
            },
            at,
            mode=mode,
        )
    _ev(
        client,
        h,
        "production.batch_started",
        f"B1{s}",
        {"product": f"P1{s}", "unit": "块", "started_at": at, "material_lots": [f"L0{s}"]},
        at,
        mode=mode,
    )
    _ev(
        client,
        h,
        "production.batch_completed",
        f"B1{s}",
        {"good_qty": 2870, "status": "completed"},
        at,
        mode=mode,
    )
    _ev(
        client,
        h,
        "quality.inspection_recorded",
        f"I1{s}",
        {"kind": "final", "batch": f"B1{s}", "inspected_at": at, "result": "fail", "report_no": "QC-1"},
        at,
        mode=mode,
    )
    # 第二天的事件不算进 11-06（北京时间 11-07 00:30 = UTC 11-06 16:30）
    _ev(
        client,
        h,
        "warehouse.lot_received",
        f"L9{s}",
        {"material": f"M1{s}", "qty": 100, "unit": "t", "received_at": at},
        "2026-11-06T16:30:00+00:00",
        mode=mode,
    )


def test_daily_report_counts_from_ledger(client, h):
    _day_events(client, h)
    r = client.get("/v1/reports/daily", params={"date": DAY}, headers=h("admin"))
    assert r.status_code == 200, r.text
    rep = r.json()
    items = {it["title"]: it for it in rep["items"]}
    assert rep["data_mode"] == "real"
    assert items["来料入库"]["count"] == 2
    assert items["来料入库"]["totals"] == [{"value": 20.0, "unit": "t"}]
    assert items["来料入库"]["groups"][0]["label"] == "莫来石粉"
    assert items["批次开工"]["totals"] == [{"value": 1.0, "unit": "批"}]
    assert items["完工合格"]["groups"] == [{"label": "隔热砖", "value": 2870.0, "unit": "块"}]
    assert items["检验"]["groups"][0]["label"] == "不合格"
    alerts = {a["text"] for a in rep["alerts"]}
    assert {"检验不合格", "来料未检"} <= alerts
    assert "来料入库：20 t（莫来石粉）" in rep["lines"]

    # 工人只看到生产相关的项，看不到仓库、质检
    rep = client.get("/v1/reports/daily", params={"date": DAY}, headers=h("worker")).json()
    titles = {it["title"] for it in rep["items"]}
    assert "批次开工" in titles and "来料入库" not in titles and "检验" not in titles
    assert all(a["object_type"] == "production_batch" for a in rep["alerts"])

    # 租户隔离：另一家工厂看不到
    assert client.get("/v1/reports/daily", params={"date": DAY}, headers=h("other_admin")).json()["events"] == 0

    nxt = client.get("/v1/reports/daily", params={"date": "2026-11-07"}, headers=h("admin")).json()
    assert {it["title"]: it for it in nxt["items"]}["来料入库"]["totals"] == [{"value": 100.0, "unit": "t"}]


def test_daily_report_mock_only_day_is_marked(client, h):
    _day_events(client, h, mode="mock")
    rep = client.get("/v1/reports/daily", params={"date": DAY}, headers=h("admin")).json()
    assert rep["data_mode"] == "mock" and rep["events"] == 8


def test_daily_report_via_im_and_schedule(client, h):
    _day_events(client, h)
    r = client.put(
        "/v1/channels/identities",
        json={"channel": "mock", "external_id": "im-gm", "login": "gm"},
        headers=h("admin"),
    )
    assert r.status_code == 200
    body = {"external_msg_id": "d1", "sender_id": "im-gm", "kind": "text", "text": f"@助手 日报 {DAY}"}
    out = client.post("/v1/channels/mock/messages", json=body, headers=h("admin")).json()
    assert out["route"] == "daily_report" and out["status"] == "done"
    card = out["replies"][0]["card"]
    assert card["title"] == "生产日报 11月6日" and "检验不合格" in card["text"]
    vals = {f["label"]: f["value"] for f in card["fields"]}
    assert vals["来料入库"] == "20 t（莫来石粉）"

    # 定时推送：同一天只推一次；没绑定 IM 的收报人跳过
    svc = client.app.state.services
    svc.registry.get("luneng").reports.daily.to = ["gm", "qc"]
    ch = MockChannel()
    res = client.portal.call(send_daily, svc, "luneng", ch, date(2026, 11, 6))
    assert res.sent == ["gm"] and "qc" in res.skipped
    assert ch.outbox[0]["to"]["user_id"] == "im-gm" and ch.outbox[0]["card"]["title"] == "生产日报 11月6日"
    again = client.portal.call(send_daily, svc, "luneng", ch, date(2026, 11, 6))
    assert again.duplicate and len(ch.outbox) == 1


def test_parse_day_and_next_fire():
    today = date(2026, 1, 3)
    assert parse_day(None, "", today) == today
    assert parse_day("昨天", "", today) == date(2026, 1, 2)
    assert parse_day(None, "昨天", today) == date(2026, 1, 2)
    assert parse_day(None, "12-31", today) == date(2025, 12, 31)  # 年初说的是去年
    assert parse_day(None, "1月2日", today) == date(2026, 1, 2)
    assert parse_day(None, "2025/11/06", today) == date(2025, 11, 6)
    assert parse_day(None, "13-40", today) is None
    assert parse_day(None, "客户", today) is None
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)  # 北京 18:00
    assert next_fire(now, "19:00", tz).isoformat() == "2026-11-07T19:00:00+08:00"
    assert next_fire(now, "08:00", tz).isoformat() == "2026-11-08T08:00:00+08:00"


def test_pilot_check_lists_gaps(settings, keys):
    import asyncio

    from fos.onboarding.check import pilot_check

    items = {i.title: i for i in asyncio.run(pilot_check(settings, "luneng"))}
    assert items["配置校验"].level == "ok"
    assert items["OCR"].level == "warn"  # 模拟 OCR
    assert items["审批人"].level == "ok"  # 测试库里 gm、sales_lead 都有人
    assert items["主数据"].level == "warn" and "供应商" in items["主数据"].detail
    assert items["生产日报"].level == "warn"
    # 测试库用 create_all 建表，PostgreSQL 上没有迁移版本记录，会报“数据库迁移”
    assert {t for t, i in items.items() if i.level == "fail"} <= {"数据库迁移"}
    assert asyncio.run(pilot_check(settings, "nope"))[0].level == "fail"
