"""云端发给 fos-edge 的只读命令、厂端状态（W21，方案 7.2、2.4.6 Watchdog）。

- 协议里只有三条命令：read_now / report_status / resend（规则 7：没有写设备的命令）；
- 命令先排队，网关下次上行时随回执带下去，**下发时**用 Ed25519 私钥签名（有效期 command_ttl_s）；
  私钥只从环境变量 FOS_EDGE_COMMAND_KEY / KMS 来（规则 2），厂端 edge.toml 只放公钥；
- 厂端执行后把结果放进下一批上行（command_results）；签名过期还没结果就用同一个 id 重签，
  厂端按 id 去重，只回报上次的结果，不会执行两遍；排队 queue_minutes 还没结果就作废；
- 状态（积压、被拒批次、点位健康）随上行批次附带，存在 edge_batches.status；
  Watchdog 每 status_every_minutes 自动要一次，积压过久 / 有被拒批次时告警（watch.py）。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import EdgeBatch, EdgeCommandRecord, utcnow
from fos.governance.principal import Principal
from fos.monitoring import service as ms
from fos.monitoring.config import MonitorConfig

OPEN = ("pending", "sent")
STATUS_NAME = {
    "pending": "待下发",
    "sent": "已下发",
    "done": "已完成",
    "failed": "失败",
    "expired": "已作废",
    "cancelled": "已取消",
}
COMMAND_NAME = {"read_now": "立即读数", "report_status": "上报状态", "resend": "重新补传"}


# ---------------- 签名 ----------------


class CommandSigner:
    """Ed25519 签名。被签名的字节与 crates/fos-protocol 的 SignedCommand::signing_bytes 一致。"""

    def __init__(self, key: Ed25519PrivateKey):
        self._key = key

    @classmethod
    def from_seed(cls, seed: bytes) -> CommandSigner:
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def from_setting(cls, value: str) -> CommandSigner | None:
        """FOS_EDGE_COMMAND_KEY：32 字节种子的 base64，或 PEM 全文。留空返回 None；格式不对直接报错。"""
        v = value.strip()
        if not v:
            return None
        if v.startswith("-----BEGIN"):
            k = serialization.load_pem_private_key(v.encode(), password=None)
            if not isinstance(k, Ed25519PrivateKey):
                raise ValueError("FOS_EDGE_COMMAND_KEY 不是 Ed25519 私钥")
            return cls(k)
        try:
            seed = base64.b64decode(v, validate=True)
        except ValueError as e:
            raise ValueError("FOS_EDGE_COMMAND_KEY 不是合法的 base64") from e
        if len(seed) != 32:
            raise ValueError("FOS_EDGE_COMMAND_KEY 应为 32 字节 Ed25519 种子的 base64")
        return cls.from_seed(seed)

    @staticmethod
    def generate() -> tuple[str, str]:
        """生成一对新密钥：(私钥种子 base64, 公钥 base64)。"""
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        return base64.b64encode(seed).decode(), CommandSigner(k).public_key

    @property
    def public_key(self) -> str:
        """公钥 base64：填进 edge.toml 的 command_public_key（公钥不是密钥，可以展示）。"""
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def sign(
        self, *, command_id: str, tenant_id: str, edge_id: str, expires_ms: int, command: dict[str, Any]
    ) -> dict[str, Any]:
        unsigned = {
            "protocol": ms.PROTOCOL_VERSION,
            "command_id": command_id,
            "tenant_id": tenant_id,
            "edge_id": edge_id,
            "expires_ms": expires_ms,
            "command": command,
        }
        sig = self._key.sign(signing_bytes(unsigned))
        return {**unsigned, "signature": base64.b64encode(sig).decode()}


def signing_bytes(unsigned: dict[str, Any]) -> bytes:
    """紧凑 JSON、不转义中文、字段按声明顺序（command 里 type 在最前）。"""
    return json.dumps(unsigned, ensure_ascii=False, separators=(",", ":")).encode()


# ---------------- 命令 ----------------


class ReadNow(BaseModel):
    type: Literal["read_now"]
    points: list[str] = Field(min_length=1, max_length=50)


class ReportStatus(BaseModel):
    type: Literal["report_status"]


class Resend(BaseModel):
    type: Literal["resend"]
    from_seq: int = Field(ge=0)


Command = Annotated[ReadNow | ReportStatus | Resend, Field(discriminator="type")]


def wire(cmd: ReadNow | ReportStatus | Resend) -> dict[str, Any]:
    """协议里的 JSON（type 在最前，字段顺序与 Rust 枚举一致）。"""
    if isinstance(cmd, ReadNow):
        return {"type": "read_now", "points": list(cmd.points)}
    if isinstance(cmd, Resend):
        return {"type": "resend", "from_seq": cmd.from_seq}
    return {"type": "report_status"}


def describe(c: dict[str, Any]) -> str:
    name = COMMAND_NAME.get(c.get("type", ""), c.get("type", ""))
    if c.get("type") == "read_now":
        return f"{name}：{'、'.join(c.get('points', []))}"
    if c.get("type") == "resend":
        return f"{name}：从序号 {c.get('from_seq')} 起"
    return name


async def known_edges(session: AsyncSession, tenant_id: str) -> dict[str, list[ms.Point]]:
    """测点登记过的网关 + 上行过的网关 → 它的（非人工）测点。"""
    out: dict[str, list[ms.Point]] = {}
    for p in await ms.active_points(session, tenant_id):
        if p.edge_id and p.source != "manual":
            out.setdefault(p.edge_id, []).append(p)
    rows = await session.execute(select(EdgeBatch.edge_id).where(EdgeBatch.tenant_id == tenant_id).distinct())
    for (e,) in rows:
        out.setdefault(e, [])
    return out


async def issue(
    session: AsyncSession,
    principal: Principal,
    edge_id: str,
    cmd: ReadNow | ReportStatus | Resend,
    cfg: MonitorConfig,
    signer: CommandSigner | None,
    *,
    auto: bool = False,
    now: datetime | None = None,
) -> EdgeCommandRecord:
    if signer is None:
        raise ms.MonitorError("云端没有配置命令签名私钥（环境变量 FOS_EDGE_COMMAND_KEY），不能下发命令", 409)
    now = now or utcnow()
    tid = principal.tenant_id
    edges = await known_edges(session, tid)
    if edge_id not in edges:
        raise ms.MonitorError(f"没有采集网关 {edge_id}（测点里没登记、也没上行过）", 404)
    if isinstance(cmd, ReadNow):
        mine = {p.code for p in edges[edge_id]}
        bad = [x for x in cmd.points if x not in mine]
        if bad:
            raise ms.MonitorError(f"这些测点不归网关 {edge_id} 采：{'、'.join(bad)}")
    rec = EdgeCommandRecord(
        tenant_id=tid,
        edge_id=edge_id,
        command=wire(cmd),
        status="pending",
        deliveries=0,
        result="",
        auto=auto,
        created_by=principal.subject_id,
        created_by_name=principal.display_name,
        created_at=now,
        expires_at=now + timedelta(minutes=cfg.edge.queue_minutes),
    )
    session.add(rec)
    await session.flush()
    return rec


async def cancel(session: AsyncSession, tenant_id: str, command_id: str) -> EdgeCommandRecord:
    rec = await session.get(EdgeCommandRecord, command_id)
    if rec is None or rec.tenant_id != tenant_id:
        raise ms.MonitorError("命令不存在", 404)
    if rec.status != "pending":
        raise ms.MonitorError(
            f"命令{STATUS_NAME.get(rec.status, rec.status)}，不能取消（只能取消待下发的）", 409
        )
    rec.status, rec.finished_at, rec.result = "cancelled", utcnow(), "下发前取消"
    await session.flush()
    return rec


async def _expire(session: AsyncSession, tenant_id: str, edge_id: str | None, now: datetime) -> None:
    q = select(EdgeCommandRecord).where(
        EdgeCommandRecord.tenant_id == tenant_id,
        EdgeCommandRecord.status.in_(OPEN),
        EdgeCommandRecord.expires_at <= now,
    )
    if edge_id is not None:
        q = q.where(EdgeCommandRecord.edge_id == edge_id)
    for rec in (await session.execute(q)).scalars():
        rec.result = "已下发，网关没回报结果" if rec.deliveries else "网关一直没上行，命令没下发"
        rec.status, rec.finished_at = "expired", now


async def apply_results(
    session: AsyncSession, tenant_id: str, edge_id: str, results: list[ms.CommandResultIn], now: datetime
) -> None:
    """厂端回报的结果。已作废的也收（晚到的结果比没有好）；已完成 / 取消的不改。"""
    for r in results:
        rec = await session.get(EdgeCommandRecord, r.command_id)
        if rec is None or rec.tenant_id != tenant_id or rec.edge_id != edge_id:
            continue
        if rec.status not in (*OPEN, "expired"):
            continue
        rec.status = "done" if r.ok else "failed"
        rec.result, rec.finished_at = r.detail[:500], now


async def latest_status(session: AsyncSession, tenant_id: str, edge_id: str) -> EdgeBatch | None:
    return (
        await session.execute(
            select(EdgeBatch)
            .where(
                EdgeBatch.tenant_id == tenant_id,
                EdgeBatch.edge_id == edge_id,
                EdgeBatch.status.is_not(None),
            )
            .order_by(EdgeBatch.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _auto_status(
    session: AsyncSession,
    principal: Principal,
    edge_id: str,
    cfg: MonitorConfig,
    signer: CommandSigner,
    now: datetime,
) -> None:
    every = cfg.edge.status_every_minutes
    if every <= 0:
        return
    last = await latest_status(session, principal.tenant_id, edge_id)
    if last is not None and now - ms.aware(last.received_at) < timedelta(minutes=every):
        return
    waiting = await session.scalar(
        select(func.count())
        .select_from(EdgeCommandRecord)
        .where(
            EdgeCommandRecord.tenant_id == principal.tenant_id,
            EdgeCommandRecord.edge_id == edge_id,
            EdgeCommandRecord.status.in_(OPEN),
            EdgeCommandRecord.auto.is_(True),
        )
    )
    if waiting:
        return
    watchdog = Principal(
        tenant_id=principal.tenant_id,
        subject_id="watchdog",
        kind="system",
        display_name="Watchdog（定时）",
        roles=(),
        permissions=frozenset(),
    )
    await issue(
        session, watchdog, edge_id, ReportStatus(type="report_status"), cfg, signer, auto=True, now=now
    )


async def after_uplink(
    session: AsyncSession,
    principal: Principal,
    batch: ms.UplinkIn,
    cfg: MonitorConfig,
    signer: CommandSigner | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """上行入库后：收结果、作废过期的、需要时自动要状态，再把该发的命令签名带回去。"""
    now = now or utcnow()
    tid, edge = principal.tenant_id, batch.edge_id
    if batch.command_results:
        await apply_results(session, tid, edge, batch.command_results, now)
    await _expire(session, tid, edge, now)
    if signer is None:
        return []
    await _auto_status(session, principal, edge, cfg, signer, now)
    ttl = timedelta(seconds=cfg.edge.command_ttl_s)
    rows = (
        await session.execute(
            select(EdgeCommandRecord)
            .where(
                EdgeCommandRecord.tenant_id == tid,
                EdgeCommandRecord.edge_id == edge,
                EdgeCommandRecord.status.in_(OPEN),
                or_(EdgeCommandRecord.sent_at.is_(None), EdgeCommandRecord.sent_at <= now - ttl),
            )
            .order_by(EdgeCommandRecord.created_at)
            .limit(20)
        )
    ).scalars()
    out = []
    for rec in rows:
        # 上一次签名过期了还没结果 → 同一个 id 重签（厂端按 id 去重，只回报结果）
        out.append(
            signer.sign(
                command_id=rec.id,
                tenant_id=tid,
                edge_id=edge,
                expires_ms=int((now + ttl).timestamp() * 1000),
                command=rec.command,
            )
        )
        rec.status, rec.sent_at, rec.deliveries = "sent", now, rec.deliveries + 1
    await session.flush()
    return out


# ---------------- 查询 ----------------


def command_out(rec: EdgeCommandRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "edge_id": rec.edge_id,
        "command": rec.command,
        "text": describe(rec.command),
        "status": rec.status,
        "status_name": STATUS_NAME.get(rec.status, rec.status),
        "result": rec.result,
        "deliveries": rec.deliveries,
        "auto": rec.auto,
        "created_by_name": rec.created_by_name,
        "created_at": ms.aware(rec.created_at).isoformat(),
        "sent_at": ms.aware(rec.sent_at).isoformat() if rec.sent_at else None,
        "finished_at": ms.aware(rec.finished_at).isoformat() if rec.finished_at else None,
    }


async def commands(
    session: AsyncSession, tenant_id: str, edge_id: str | None = None, limit: int = 50
) -> list[EdgeCommandRecord]:
    await _expire(session, tenant_id, edge_id, utcnow())
    q = select(EdgeCommandRecord).where(EdgeCommandRecord.tenant_id == tenant_id)
    if edge_id:
        q = q.where(EdgeCommandRecord.edge_id == edge_id)
    q = q.order_by(EdgeCommandRecord.created_at.desc()).limit(limit)
    return list((await session.execute(q)).scalars())


@dataclass
class Health:
    """厂端状态解读（Watchdog 用）：积压多久、被拒几批、哪些点位连续失败。"""

    backlog_minutes: float | None
    rejected_batches: int
    failing_points: list[str]


def health(status: dict[str, Any], at: datetime) -> Health:
    oldest = status.get("oldest_pending_ms")
    backlog = None
    if oldest is not None and status.get("pending_batches", 0) > 0:
        backlog = max(0.0, (at - ms.from_ms(oldest)).total_seconds() / 60)
    failing = [p["point"] for p in status.get("points", []) if p.get("consecutive_errors", 0) > 0]
    return Health(backlog, int(status.get("rejected_batches", 0)), failing)


async def gateways(
    session: AsyncSession, tenant_id: str, cfg: MonitorConfig, signer: CommandSigner | None
) -> dict[str, Any]:
    now = utcnow()
    edges = await known_edges(session, tenant_id)
    seen = await ms.edge_last_seen(session, tenant_id)
    recent = await commands(session, tenant_id, limit=200)
    out = []
    for e in sorted(edges):
        pts = edges[e]
        last = seen.get(e)
        st = await latest_status(session, tenant_id, e)
        h = health(st.status, ms.aware(st.received_at)) if st and st.status else None
        out.append(
            {
                "edge_id": e,
                "points": [p.code for p in pts],
                "data_mode": "mock" if pts and all(p.data_mode == "mock" for p in pts) else "real",
                "last_seen": last.isoformat() if last else None,
                "online": last is not None
                and now - last <= timedelta(minutes=cfg.alarms.edge_offline_minutes),
                "status": st.status if st else None,
                "status_at": ms.aware(st.received_at).isoformat() if st else None,
                "health": h.__dict__ if h else None,
                "commands": [command_out(c) for c in recent if c.edge_id == e][:5],
            }
        )
    return {
        "signing": signer is not None,
        "public_key": signer.public_key if signer else None,
        "gateways": out,
    }
