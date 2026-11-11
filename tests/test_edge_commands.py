"""W21 云端签发 fos-edge 只读命令、厂端状态：签名与 Rust 互通、排队 → 下发 → 结果、重签、作废、Watchdog。"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from fos.monitoring import edge as ec
from fos.monitoring import service as ms

VECTOR = Path(__file__).resolve().parents[1] / "crates/fos-protocol/testdata/signed_command.json"
SEED = bytes([7] * 32)  # 与 crates/fos-edge/src/commands.rs 测试里的私钥相同


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


def test_signing_matches_rust_vector():
    """Python 签出的命令与 Rust 测试用的向量逐字节一致（Rust 侧 verifies_cloud_vector 验签通过）。"""
    v = json.loads(VECTOR.read_text(encoding="utf-8"))
    s = ec.CommandSigner.from_seed(SEED)
    c = s.sign(
        command_id=v["command_id"],
        tenant_id=v["tenant_id"],
        edge_id=v["edge_id"],
        expires_ms=v["expires_ms"],
        command=v["command"],
    )
    assert c == v
    unsigned = {k: c[k] for k in ("protocol", "command_id", "tenant_id", "edge_id", "expires_ms", "command")}
    assert ec.signing_bytes(unsigned).decode() == (
        '{"protocol":1,"command_id":"cmd-测试-1","tenant_id":"luneng","edge_id":"edge-1",'
        '"expires_ms":1762000300000,"command":{"type":"read_now","points":["k1.top","窑顶"]}}'
    )
    assert ec.wire(ec.Resend(type="resend", from_seq=3)) == {"type": "resend", "from_seq": 3}


def test_key_setting():
    assert ec.CommandSigner.from_setting("  ") is None
    with pytest.raises(ValueError):
        ec.CommandSigner.from_setting("不是 base64")
    with pytest.raises(ValueError):
        ec.CommandSigner.from_setting(base64.b64encode(b"short").decode())
    seed, pub = ec.CommandSigner.generate()
    s = ec.CommandSigner.from_setting(seed)
    assert s is not None and s.public_key == pub
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = (
        Ed25519PrivateKey.from_private_bytes(SEED)
        .private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        .decode()
    )
    assert ec.CommandSigner.from_setting(pem).public_key == ec.CommandSigner.from_seed(SEED).public_key


def _ev(client, h, type_, payload, object_id):
    body = {"type": type_, "payload": payload, "object_id": object_id}
    body["evidence"] = [{"kind": "manual", "ref": "test"}]
    r = client.post("/v1/events", json=body, headers=h("admin"))
    assert r.status_code == 201, r.text


def _batch(seq, **extra):
    return {"protocol": 1, "tenant_id": "luneng", "edge_id": "edge-1", "seq": seq, "readings": [], **extra}


def _verify(pub_b64: str, c: dict) -> None:
    unsigned = {k: c[k] for k in ("protocol", "command_id", "tenant_id", "edge_id", "expires_ms", "command")}
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    key.verify(base64.b64decode(c["signature"]), ec.signing_bytes(unsigned))  # 不对会抛异常


def _status(pending=0, oldest_ms=None, rejected=0):
    return {
        "version": "0.1.0",
        "pending_batches": pending,
        "pending_readings": pending * 10,
        "oldest_pending_ms": oldest_ms,
        "rejected_batches": rejected,
        "points": [{"point": "k1.top", "consecutive_errors": 3, "last_error": "超时"}],
    }


def test_edge_command_flow(client, h):
    from fos.monitoring.watch import evaluate

    svc = client.app.state.services
    _ev(client, h, "production.kiln_registered", {"name": "1号窑", "kiln_type": "shuttle",
        "access_path": "retrofit", "max_temp": 1400}, "K1")  # fmt: skip
    base = {"kiln": "K1", "unit": "℃", "kind": "temperature", "status": "active", "interval_s": 60}
    for code, extra in (
        ("k1.top", {"source": "retrofit", "edge_id": "edge-1"}),
        ("k1.door", {"source": "direct", "edge_id": "edge-2"}),
        ("k1.panel", {"source": "manual"}),
    ):
        _ev(client, h, "production.point_registered", {**base, "code": code, "name": code, **extra}, code)

    cmd = {"edge_id": "edge-1", "command": {"type": "report_status"}}
    # 没配私钥：不能下发；回执里也没有命令
    assert client.post("/v1/edge/commands", json=cmd, headers=h("shift_lead")).status_code == 409
    assert client.post("/v1/edge/uplink", json=_batch(1), headers=h("edge")).json()["commands"] == []
    g = client.get("/v1/edge/gateways", headers=h("worker")).json()
    assert g["signing"] is False and [e["edge_id"] for e in g["gateways"]] == ["edge-1", "edge-2"]

    svc.edge_signer = ec.CommandSigner.from_seed(SEED)
    pub = svc.edge_signer.public_key
    # 权限：工人只能看；采集网关的 Key 不能给自己发命令；租户隔离
    assert client.post("/v1/edge/commands", json=cmd, headers=h("worker")).status_code == 403
    assert client.post("/v1/edge/commands", json=cmd, headers=h("edge")).status_code == 403
    assert client.post("/v1/edge/commands", json=cmd, headers=h("other_admin")).status_code in (404, 409)
    bad = [
        ({"edge_id": "edge-9", "command": {"type": "report_status"}}, 404),
        ({"edge_id": "edge-1", "command": {"type": "read_now", "points": ["k1.door"]}}, 422),  # 归 edge-2 采
        ({"edge_id": "edge-1", "command": {"type": "write_register", "addr": 1}}, 422),  # 协议里没有写命令
        ({"edge_id": "edge-1", "command": {"type": "resend", "from_seq": -1}}, 422),
    ]
    for body, code in bad:
        assert client.post("/v1/edge/commands", json=body, headers=h("shift_lead")).status_code == code, body

    read = {"edge_id": "edge-1", "command": {"type": "read_now", "points": ["k1.top"]}}
    r1 = client.post("/v1/edge/commands", json=read, headers=h("shift_lead")).json()
    assert (
        r1["status"] == "pending"
        and r1["text"] == "立即读数：k1.top"
        and r1["created_by_name"] == "shift_lead"
    )
    r2 = client.post(
        "/v1/edge/commands", json={"edge_id": "edge-1", "command": {"type": "resend", "from_seq": 5}},
        headers=h("admin"),
    ).json()  # fmt: skip
    assert client.post(f"/v1/edge/commands/{r2['id']}/cancel", headers=h("shift_lead")).json()["status"] == (
        "cancelled"
    )

    # —— 上行：回执带回签名命令（人要的 read_now + Watchdog 自动要的 report_status） ——
    ack = client.post("/v1/edge/uplink", json=_batch(2), headers=h("edge")).json()
    got = {c["command"]["type"]: c for c in ack["commands"]}
    assert sorted(got) == ["read_now", "report_status"]
    for c in ack["commands"]:
        _verify(pub, c)
        assert c["tenant_id"] == "luneng" and c["edge_id"] == "edge-1"
        now_ms = datetime.now(UTC).timestamp() * 1000
        assert 0 < c["expires_ms"] - now_ms <= 300_000 + 5000  # 有效期 command_ttl_s，远小于厂端上限 1 小时
    assert got["read_now"]["command_id"] == r1["id"]
    # 签名还没过期：下一批不重复带
    assert client.post("/v1/edge/uplink", json=_batch(3), headers=h("edge")).json()["commands"] == []
    # edge-2 的回执里没有 edge-1 的命令
    other = {**_batch(1), "edge_id": "edge-2"}
    assert all(c["edge_id"] == "edge-2" for c in client.post("/v1/edge/uplink", json=other,
               headers=h("edge")).json()["commands"])  # fmt: skip

    # —— 厂端回报结果 + 状态 ——
    oldest = int((datetime.now(UTC) - timedelta(minutes=45)).timestamp() * 1000)
    results = [
        {"command_id": r1["id"], "ok": True, "detail": "已安排立即读 1 个点位"},
        {"command_id": got["report_status"]["command_id"], "ok": True, "detail": "状态随下一批上行"},
        {"command_id": "不存在", "ok": False, "detail": "x"},
    ]
    up = _batch(4, command_results=results, status=_status(pending=3, oldest_ms=oldest, rejected=1))
    assert client.post("/v1/edge/uplink", json=up, headers=h("edge")).status_code == 200
    cs = client.get("/v1/edge/commands", params={"edge_id": "edge-1"}, headers=h("worker")).json()["commands"]
    by = {c["id"]: c for c in cs}
    assert by[r1["id"]]["status"] == "done" and by[r1["id"]]["result"] == "已安排立即读 1 个点位"
    assert by[got["report_status"]["command_id"]]["auto"] is True
    assert by[r2["id"]]["status"] == "cancelled" and by[r2["id"]]["deliveries"] == 0
    # 已完成的不能取消；别的租户看不到
    assert client.post(f"/v1/edge/commands/{r1['id']}/cancel", headers=h("shift_lead")).status_code == 409
    assert client.get("/v1/edge/commands", headers=h("other_admin")).status_code in (200, 404)
    g = client.get("/v1/edge/gateways", headers=h("worker")).json()
    e1 = next(e for e in g["gateways"] if e["edge_id"] == "edge-1")
    assert g["signing"] and g["public_key"] == pub
    assert e1["online"] and e1["points"] == ["k1.top"] and e1["status"]["rejected_batches"] == 1
    assert e1["health"]["failing_points"] == ["k1.top"] and e1["health"]["backlog_minutes"] >= 44
    # 刚报过状态：不再自动要
    assert client.post("/v1/edge/uplink", json=_batch(5), headers=h("edge")).json()["commands"] == []

    # —— Watchdog：网关在线、积压 45 分钟、有被拒批次 → 两条告警 ——
    cfg = svc.monitoring["luneng"]

    async def judge(now):
        async with svc.sessionmaker() as s, s.begin():
            return await evaluate(s, "luneng", cfg, now)

    res = client.portal.call(judge, datetime.now(UTC))
    keys = sorted(a.key for a in res.opened if a.kind.startswith("edge_") and a.edge_id == "edge-1")
    assert keys == ["edge_backlog:edge-1", "edge_rejected:edge-1"]
    assert "3 批" in next(a for a in res.opened if a.kind == "edge_backlog").message
    # 状态恢复正常 → 告警关闭
    client.post("/v1/edge/uplink", json=_batch(6, status=_status()), headers=h("edge"))
    res = client.portal.call(judge, datetime.now(UTC))
    assert sorted(a.key for a in res.closed if a.kind.startswith("edge_")) == keys

    # —— 重签与作废（直接调服务，时间往后拨） ——
    r3 = client.post("/v1/edge/commands", json=read, headers=h("shift_lead")).json()

    async def uplink_at(now, seq, results=()):
        from fos.governance.principal import Principal

        edge_user = Principal("luneng", "edge-user", "user", "edge", ("edge",), frozenset())
        b = ms.UplinkIn.model_validate(_batch(seq, command_results=list(results)))
        async with svc.sessionmaker() as s, s.begin():
            return await ec.after_uplink(s, edge_user, b, cfg, svc.edge_signer, now)

    t0 = datetime.now(UTC)
    first = client.portal.call(uplink_at, t0, 7)
    assert [c["command_id"] for c in first] == [r3["id"]]
    assert client.portal.call(uplink_at, t0 + timedelta(minutes=2), 8) == []
    # 签名过期还没结果：同一个 id 重签，过期时间更新（厂端按 id 去重，只回报结果）
    again = client.portal.call(uplink_at, t0 + timedelta(minutes=6), 9)
    assert [c["command_id"] for c in again] == [r3["id"]] and again[0]["expires_ms"] > first[0]["expires_ms"]
    _verify(pub, again[0])
    # 排队 30 分钟还没结果 → 作废；晚到的结果照收
    client.portal.call(uplink_at, t0 + timedelta(minutes=31), 10)
    rec = next(c for c in client.get("/v1/edge/commands", headers=h("worker")).json()["commands"]
               if c["id"] == r3["id"])  # fmt: skip
    assert rec["status"] == "expired" and rec["deliveries"] == 2 and rec["result"] == "已下发，网关没回报结果"
    late = [ms.CommandResultIn(command_id=r3["id"], ok=False, detail="本网关不采这些点位")]
    client.portal.call(uplink_at, t0 + timedelta(minutes=32), 11, late)
    rec = next(c for c in client.get("/v1/edge/commands", headers=h("worker")).json()["commands"]
               if c["id"] == r3["id"])  # fmt: skip
    assert rec["status"] == "failed" and rec["result"] == "本网关不采这些点位"
