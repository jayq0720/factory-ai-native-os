"""租户账号角色在线治理（入离职/调岗/防自锁）与滑动窗口 API 限流回归测试。"""

from __future__ import annotations

from pathlib import Path

from fos.governance.ratelimit import SlidingWindowLimiter


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_sliding_window_limiter_unit() -> None:
    """验证滑动窗口限流器的计数、超限拒绝、窗口滑出恢复及 0=关闭逻辑。"""
    lim = SlidingWindowLimiter(limit=2, window_s=10.0)
    assert lim.check("k1", now=100.0) == (True, 1, 0)
    assert lim.check("k1", now=101.0) == (True, 0, 0)
    ok, rem, retry = lim.check("k1", now=102.0)
    assert ok is False and rem == 0 and retry >= 1
    # 窗口部分滑出后恢复 1 个名额（101.0 仍在窗口内），完全滑出后恢复全部名额
    assert lim.check("k1", now=110.5) == (True, 0, 0)
    assert lim.check("k1", now=125.0) == (True, 1, 0)

    off = SlidingWindowLimiter(limit=0)
    assert off.check("k1", now=100.0) == (True, 0, 0)


def test_http_rate_limit_middleware_and_probe_exemption(client, keys) -> None:
    """验证 HTTP 限流返回 429 与 Retry-After，且 /healthz 探针豁免不受限流影响。"""
    rl = client.app.state.rate_limiter
    old_limit = rl.limit
    rl.limit = 3
    rl._buckets.clear()
    try:
        for expected_rem in ("2", "1", "0"):
            r = client.get("/v1/me", headers=_h(keys, "sales"))
            assert r.status_code == 200
            assert r.headers["x-ratelimit-limit"] == "3"
            assert r.headers["x-ratelimit-remaining"] == expected_rem
        r4 = client.get("/v1/me", headers=_h(keys, "sales"))
        assert r4.status_code == 429
        assert int(r4.headers["retry-after"]) >= 1
        # 健康探针不受限流阻断
        assert client.get("/healthz").status_code == 200
    finally:
        rl.limit = old_limit
        rl._buckets.clear()


def test_ops_users_lifecycle_self_guard_and_tenant_isolation(client, keys) -> None:
    """验证账号创建、角色校验、离职停用瞬间封禁 Key、管理员防自锁与跨租户隔离。"""
    assert client.get("/v1/ops/users", headers=_h(keys, "sales")).status_code == 403
    assert (
        client.post(
            "/v1/ops/users",
            json={"login": "bad_role_u", "display_name": "测试", "roles": ["nonexistent"]},
            headers=_h(keys, "admin"),
        ).status_code
        == 422
    )

    r_new = client.post(
        "/v1/ops/users",
        json={"login": "new_qc", "display_name": "新质检员", "roles": ["qc"]},
        headers=_h(keys, "admin"),
    )
    assert r_new.status_code == 201 and r_new.json()["is_active"] is True

    # 重复登录名返回 409
    assert (
        client.post(
            "/v1/ops/users",
            json={"login": "new_qc", "display_name": "重复", "roles": ["qc"]},
            headers=_h(keys, "admin"),
        ).status_code
        == 409
    )

    # 签发密钥 -> 正常访问 -> 离职停用 -> 瞬间 401 -> 重新启用 -> 恢复 200
    tok = client.post(
        "/v1/ops/keys", json={"login": "new_qc", "label": "首把"}, headers=_h(keys, "admin")
    ).json()["plaintext"]
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200

    r_off = client.patch("/v1/ops/users/new_qc", json={"is_active": False}, headers=_h(keys, "admin"))
    assert r_off.status_code == 200 and r_off.json()["is_active"] is False
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401

    r_on = client.patch(
        "/v1/ops/users/new_qc", json={"is_active": True, "roles": ["qc", "worker"]}, headers=_h(keys, "admin")
    )
    assert r_on.status_code == 200 and r_on.json()["roles"] == ["qc", "worker"]
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200

    # 防自锁保护：禁止管理员停用自身或移除自身 admin 角色
    assert (
        client.patch("/v1/ops/users/admin", json={"is_active": False}, headers=_h(keys, "admin")).status_code
        == 400
    )
    assert (
        client.patch("/v1/ops/users/admin", json={"roles": ["gm"]}, headers=_h(keys, "admin")).status_code
        == 400
    )

    # 跨租户隔离：other_admin 无法修改 luneng 账号
    assert (
        client.patch(
            "/v1/ops/users/new_qc", json={"is_active": False}, headers=_h(keys, "other_admin")
        ).status_code
        == 404
    )

    ops_path = Path(__file__).resolve().parent.parent / "fos/web/static/ops.html"
    assert 'id="userTable"' in ops_path.read_text(encoding="utf-8")
