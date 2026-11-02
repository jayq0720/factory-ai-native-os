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
