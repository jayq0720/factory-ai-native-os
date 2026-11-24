"""A1-A5 生产级补强全量回归测试：S3/OSS 存储、Web 统一登录会话、Redis 限流、MCP 服务端与压测/前端冒烟。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest

from fos.cli import main
from fos.config import Settings, get_settings
from fos.governance.ratelimit import RedisSlidingWindowLimiter, SlidingWindowLimiter, build_rate_limiter
from fos.perception.blobs import LocalBlobStore, S3BlobStore, build_blob_store, sign_s3_v4

STATIC_DIR = Path(__file__).resolve().parent.parent / "fos" / "web" / "static"
ASSET_RE = re.compile(r"""(?:href|src)=["']([^"']+)["']""")


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


# ---------------- A1: S3 / 阿里云 OSS 兼容对象存储 ----------------


def test_a1_s3_blob_store_sigv4_dedup_and_factory(tmp_path: Path) -> None:
    """验证 S3BlobStore SigV4 签名、HEAD 去重、GET 读取、404 异常、防路径穿越及工厂切换。"""
    bucket_data: dict[str, bytes] = {}
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256 Credential=ak_test/")
        assert "x-amz-date" in req.headers and "x-amz-content-sha256" in req.headers
        key = req.url.path.lstrip("/")
        if req.method == "HEAD":
            return httpx.Response(200 if key in bucket_data else 404)
        if req.method == "PUT":
            bucket_data[key] = req.content
            return httpx.Response(200)
        if req.method == "GET":
            if key not in bucket_data:
                return httpx.Response(404)
            return httpx.Response(200, content=bucket_data[key])
        return httpx.Response(405)

    store = S3BlobStore(
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="fos-prod-blobs",
        region="cn-shanghai",
        access_key="ak_test",
        secret_key="sk_secret",
        prefix="blobs/",
        transport=httpx.MockTransport(handler),
    )
    payload = b"factory-kiln-spec-pdf-bytes"
    d1 = store.put("luneng", payload)
    assert d1 == hashlib.sha256(payload).hexdigest()
    assert store.uri("luneng", d1) == f"s3://fos-prod-blobs/blobs/luneng/{d1[:2]}/{d1}"
    assert store.get("luneng", d1) == payload

    # 同一文件重复上传触发 HEAD 命中跳过 PUT（内容寻址去重）
    put_count_before = sum(1 for m, _ in calls if m == "PUT")
    assert store.put("luneng", payload) == d1
    assert sum(1 for m, _ in calls if m == "PUT") == put_count_before

    with pytest.raises(FileNotFoundError):
        store.get("luneng", "0" * 64)
    with pytest.raises(ValueError):
        store.get("../evil", d1)

    hdrs = sign_s3_v4(
        method="GET",
        url="https://oss-cn-shanghai.aliyuncs.com/fos-prod-blobs/test",
        region="cn-shanghai",
        access_key="ak",
        secret_key="sk",
        payload_sha256=hashlib.sha256(b"").hexdigest(),
    )
    assert "Signature=" in hdrs["Authorization"]

    local_st = build_blob_store(Settings(blob_backend="local", blob_dir=tmp_path / "b"))
    assert isinstance(local_st, LocalBlobStore)
    s3_st = build_blob_store(
        Settings(
            blob_backend="s3",
            s3_endpoint="https://oss-cn-shanghai.aliyuncs.com",
            s3_bucket="fos-b",
            s3_access_key="ak",
            s3_secret_key="sk",
        )
    )
    assert isinstance(s3_st, S3BlobStore)


# ---------------- A2: Web 统一登录与 HttpOnly 会话 Cookie ----------------


def test_a2_unified_web_login_cookie_and_revocation(client, keys) -> None:
    """验证 /v1/auth/login 签发 HttpOnly Cookie、免 Bearer 鉴权、登出清除及密钥吊销即时失效。"""
    # 1. 登录页可直接访问
    r_page = client.get("/app/login.html")
    assert r_page.status_code == 200 and "统一登录" in r_page.text

    # 2. 签发临时 API Key 用于测试登录与吊销联动
    r_new = client.post(
        "/v1/ops/keys",
        json={"login": "admin", "label": "web-session-test"},
        headers=_h(keys, "admin"),
    )
    assert r_new.status_code == 201
    tmp_key = r_new.json()["plaintext"]
    tmp_kid = r_new.json()["key_id"]

    # 3. 无认证访问 /v1/auth/session 返回 401
    assert client.get("/v1/auth/session").status_code == 401

    # 4. POST /v1/auth/login 签发 HttpOnly Cookie
    r_login = client.post("/v1/auth/login", json={"api_key": tmp_key})
    assert r_login.status_code == 200
    assert r_login.json()["authenticated"] is True
    assert "httponly" in r_login.headers.get("set-cookie", "").lower()

    # 5. 仅凭 Cookie 即可调用 /v1/me 与 /v1/auth/session（无需 Authorization 头）
    assert client.get("/v1/me").status_code == 200
    assert client.get("/v1/auth/session").json()["authenticated"] is True

    # 6. 吊销底层 API Key 后，已有 Cookie 立即失效（401）
    r_rev = client.post(f"/v1/ops/keys/{tmp_kid}/revoke", headers=_h(keys, "admin"))
    assert r_rev.status_code == 200
    assert client.get("/v1/me").status_code == 401

    # 7. 重新用正常 Key 登录并登出
    assert client.post("/v1/auth/login", json={"api_key": keys["admin"]}).status_code == 200
    assert client.get("/v1/me").status_code == 200
    r_out = client.post("/v1/auth/logout")
    assert r_out.status_code == 200 and r_out.json()["authenticated"] is False
    assert client.get("/v1/me").status_code == 401


# ---------------- A3: 分布式 Redis 滑动窗口限流器与自动降级 ----------------


def test_a3_redis_rate_limiter_and_fallback() -> None:
    """验证 Redis 分布式限流判定、故障自动降级至本地滑动窗口以及工厂切换。"""
    hits: dict[str, int] = {}

    def fake_eval(rkey: str, _now_ms: int, _win_ms: int, limit: int) -> tuple[bool, int, int]:
        hits[rkey] = hits.get(rkey, 0) + 1
        if hits[rkey] > limit:
            return False, 0, 12
        return True, limit - hits[rkey], 0

    rl = RedisSlidingWindowLimiter(2, "redis://127.0.0.1:6379/0", eval_fn=fake_eval)
    assert rl.check("key:abc")[0] is True
    assert rl.check("key:abc")[0] is True
    ok, rem, retry = rl.check("key:abc")
    assert ok is False and rem == 0 and retry == 12

    # Redis 不可达时自动降级至本地滑动窗口，不中断服务
    def broken_eval(*_args):
        raise ConnectionRefusedError("redis down")

    rl_down = RedisSlidingWindowLimiter(1, "redis://127.0.0.1:1/0", eval_fn=broken_eval)
    assert rl_down.check("ip:1.2.3.4", now=100.0)[0] is True
    assert rl_down.check("ip:1.2.3.4", now=100.1)[0] is False

    assert isinstance(build_rate_limiter(100, ""), SlidingWindowLimiter)
    assert isinstance(build_rate_limiter(100, "redis://redis:6379/0"), RedisSlidingWindowLimiter)


# ---------------- A4: 标准 MCP Server（JSON-RPC 2.0） ----------------


def test_a4_mcp_server_jsonrpc_and_rbac(client, keys) -> None:
    """验证 /v1/mcp 标准协议：initialize、tools/list、tools/call、权限隔离与错误码。"""
    # 未认证拒绝
    assert client.post("/v1/mcp", json={"id": 1, "method": "initialize"}).status_code == 401

    # initialize
    r_init = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=_h(keys, "admin"),
    )
    assert r_init.status_code == 200
    res_init = r_init.json()["result"]
    assert res_init["protocolVersion"] == "2024-11-05"
    assert res_init["serverInfo"]["name"] == "factory-ai-native-os"

    # tools/list
    r_list = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=_h(keys, "admin"),
    )
    assert r_list.status_code == 200
    tools = r_list.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"ontology.describe", "objects.list"}.issubset(names)
    assert all("inputSchema" in t and "description" in t for t in tools)

    # tools/call：正常只读查询（ontology.describe）
    r_call = client.post(
        "/v1/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ontology.describe", "arguments": {}},
        },
        headers=_h(keys, "admin"),
    )
    assert r_call.status_code == 200
    c_res = r_call.json()["result"]
    assert c_res["isError"] is False
    parsed = json.loads(c_res["content"][0]["text"])
    assert "objects" in parsed and len(parsed["objects"]) > 0

    # 未知工具 -> -32602；未知方法 -> -32601
    r_bad_tool = client.post(
        "/v1/mcp",
        json={"id": 4, "method": "tools/call", "params": {"name": "no.such.tool"}},
        headers=_h(keys, "admin"),
    )
    assert r_bad_tool.json()["error"]["code"] == -32602
    r_bad_method = client.post(
        "/v1/mcp",
        json={"id": 5, "method": "resources/unknown"},
        headers=_h(keys, "admin"),
    )
    assert r_bad_method.json()["error"]["code"] == -32601


# ---------------- A5: 全站 17 页前端路由冒烟 + 账本性能基准 CLI ----------------


def test_a5_all_frontend_pages_smoke_and_ledger_benchmark(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证全部 17 个静态工作台页面 HTTP 200 且静态资源无断链，并验证 fos benchmark-ledger CLI。"""
    html_files = sorted(STATIC_DIR.glob("*.html"))
    assert len(html_files) >= 17
    for page in html_files:
        r = client.get(f"/app/{page.name}")
        assert r.status_code == 200, f"/app/{page.name} 返回 {r.status_code}"
        for ref in ASSET_RE.findall(r.text):
            if ref.startswith(("http://", "https://", "/", "#", "data:", "mailto:")):
                continue
            r_asset = client.get(f"/app/{ref.split('?')[0]}")
            assert r_asset.status_code == 200, f"{page.name} 引用资源 {ref} 返回 {r_asset.status_code}"

    # 账本性能基准 CLI（写入 + 实时审计 + 防篡改灾备导出与校验）
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bench.db'}"
    monkeypatch.setenv("FOS_DATABASE_URL", db_url)
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        capsys.readouterr()
        assert main(["benchmark-ledger", "bench_t1", "--events", "150", "--objects", "15", "--json"]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["events_written"] == 150
        assert rep["audit_ok"] is True and rep["verify_backup_ok"] is True
        assert rep["write_events_per_sec"] > 0

        # 非空租户默认拒绝压测污染，需显式 --allow-nonempty
        assert main(["benchmark-ledger", "bench_t1", "--events", "10"]) == 1
        assert main(["benchmark-ledger", "bench_t1", "--events", "10", "--allow-nonempty"]) == 0
    finally:
        get_settings.cache_clear()
