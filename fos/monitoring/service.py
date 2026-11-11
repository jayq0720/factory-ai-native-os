"""窑炉监控服务：读数入库（fos-edge 上行 / 人工抄表）、窑况、窑次曲线、结束窑次、能耗、告警确认。

- 读数存 point_readings（时序，不走账本）；测点、窑炉、窑次是本体对象，读的是投影；
- 结束窑次时按读数算最高温度、超差、能耗，以操作人身份 Ledger.append()（规则 5），证据指向读数区间；
- 模拟读数（data_mode=mock，演示用）算出来的结果同样带 mock，不能当正式记录（规则 4）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import EdgeBatch, KilnAlarm, ObjectState, PointReading, utcnow
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, Evidence, Ledger
from fos.monitoring import curve as cv
from fos.monitoring.config import Curve, MonitorConfig

PROTOCOL_VERSION = 1  # 与 crates/fos-protocol 的 PROTOCOL_VERSION 一致
FINISH_EVENT = "production.kiln_run_finished"
SUMMARY_EVENT = "production.kiln_run_summarized"
KIND_NAME = {
    "temperature": "温度",
    "pressure": "压力",
    "atmosphere": "气氛",
    "electricity": "电表",
    "gas": "燃气表",
    "other": "其他",
}
SOURCE_NAME = {"direct": "直连", "retrofit": "加装采集", "manual": "人工抄表"}
METER_KINDS = {"electricity": "electricity_kwh", "gas": "gas_m3"}


class MonitorError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite 读回来不带时区


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


# ---------------- 协议（与 crates/fos-protocol 的 JSON 一致） ----------------


class ReadingIn(BaseModel):
    point: str = Field(min_length=1, max_length=64)
    ts_ms: int
    value: float
    unit: str = Field(max_length=16)
    source: Literal["direct", "retrofit", "manual"]
    quality: Literal["good", "estimated", "bad"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: str | None = Field(default=None, max_length=512)
    mock: bool = False  # 仪表模拟器 / 演示读数（fos-edge mode = "mock"）


class PointHealthIn(BaseModel):
    point: str = Field(max_length=64)
    last_ok_ms: int | None = None
    last_error: str | None = Field(default=None, max_length=500)
    consecutive_errors: int = Field(default=0, ge=0)


class EdgeStatusIn(BaseModel):
    """厂端状态（EdgeStatus）：云端要了才附带。"""

    version: str = Field(max_length=32)
    pending_batches: int = Field(ge=0)
    pending_readings: int = Field(ge=0)
    oldest_pending_ms: int | None = None
    rejected_batches: int = Field(ge=0)
    points: list[PointHealthIn] = Field(default_factory=list, max_length=5000)


class CommandResultIn(BaseModel):
    command_id: str = Field(max_length=64)
    ok: bool
    detail: str = Field(default="", max_length=500)


class UplinkIn(BaseModel):
    protocol: int
    tenant_id: str
    edge_id: str = Field(min_length=1, max_length=64)
    seq: int = Field(ge=0)
    backfill: bool = False
    readings: list[ReadingIn] = Field(max_length=5000)
    status: EdgeStatusIn | None = None
    command_results: list[CommandResultIn] = Field(default_factory=list, max_length=100)

    @field_validator("protocol")
    @classmethod
    def _proto(cls, v: int) -> int:
        if v != PROTOCOL_VERSION:
            raise ValueError(f"不支持的协议版本 {v}（云端是 v{PROTOCOL_VERSION}）")
        return v


# ---------------- 测点 ----------------


@dataclass
class Point:
    id: str
    code: str
    name: str
    kiln: str | None
    kind: str
    unit: str
    source: str
    edge_id: str | None
    interval_s: int
    low: float | None
    high: float | None
    curve_role: bool
    data_mode: str

    @classmethod
    def of(cls, o: ObjectState) -> Point:
        s = o.state
        return cls(
            id=o.object_id,
            code=s["code"],
            name=s.get("name") or s["code"],
            kiln=s.get("kiln"),
            kind=s.get("kind", "other"),
            unit=s.get("unit", ""),
            source=s.get("source", "direct"),
            edge_id=s.get("edge_id") or None,
            interval_s=int(s.get("interval_s") or 60),
            low=s.get("low_limit"),
            high=s.get("high_limit"),
            curve_role=bool(s.get("curve_role")),
            data_mode=o.data_mode,
        )


async def objects(session: AsyncSession, tenant_id: str, object_type: str) -> list[ObjectState]:
    q = select(ObjectState).where(ObjectState.tenant_id == tenant_id, ObjectState.object_type == object_type)
    return list(await session.scalars(q))


async def active_points(session: AsyncSession, tenant_id: str) -> list[Point]:
    rows = await objects(session, tenant_id, "monitor_point")
    return [Point.of(o) for o in rows if o.state.get("status", "active") == "active"]


async def find_point(session: AsyncSession, tenant_id: str, key: str) -> Point:
    """按测点编号、对象 id 或名称找一个在用测点。"""
    pts = await active_points(session, tenant_id)
    hits = [p for p in pts if key in (p.code, p.id)] or [p for p in pts if p.name == key]
    if not hits:
        hits = [p for p in pts if key in p.name]
    if len(hits) != 1:
        names = "、".join(f"{p.name}（{p.code}）" for p in hits[:5])
        raise MonitorError(f"“{key}”对应多个测点：{names}" if hits else f"没有找到在用的测点“{key}”", 404)
    return hits[0]


# ---------------- 读数入库 ----------------


async def ingest(session: AsyncSession, principal: Principal, batch: UplinkIn) -> dict[str, Any]:
    """fos-edge 上行一批读数。按 (edge_id, seq) 去重；未登记 / 已停用 / 单位不符的点位逐条拒绝。"""
    tid = principal.tenant_id
    if batch.tenant_id != tid:
        raise MonitorError("批次的租户与 API Key 不一致", 403)
    pts = {p.code: p for p in await active_points(session, tid)}
    rejected: dict[str, str] = {}
    rows: list[PointReading] = []
    now = utcnow()
    for r in batch.readings:
        p = pts.get(r.point)
        if p is None:
            rejected[r.point] = "测点未登记或已停用"
            continue
        if p.source != "manual" and p.edge_id and p.edge_id != batch.edge_id:
            rejected[r.point] = f"测点登记的采集网关是 {p.edge_id}"
            continue
        if r.unit != p.unit:
            rejected[r.point] = f"单位 {r.unit} 与测点登记的 {p.unit} 不一致"
            continue
        if r.mock and p.data_mode != "mock":
            rejected[r.point] = "模拟读数不能写入正式测点（规则 4）"
            continue
        ts = from_ms(r.ts_ms)
        if ts > now + timedelta(minutes=5):
            rejected[r.point] = "采样时间在未来（检查网关时钟）"
            continue
        rows.append(
            PointReading(
                tenant_id=tid,
                point=p.code,
                ts=ts,
                value=r.value,
                unit=r.unit,
                source=r.source,
                quality=r.quality,
                confidence=r.confidence,
                evidence=r.evidence,
                edge_id=batch.edge_id,
                data_mode=p.data_mode,
                received_at=now,
            )
        )
    rec = EdgeBatch(
        tenant_id=tid,
        edge_id=batch.edge_id,
        seq=batch.seq,
        backfill=batch.backfill,
        accepted=len(rows),
        rejected=rejected,
        status=batch.status.model_dump() if batch.status else None,
        received_by=principal.subject_id,
        received_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(rec)
    except IntegrityError:
        return {"duplicate": True, "seq": batch.seq, "accepted": 0, "rejected": {}}
    session.add_all(rows)
    await session.flush()
    return {"duplicate": False, "seq": batch.seq, "accepted": len(rows), "rejected": rejected}


async def record_manual(
    session: AsyncSession,
    principal: Principal,
    key: str,
    value: float,
    *,
    ts: datetime | None = None,
    evidence: str | None = None,
    confidence: float | None = None,
    quality: Literal["good", "estimated"] = "good",
) -> tuple[Point, PointReading]:
    """路径 C：人工报数 / 拍照读数。报数人本人确认了数值（照片读数先在卡片里确认）。"""
    p = await find_point(session, principal.tenant_id, key)
    now = utcnow()
    ts = aware(ts) if ts else now
    if ts > now + timedelta(minutes=5):
        raise MonitorError("抄表时间不能在未来")
    row = PointReading(
        tenant_id=principal.tenant_id,
        point=p.code,
        ts=ts,
        value=value,
        unit=p.unit,
        source="manual",
        quality=quality,
        confidence=confidence,
        evidence=evidence,
        recorded_by=principal.subject_id,
        data_mode=p.data_mode,
        received_at=now,
    )
    session.add(row)
    await session.flush()
    return p, row


async def samples(
    session: AsyncSession, tenant_id: str, code: str, start: datetime, end: datetime, limit: int = 20000
) -> list[tuple[cv.Sample, str]]:
    q = (
        select(PointReading.ts, PointReading.value, PointReading.quality, PointReading.data_mode)
        .where(
            PointReading.tenant_id == tenant_id,
            PointReading.point == code,
            PointReading.ts >= start,
            PointReading.ts <= end,
        )
        .order_by(PointReading.ts)
        .limit(limit)
    )
    return [(cv.Sample(aware(ts), v, qual), mode) for ts, v, qual, mode in await session.execute(q)]


async def latest(session: AsyncSession, tenant_id: str, code: str) -> PointReading | None:
    q = (
        select(PointReading)
        .where(PointReading.tenant_id == tenant_id, PointReading.point == code)
        .order_by(PointReading.ts.desc())
        .limit(1)
    )
    return (await session.scalars(q)).first()


async def edge_last_seen(session: AsyncSession, tenant_id: str) -> dict[str, datetime]:
    q = (
        select(EdgeBatch.edge_id, func.max(EdgeBatch.received_at))
        .where(EdgeBatch.tenant_id == tenant_id)
        .group_by(EdgeBatch.edge_id)
    )
    return {e: aware(t) for e, t in await session.execute(q)}


def reading_out(r: PointReading) -> dict[str, Any]:
    return {
        "ts": aware(r.ts).isoformat(),
        "value": r.value,
        "unit": r.unit,
        "source": r.source,
        "quality": r.quality,
        "confidence": r.confidence,
        "evidence": r.evidence,
        "edge_id": r.edge_id,
        "data_mode": r.data_mode,
    }


# ---------------- 窑次与烧成制度 ----------------


def run_start(o: ObjectState) -> datetime:
    return aware(datetime.fromisoformat(o.state["started_at"]))


def run_end(o: ObjectState) -> datetime | None:
    e = o.state.get("ended_at")
    return aware(datetime.fromisoformat(e)) if e else None


async def firing_runs(session: AsyncSession, tenant_id: str) -> dict[str, ObjectState]:
    """每个窑炉正在烧的窑次（status=firing，有多个取最近开始的）。"""
    out: dict[str, ObjectState] = {}
    for o in await objects(session, tenant_id, "kiln_run"):
        if o.state.get("status", "firing") != "firing" or o.state.get("ended_at"):
            continue
        k = o.state.get("kiln")
        if k and (k not in out or run_start(o) > run_start(out[k])):
            out[k] = o
    return out


async def curve_for_run(
    session: AsyncSession, tenant_id: str, cfg: MonitorConfig, run: ObjectState
) -> tuple[str | None, Curve | None, str]:
    """窑次用哪条烧成制度：窑次上已定 → 批次配方 → 窑炉默认。返回 (编号, 制度, 来源说明)。"""
    code = run.state.get("firing_curve")
    if code:
        return code, cfg.curves.get(code), "窑次"
    recipes = {o.object_id: o for o in await objects(session, tenant_id, "recipe")}
    found: Counter[str] = Counter()
    for b in await objects(session, tenant_id, "production_batch"):
        if b.state.get("kiln_run") != run.object_id:
            continue
        r = recipes.get(b.state.get("recipe") or "")
        if r is not None and r.state.get("firing_curve") in cfg.curves:
            found[r.state["firing_curve"]] += 1
    if found:
        code = found.most_common(1)[0][0]
        return code, cfg.curves[code], "批次配方"
    kiln = await session.get(ObjectState, (tenant_id, "kiln", run.state.get("kiln", "")))
    code = kiln.state.get("firing_curve") if kiln else None
    if code:
        return code, cfg.curves.get(code), "窑炉默认"
    return None, None, ""


async def run_curve(
    session: AsyncSession,
    tenant_id: str,
    cfg: MonitorConfig,
    run_id: str,
    now: datetime | None = None,
    *,
    until: datetime | None = None,
) -> dict[str, Any]:
    """一个窑次的曲线：参与比对的温度点读数 + 目标曲线 + 超差区间 + 能耗（画图和结束窑次共用）。"""
    run = await session.get(ObjectState, (tenant_id, "kiln_run", run_id))
    if run is None:
        raise MonitorError(f"窑次 {run_id} 不存在", 404)
    now = now or utcnow()
    start = run_start(run)
    end = until or run_end(run) or now
    code, curve, curve_from = await curve_for_run(session, tenant_id, cfg, run)
    pts = [p for p in await active_points(session, tenant_id) if p.kiln == run.state.get("kiln")]
    temps = [p for p in pts if p.kind == "temperature" and p.curve_role] or [
        p for p in pts if p.kind == "temperature"
    ]
    modes = {run.data_mode}
    lines, summaries = [], []
    for p in temps:
        rows = await samples(session, tenant_id, p.code, start, end)
        modes |= {m for _, m in rows}
        ss = [s for s, _ in rows]
        gap = timedelta(seconds=max(p.interval_s * cfg.alarms.stale_factor, 60))
        sm = cv.summarize(curve, start, ss, min_minutes=cfg.alarms.deviation_minutes, max_gap=gap)
        summaries.append(sm)
        lines.append(
            {
                "point": p.code,
                "name": p.name,
                "unit": p.unit,
                "source": p.source,
                "readings": [[s.ts.isoformat(), s.value, s.quality] for s in ss],
                "max_temp": sm.max_temp,
                "deviation_minutes": sm.deviation_minutes,
                "spans": [
                    {
                        "start": x.start.isoformat(),
                        "end": x.end.isoformat(),
                        "minutes": round(x.minutes, 1),
                        "worst": round(x.worst, 1),
                    }
                    for x in sm.spans
                ],
            }
        )
    energy: dict[str, float] = {}
    for p in pts:
        if p.kind in METER_KINDS:
            rows = await samples(session, tenant_id, p.code, start, end)
            modes |= {m for _, m in rows}
            key = METER_KINDS[p.kind]
            energy[key] = round(energy.get(key, 0.0) + cv.meter_usage([s for s, _ in rows]), 2)
    tops = [s for s in summaries if s.max_temp is not None]
    return {
        "run": run.object_id,
        "run_no": run.state.get("run_no"),
        "kiln": run.state.get("kiln"),
        "status": run.state.get("status"),
        "started_at": start.isoformat(),
        "ended_at": run_end(run).isoformat() if run_end(run) else None,
        "curve": {
            "code": code,
            "name": curve.name if curve else None,
            "tolerance": curve.tolerance if curve else None,
            "from": curve_from,
            "missing": bool(code and curve is None),  # 写了制度编号但配置里没有
            "series": [[t.isoformat(), v] for t, v in cv.series(curve, start)] if curve else [],
        },
        "lines": lines,
        "max_temp": max((s.max_temp for s in tops), default=None),  # type: ignore[type-var]
        "deviation": any(s.deviation for s in summaries),
        "deviation_minutes": max((s.deviation_minutes for s in summaries), default=0.0),
        "energy": energy,
        "samples": sum(s.samples for s in summaries),
        "data_mode": "mock" if "mock" in modes else "real",
    }


async def finish_run(
    session: AsyncSession,
    ledger: Ledger,
    principal: Principal,
    cfg: MonitorConfig,
    run_id: str,
    *,
    ended_at: datetime | None = None,
    status: Literal["finished", "aborted"] = "finished",
    note: str | None = None,
) -> dict[str, Any]:
    """结束窑次：按读数算最高温度、是否超差、能耗，和窑次结束一起入账（以操作人身份）。"""
    run = await session.get(ObjectState, (principal.tenant_id, "kiln_run", run_id))
    if run is None:
        raise MonitorError(f"窑次 {run_id} 不存在", 404)
    if run.state.get("ended_at") or run.state.get("status") in ("finished", "aborted"):
        raise MonitorError(f"窑次 {run.state.get('run_no')} 已经结束", 409)
    end = aware(ended_at) if ended_at else utcnow()
    if end <= run_start(run):
        raise MonitorError("结束时间必须晚于窑次开始时间")
    if end > utcnow() + timedelta(minutes=5):
        raise MonitorError("结束时间不能在未来")
    c = await run_curve(session, principal.tenant_id, cfg, run_id, until=end)
    if not c["samples"]:
        raise MonitorError("这个窑次没有温度读数，不能自动汇总；请先补抄表，或在账本里手工记录窑次结束")
    rng = f"{c['started_at']}/{end.isoformat()}"
    ev = [
        Evidence(kind="sensor", ref=f"{ln['point']}@{rng}"[:512], note=f"{len(ln['readings'])} 个读数")
        for ln in c["lines"]
    ]
    if c["curve"]["code"]:
        ev.append(
            Evidence(
                kind="calculation",
                ref=f"curve:{c['curve']['code']}",
                note=(f"允许偏差 ±{c['curve']['tolerance']:g}℃，" if c["curve"]["tolerance"] else "")
                + f"超差 {c['deviation_minutes']:g} 分钟",
            )
        )
    if note:
        ev.append(Evidence(kind="manual", ref=principal.subject_id, note=note[:500]))
    mode = c["data_mode"]
    finish = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=FINISH_EVENT,
            object_id=run_id,
            payload={
                "ended_at": end.isoformat(),
                "max_temp": round(c["max_temp"], 1) if c["max_temp"] is not None else None,
                "deviation": c["deviation"],
                "status": status,
            },
            evidence=ev,
            data_mode=mode,
            expected_version=run.version,
            idempotency_key=f"kiln_finish:{run_id}",
        ),
    )
    payload: dict[str, Any] = {"deviation_minutes": c["deviation_minutes"], **c["energy"]}
    if c["curve"]["code"] and not run.state.get("firing_curve"):
        payload["firing_curve"] = c["curve"]["code"]
    summary = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=SUMMARY_EVENT,
            object_id=run_id,
            payload={k: v for k, v in payload.items() if v is not None},
            evidence=ev,
            data_mode=mode,
            causation_id=finish.event_id,
            idempotency_key=f"kiln_summary:{run_id}",
        ),
    )
    return {"events": [finish.event_id, summary.event_id], "summary": c}


# ---------------- 窑况 ----------------


async def open_alarms(session: AsyncSession, tenant_id: str) -> list[KilnAlarm]:
    q = (
        select(KilnAlarm)
        .where(KilnAlarm.tenant_id == tenant_id, KilnAlarm.status == "open")
        .order_by(KilnAlarm.started_at)
    )
    return list(await session.scalars(q))


def alarm_out(a: KilnAlarm) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "point": a.point,
        "kiln": a.kiln,
        "kiln_run": a.kiln_run,
        "edge_id": a.edge_id,
        "status": a.status,
        "message": a.message,
        "detail": a.detail,
        "data_mode": a.data_mode,
        "started_at": aware(a.started_at).isoformat(),
        "last_seen_at": aware(a.last_seen_at).isoformat(),
        "closed_at": aware(a.closed_at).isoformat() if a.closed_at else None,
        "acked_by_name": a.acked_by_name,
        "acked_at": aware(a.acked_at).isoformat() if a.acked_at else None,
    }


async def board(
    session: AsyncSession, tenant_id: str, cfg: MonitorConfig, now: datetime | None = None
) -> dict[str, Any]:
    """所有窑炉的当前状态：在烧窑次、各测点最新读数（含目标温度与偏差）、未关闭告警。"""
    now = now or utcnow()
    kilns = sorted(await objects(session, tenant_id, "kiln"), key=lambda o: o.state.get("name", ""))
    runs = await firing_runs(session, tenant_id)
    pts = await active_points(session, tenant_id)
    alarms = await open_alarms(session, tenant_id)
    edges = await edge_last_seen(session, tenant_id)
    modes: set[str] = set()
    out = []
    for k in kilns:
        run = runs.get(k.object_id)
        code, curve, _ = await curve_for_run(session, tenant_id, cfg, run) if run else (None, None, "")
        hours = (now - run_start(run)).total_seconds() / 3600 if run else None
        rows = []
        for p in [p for p in pts if p.kiln == k.object_id]:
            r = await latest(session, tenant_id, p.code)
            exp = None
            if r is not None and run and curve and p.kind == "temperature":
                exp = cv.expected(curve, (aware(r.ts) - run_start(run)).total_seconds() / 3600)
            if r is not None:
                modes.add(r.data_mode)
            rows.append(
                {
                    "point": p.code,
                    "name": p.name,
                    "kind": p.kind,
                    "kind_name": KIND_NAME.get(p.kind, p.kind),
                    "source": p.source,
                    "source_name": SOURCE_NAME.get(p.source, p.source),
                    "unit": p.unit,
                    "low_limit": p.low,
                    "high_limit": p.high,
                    "latest": reading_out(r) if r else None,
                    "age_minutes": round((now - aware(r.ts)).total_seconds() / 60, 1) if r else None,
                    "expected": round(exp, 1) if exp is not None else None,
                    "deviation": round(r.value - exp, 1) if r is not None and exp is not None else None,
                }
            )
        out.append(
            {
                "kiln": k.object_id,
                "name": k.state.get("name", k.object_id),
                "kiln_type": k.state.get("kiln_type"),
                "access_path": k.state.get("access_path"),
                "max_temp": k.state.get("max_temp"),
                "run": {
                    "id": run.object_id,
                    "run_no": run.state.get("run_no"),
                    "started_at": run_start(run).isoformat(),
                    "hours": round(hours, 1) if hours is not None else None,
                    "curve": code,
                    "curve_name": curve.name if curve else None,
                    "curve_hours": curve.hours if curve else None,
                    "tolerance": curve.tolerance if curve else None,
                }
                if run
                else None,
                "points": rows,
                "alarms": [alarm_out(a) for a in alarms if a.kiln == k.object_id],
            }
        )
    return {
        "kilns": out,
        "alarms": [alarm_out(a) for a in alarms if a.kiln is None],  # 网关断网等不属于某个窑的
        "edges": {e: t.isoformat() for e, t in edges.items()},
        "data_mode": "mock" if "mock" in modes else "real",
        "at": now.isoformat(),
    }


async def energy(session: AsyncSession, tenant_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """区间内各窑炉（和不属于窑的总表）的用电、用气。"""
    kilns = {o.object_id: o.state.get("name", o.object_id) for o in await objects(session, tenant_id, "kiln")}
    rows: dict[str, dict[str, Any]] = {}
    modes: set[str] = set()
    for p in await active_points(session, tenant_id):
        if p.kind not in METER_KINDS:
            continue
        data = await samples(session, tenant_id, p.code, start, end)
        modes |= {m for _, m in data}
        key = p.kiln or ""
        r = rows.setdefault(
            key,
            {
                "kiln": p.kiln,
                "name": kilns.get(p.kiln or "", "全厂 / 公用"),
                "electricity_kwh": 0.0,
                "gas_m3": 0.0,
            },
        )
        r[METER_KINDS[p.kind]] = round(r[METER_KINDS[p.kind]] + cv.meter_usage([s for s, _ in data]), 2)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": sorted(rows.values(), key=lambda r: (r["kiln"] is None, r["name"])),
        "data_mode": "mock" if "mock" in modes else "real",
    }


async def ack_alarm(session: AsyncSession, principal: Principal, alarm_id: str) -> KilnAlarm:
    a = await session.get(KilnAlarm, alarm_id)
    if a is None or a.tenant_id != principal.tenant_id:
        raise MonitorError("告警不存在", 404)
    if a.acked_at is None:
        a.acked_by, a.acked_by_name, a.acked_at = principal.subject_id, principal.display_name, utcnow()
    return a
