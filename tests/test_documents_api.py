import asyncio
import io
import time

from fos.quickstart import sample_tech_docx, sample_tender_docx
from tests import samples
from tests.test_perception import TEXT


def _h(k):
    return {"Authorization": f"Bearer {k}"}


def test_upload_parse_and_read(client, keys):
    pdf = samples.pdf([TEXT, None])
    r = client.post(
        "/v1/documents",
        files={"file": ("HDTC-JS-2026-117.pdf", pdf, "application/pdf")},
        data={"kind": "tech"},
        headers=_h(keys["sales"]),
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["format"] == "pdf" and d["kind"] == "tech" and d["page_count"] == 2
    assert d["data_mode"] == "mock" and d["notice"] == "模拟数据，非真实识别结果"
    assert [p["method"] for p in d["pages"]] == ["text_layer", "ocr_mock"]

    p1 = client.get(f"/v1/documents/{d['id']}/pages/1", headers=_h(keys["sales"])).json()
    assert "1250 C" in p1["markdown"] and p1["data_mode"] == "real" and "notice" not in p1
    p2 = client.get(f"/v1/documents/{d['id']}/pages/2", headers=_h(keys["sales"])).json()
    assert p2["notice"]
    assert client.get(f"/v1/documents/{d['id']}/pages/3", headers=_h(keys["sales"])).status_code == 404

    # 同一文件再传一次：不重复解析
    again = client.post("/v1/documents", files={"file": ("copy.pdf", pdf)}, headers=_h(keys["sales"])).json()
    assert again["duplicate"] is True and again["id"] == d["id"]


def test_permissions_and_isolation(client, keys):
    r = client.post("/v1/documents", files={"file": ("a.docx", samples.docx())}, headers=_h(keys["worker"]))
    assert r.status_code == 201  # 工人可以上传（拍照录单）
    doc_id = r.json()["id"]
    assert client.get(f"/v1/documents/{doc_id}", headers=_h(keys["worker"])).status_code == 403  # 但不能浏览
    assert client.get(f"/v1/documents/{doc_id}", headers=_h(keys["admin"])).status_code == 200
    assert client.get(f"/v1/documents/{doc_id}", headers=_h(keys["other_admin"])).status_code == 404
    assert client.post("/v1/documents", files={"file": ("a.xlsx", samples.xlsx())}).status_code == 401


def test_rejects_bad_uploads(client, keys, settings):
    h = _h(keys["admin"])
    assert client.post("/v1/documents", files={"file": ("x.txt", b"hello")}, headers=h).status_code == 415
    assert client.post("/v1/documents", files={"file": ("x.pdf", b"")}, headers=h).status_code == 422
    assert (
        client.post("/v1/documents", files={"file": ("x.pdf", b"%PDF-1.4 broken")}, headers=h).status_code
        == 422
    )
    big = b"%PDF-" + b"0" * (settings.max_upload_mb * 1024 * 1024)
    assert client.post("/v1/documents", files={"file": ("big.pdf", big)}, headers=h).status_code == 413


# ---------------- 后台解析、字段核对、导出（OCR MVP） ----------------


def _wait_parsed(client, doc_id, key, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/v1/documents/{doc_id}", headers=_h(key)).json()
        if d["status"] in ("parsed", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"文档没有在 {timeout}s 内解析完：{d['status']}")


def _upload_async(client, key, name, data, kind):
    r = client.post(
        "/v1/documents?wait=false", files={"file": (name, data)}, data={"kind": kind}, headers=_h(key)
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    return r.json()["id"]


def test_async_upload_extract_review_export(client, keys):
    k = keys["sales"]
    doc_id = _upload_async(client, k, "技术要求.docx", sample_tech_docx(), "tech")
    d = _wait_parsed(client, doc_id, k)
    assert d["status"] == "parsed" and d["schema_id"] == "tech" and d["data_mode"] == "real"
    assert d["fields_summary"]["missing_required"] == 0

    f = client.get(f"/v1/documents/{doc_id}/fields", headers=_h(k)).json()
    assert f["schema"]["id"] == "tech" and f["llm_extraction"] is False
    by = {x["key"]: x for x in f["fields"]}
    t = by["normal_temp"]
    assert (t["value"], t["unit"], t["status"]) == ("1250", "℃", "extracted")
    assert t["sources"][0]["page"] == 1 and "1250℃" in t["sources"][0]["quote"]
    assert [x["key"] for x in f["fields"]][:2] == ["customer_name", "project_name"]  # 按 Schema 顺序

    # 人工修改 → 留痕；确认；还原
    url = f"/v1/documents/{doc_id}/fields"
    r = client.patch(
        f"{url}/insulation_layer", json={"value": "LN-1260 纤维板 120 mm", "note": "电话确认"}, headers=_h(k)
    )
    assert r.status_code == 200 and r.json()["status"] == "corrected" and r.json()["method"] == "manual"
    h = r.json()["history"][0]
    assert (
        h["by_name"] == "sales"
        and h["before"]["value"] == "LN-1260 硅酸铝纤维板 100 mm"
        and h["note"] == "电话确认"
    )
    assert (
        client.patch(f"{url}/normal_temp", json={"action": "confirm"}, headers=_h(k)).json()["status"]
        == "confirmed"
    )
    assert client.patch(f"{url}/heat_loss", json={"action": "confirm"}, headers=_h(k)).status_code == 422
    assert (
        client.patch(f"{url}/heat_loss", json={"action": "correct", "value": " "}, headers=_h(k)).status_code
        == 422
    )
    assert client.patch(f"{url}/nope", json={"action": "confirm"}, headers=_h(k)).status_code == 404

    # 重新抽取：人工修改和确认保留
    r = client.post(f"/v1/documents/{doc_id}/extract", headers=_h(k)).json()
    assert r["kept_human_edits"] == 2
    by = {x["key"]: x for x in client.get(url, headers=_h(k)).json()["fields"]}
    assert by["insulation_layer"]["value"] == "LN-1260 纤维板 120 mm"
    assert by["normal_temp"]["status"] == "confirmed"
    r = client.patch(f"{url}/insulation_layer", json={"action": "reset"}, headers=_h(k)).json()
    assert (
        r["value"] == "LN-1260 硅酸铝纤维板 100 mm" and r["status"] == "extracted" and len(r["history"]) == 2
    )

    # 导出
    x = client.get(f"/v1/documents/{doc_id}/export?format=xlsx", headers=_h(k))
    assert x.status_code == 200 and x.headers["content-type"].startswith("application/vnd.openxmlformats")
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(x.content))
    assert wb.sheetnames == ["字段", "逐页原文", "说明"]
    rows = {r[1]: r for r in wb["字段"].iter_rows(min_row=2, values_only=True)}
    assert rows["正常工作温度"][2:5] == ("1250", "℃", "已确认") and "[第1页]" in rows["正常工作温度"][10]
    j = client.get(f"/v1/documents/{doc_id}/export?format=json", headers=_h(k)).json()
    assert j["summary"]["confirmed"] == 1 and j["pages"][0]["method"] == "docx"

    # 列表带统计；原件下载；Word 没有版面预览
    items = client.get("/v1/documents", headers=_h(k)).json()["items"]
    assert items[0]["id"] == doc_id and items[0]["fields"]["confirmed"] == 1
    orig = client.get(f"/v1/documents/{doc_id}/original", headers=_h(k))
    assert (
        orig.status_code == 200
        and orig.content.startswith(b"PK")
        and "filename*=UTF-8" in orig.headers["content-disposition"]
    )
    assert client.get(f"/v1/documents/{doc_id}/pages/1/image", headers=_h(k)).status_code == 404


def test_review_permissions_and_isolation(client, keys):
    doc_id = _upload_async(client, keys["sales"], "招标.docx", sample_tender_docx(), "tender")
    _wait_parsed(client, doc_id, keys["sales"])
    url = f"/v1/documents/{doc_id}/fields/tender_no"
    # 质检能看文档，但没有 doc.field:write，不能改字段
    assert client.get(f"/v1/documents/{doc_id}/fields", headers=_h(keys["qc"])).status_code == 200
    assert client.patch(url, json={"action": "confirm"}, headers=_h(keys["qc"])).status_code == 403
    assert client.patch(url, json={"action": "confirm"}, headers=_h(keys["other_admin"])).status_code == 404
    assert client.get(f"/v1/documents/{doc_id}/export", headers=_h(keys["other_admin"])).status_code == 404
    assert client.get("/v1/documents", headers=_h(keys["other_admin"])).json()["total"] == 0
    assert client.patch(url, json={"action": "confirm"}, headers=_h(keys["admin"])).status_code == 200


def test_mock_scan_cannot_export_and_has_preview(client, keys):
    k = keys["admin"]
    pdf = samples.pdf([TEXT, None])
    doc_id = _upload_async(client, k, "scan.pdf", pdf, "tech")
    d = _wait_parsed(client, doc_id, k)
    assert d["data_mode"] == "mock" and d["notice"] and d["progress"] == 2
    assert client.get(f"/v1/documents/{doc_id}/export?format=xlsx", headers=_h(k)).status_code == 409
    assert client.get(f"/v1/documents/{doc_id}/export?format=json", headers=_h(k)).status_code == 200
    img = client.get(f"/v1/documents/{doc_id}/pages/2/image", headers=_h(k))
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG")
    assert client.get(f"/v1/documents/{doc_id}/pages/3/image", headers=_h(k)).status_code == 404
    f = client.get(f"/v1/documents/{doc_id}/fields", headers=_h(k)).json()
    assert f["notice"] and {x["data_mode"] for x in f["fields"] if x["status"] == "missing"} == {"mock"}
    # 重复上传直接返回已有文档
    again = client.post("/v1/documents?wait=false", files={"file": ("x.pdf", pdf)}, headers=_h(k))
    assert again.status_code == 200 and again.json()["duplicate"] is True


def test_other_kind_has_no_fields_and_schema_switch(client, keys):
    k = keys["admin"]
    doc_id = _upload_async(client, k, "说明.docx", sample_tender_docx(), "other")
    _wait_parsed(client, doc_id, k)
    f = client.get(f"/v1/documents/{doc_id}/fields", headers=_h(k)).json()
    assert f["schema"] is None and f["fields"] == []
    assert client.post(f"/v1/documents/{doc_id}/extract", headers=_h(k)).status_code == 422
    assert (
        client.post(f"/v1/documents/{doc_id}/extract", json={"schema_id": "nope"}, headers=_h(k)).status_code
        == 422
    )
    r = client.post(f"/v1/documents/{doc_id}/extract", json={"schema_id": "tender"}, headers=_h(k)).json()
    assert r["schema_id"] == "tender" and r["summary"]["extracted"] > 10
    schemas = client.get("/v1/extraction/schemas", headers=_h(k)).json()["items"]
    assert {s["id"] for s in schemas} == {"tech", "tender"}


def test_failed_ocr_can_retry_and_restart_recovers(settings, keys, monkeypatch):
    """OCR 服务不可用 → failed，可重试；进程重启时排队中的文档自动续跑。"""
    from fastapi.testclient import TestClient

    from fos.api.app import create_app
    from fos.perception.ocr import MockOCR, OCRError

    async def broken(self, image_png, *, page):
        raise OCRError(f"第 {page} 页 OCR 失败：连接被拒绝")

    k = keys["admin"]
    monkeypatch.setattr(MockOCR, "recognize", broken)
    with TestClient(create_app(settings)) as c:
        doc_id = _upload_async(c, k, "scan.pdf", samples.pdf([None]), "tech")
        d = _wait_parsed(c, doc_id, k)
        assert d["status"] == "failed" and "连接被拒绝" in d["error"]
        assert c.post(f"/v1/documents/{doc_id}/retry", headers=_h(k)).status_code == 202
        assert _wait_parsed(c, doc_id, k)["status"] == "failed"
    monkeypatch.undo()

    # 模拟"进程在解析中途退出"：把状态改回 parsing，再启动一次
    from fos.db.models import Document
    from fos.db.session import make_engine, make_sessionmaker

    async def set_parsing():
        engine = make_engine(settings.database_url)
        async with make_sessionmaker(engine)() as s, s.begin():
            (await s.get(Document, doc_id)).status = "parsing"
        await engine.dispose()

    asyncio.run(set_parsing())
    with TestClient(create_app(settings)) as c:
        d = _wait_parsed(c, doc_id, k)
        assert d["status"] == "parsed" and d["data_mode"] == "mock"
        assert c.post(f"/v1/documents/{doc_id}/retry", headers=_h(k)).status_code == 409


def test_web_console_is_served(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/"
    page = client.get("/app/")
    assert page.status_code == 200 and "文档解析" in page.text
    assert client.get("/app/app.js").status_code == 200
