"""API Key 在线生命周期治理（签发、脱敏列举、跨租户隔离吊销）与运营总控面板回归测试。"""

from __future__ import annotations

from pathlib import Path


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_ops_apikey_lifecycle_and_tenant_isolation(client, keys) -> None:
    """验证管理员签发、脱敏列举、跨租户隔离与吊销立即生效。"""
    # 1. 非管理员全部 403
    assert client.get("/v1/ops/keys", headers=_h(keys, "sales")).status_code == 403
    assert client.post("/v1/ops/keys", json={"login": "sales"}, headers=_h(keys, "sales")).status_code == 403

    # 2. 为同租户 sales 签发新密钥，立即生效
    r_issue = client.post(
        "/v1/ops/keys",
        json={"login": "sales", "label": "轮换测试"},
        headers=_h(keys, "admin"),
    )
    assert r_issue.status_code == 201, r_issue.text
    issued = r_issue.json()
    key_id = issued["key_id"]
    new_token = issued["plaintext"]
    assert new_token.startswith(f"fos_{key_id}_")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200

    # 3. 列举接口绝不泄漏 secret_sha256 或明文
    r_list = client.get("/v1/ops/keys", headers=_h(keys, "admin"))
    assert r_list.status_code == 200
    body = r_list.json()
    found = [it for it in body["items"] if it["id"] == key_id]
    assert len(found) == 1
    assert "secret_sha256" not in found[0] and "plaintext" not in found[0]
    assert found[0]["label"] == "轮换测试" and found[0]["active"] is True

    # 4. 跨租户隔离：other_admin 无权吊销 luneng 的密钥（返回 404 且密钥仍有效）
    r_cross = client.post(f"/v1/ops/keys/{key_id}/revoke", headers=_h(keys, "other_admin"))
    assert r_cross.status_code == 404
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200

    # 5. 本租户管理员吊销后立即失效（401）
    r_rev = client.post(f"/v1/ops/keys/{key_id}/revoke", headers=_h(keys, "admin"))
    assert r_rev.status_code == 200 and r_rev.json()["revoked_at"] is not None
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 401

    # 默认列表隐藏已吊销，include_revoked=true 可查到且 active=False
    r_active = client.get("/v1/ops/keys", headers=_h(keys, "admin")).json()
    assert all(it["id"] != key_id for it in r_active["items"])
    r_all = client.get("/v1/ops/keys?include_revoked=true", headers=_h(keys, "admin")).json()
    rev_item = [it for it in r_all["items"] if it["id"] == key_id][0]
    assert rev_item["active"] is False

    # 6. 验证 ops.html 已接入预算租约与密钥治理表格
    ops_html = (Path(__file__).resolve().parent.parent / "fos/web/static/ops.html").read_text(
        encoding="utf-8"
    )
    assert 'id="budgetSummary"' in ops_html and 'id="keyTable"' in ops_html
