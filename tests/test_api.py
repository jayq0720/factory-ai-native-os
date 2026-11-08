def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers["x-request-id"]


def test_readyz(client):
    r = client.get("/readyz")
    assert r.json() == {
        "status": "ready",
        "tenants": 2,
        "llm_mode": "mock",
        "ocr_mode": "mock",
        "llm_extraction": False,
    }


def test_me_requires_key(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=_h("fos_0123456789abcdef_wrong")).status_code == 401
    assert client.get("/v1/me", headers=_h("not-a-key")).status_code == 401


def test_me_resolves_tenant_and_permissions(client, keys):
    r = client.get("/v1/me", headers=_h(keys["worker"]))
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "luneng"
    assert body["tenant_name"] == "六安绿能"
    assert body["permissions"] == [
        "doc.document:write",
        "production.*:read",
        "production.reading:write",
        "production.record:write",
    ]

    r = client.get("/v1/me", headers=_h(keys["other_admin"]))
    assert r.json()["tenant_id"] == "other"


def test_tampered_secret_rejected(client, keys):
    k = keys["admin"]
    bad = k[:-1] + ("A" if k[-1] != "A" else "B")
    assert client.get("/v1/me", headers=_h(bad)).status_code == 401


def test_permission_enforced(client, keys):
    msg = {"messages": [{"role": "user", "content": "1# 窑昨天烧了几车？"}]}
    assert client.post("/v1/llm/chat", json=msg, headers=_h(keys["worker"])).status_code == 403
    r = client.post("/v1/llm/chat", json=msg, headers=_h(keys["admin"]))
    assert r.status_code == 200
    assert r.json()["data_mode"] == "mock"
    assert "1# 窑" in r.json()["content"]


def test_unknown_model_alias(client, keys):
    msg = {"model": "gpt-x", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/llm/chat", json=msg, headers=_h(keys["admin"]))
    assert r.status_code == 400
