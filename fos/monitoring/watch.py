"""监控判定与推送（方案 2.4.6 Watchdog、3.2、10.6）：`fos channels run` 进程里每 poll_seconds 判定一轮。

判定（确定性代码，零模型成本）：
- 超上 / 下限持续 limit_minutes → high / low；
- 在烧窑次的温度点偏离烧成制度超过允许偏差、持续 deviation_minutes → deviation；
- 网关 edge_offline_minutes 没有上行 → edge_offline（一台网关一条，不再逐个点位报“故障”）；
- 网关在线、点位超过 stale_factor 个采样间隔没有读数，或最新读数质量为 bad → point_fault；
- 人工抄表点位到点 manual_grace_minutes 后还没报数 → 抄表提醒
  （不是告警，Trigger 唯一约束保证每次只提醒一回）。

告警表里同一 key 同时只有一条未关闭的；条件消失自动关闭。新告警立即推送，持续未确认的每 repeat_minutes 再推。
先提交再发送（规则 6）；推送只读，不写账本。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.db.models import KilnAlarm, Trigger, utcnow
from fos.monitoring import curve as cv
from fos.monitoring import service as ms
from fos.monitoring.config import MonitorConfig
from fos.planning.notify import _thread, dm, role_identities

if TYPE_CHECKING:
    from fos.services import Services

log = logging.getLogger(__name__)
KIND_TEXT = {
    "high": "超上限",
    "low": "低于下限",
    "deviation": "偏离烧成曲线",
    "edge_offline": "采集网关断网",
    "point_fault": "测点无数据 / 设备故障",
}


@dataclass
class Condition:
    key: str
    kind: str
    message: str
    point: str | None = None
    kiln: str | None = None
    kiln_run: str | None = None
    edge_id: str | None = None
    detail: dict = field(default_factory=dict)
    data_mode: str = "real"


@dataclass
class Reminder:
    point: ms.Point
    due: datetime
    kiln_name: str


@dataclass
class Round:
    opened: list[KilnAlarm] = field(default_factory=list)
    closed: list[KilnAlarm] = field(default_factory=list)
    push: list[KilnAlarm] = field(default_factory=list)  # 新开的 + 到了重复提醒时间的
    reminders: list[Reminder] = field(default_factory=list)


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _hm(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%H:%M") if tz else dt.strftime("%H:%M")


async def conditions(
    session: AsyncSession, tenant_id: str, cfg: MonitorConfig, now: datetime, tz=None
) -> tuple[list[Condition], list[Reminder]]:
    a = cfg.alarms
    pts = await ms.active_points(session, tenant_id)
    kilns = {o.object_id: o for o in await ms.objects(session, tenant_id, "kiln")}
    runs = await ms.firing_runs(session, tenant_id)
    edges = await ms.edge_last_seen(session, tenant_id)
    out: list[Condition] = []
    reminders: list[Reminder] = []
    offline: dict[str, list[ms.Point]] = {}
    for p in pts:
        kname = kilns[p.kiln].state.get("name", p.kiln) if p.kiln in kilns else ""
        label = f"{kname} · {p.name}" if kname else p.name
        run = runs.get(p.kiln or "")
        last = await ms.latest(session, tenant_id, p.code)
        last_ts = ms.aware(last.ts) if last else None
        interval = timedelta(seconds=p.interval_s)
        # —— 有没有数据 ——
        if p.source == "manual":
            needs = p.kind in ms.METER_KINDS or p.kiln is None or run is not None
            since = last_ts if last_ts and (run is None or last_ts >= ms.run_start(run)) else None
            due = (since + interval) if since else (ms.run_start(run) if run else None)
            if needs and due is not None and now >= due + timedelta(minutes=a.manual_grace_minutes):
                reminders.append(Reminder(p, due, kname))
            elif needs and due is None and last_ts is None:
                reminders.append(Reminder(p, now.replace(minute=0, second=0, microsecond=0), kname))
        else:
            seen = edges.get(p.edge_id) if p.edge_id else None
            if p.edge_id and (seen is None or now - seen > timedelta(minutes=a.edge_offline_minutes)):
                offline.setdefault(p.edge_id, []).append(p)
                continue  # 断网时不再判点位，避免一台网关断网报一串故障
            stale = last_ts is None or now - last_ts > interval * a.stale_factor
            if stale or (last is not None and last.quality == "bad"):
                why = (
                    "最新读数质量差（通讯失败 / 超量程）"
                    if not stale
                    else (
                        f"{_fmt((now - last_ts).total_seconds() / 60)} 分钟没有读数"
                        if last_ts
                        else "从未收到读数"
                    )
                )
                out.append(
                    Condition(
                        key=f"point_fault:{p.code}",
                        kind="point_fault",
                        message=f"{label}：{why}，网关在线，请检查仪表 / 传感器 / 接线",
                        point=p.code,
                        kiln=p.kiln,
                        edge_id=p.edge_id,
                        detail={"last_ts": last_ts.isoformat() if last_ts else None},
                        data_mode=p.data_mode,
                    )
                )
                continue
        if last is None:
            continue
        # —— 上下限 / 曲线：取最近一段读数，看“现在”是否处在一段持续超出里 ——
        window = timedelta(minutes=max(a.limit_minutes, a.deviation_minutes) + 1) + interval * a.stale_factor
        rows = await ms.samples(session, tenant_id, p.code, now - window, now)
        ss = [s for s, _ in rows]
        mode = "mock" if p.data_mode == "mock" or any(m == "mock" for _, m in rows) else "real"
        gap = interval * a.stale_factor
        if p.source == "manual":
            ss = ss[-1:]  # 抄表点位稀疏：只看最近一次报数
        if not ss:
            continue
        lim = cv.spans(ss, cv.limit_excess(p.low, p.high, ss), gap)
        cur = lim[-1] if lim and lim[-1].end == ss[-1].ts else None
        if cur and (p.source == "manual" or cur.minutes >= a.limit_minutes):
            kind = "high" if cur.worst > 0 else "low"
            bound = p.high if kind == "high" else p.low
            out.append(
                Condition(
                    key=f"{kind}:{p.code}",
                    kind=kind,
                    message=(
                        f"{label} {_fmt(ss[-1].value)}{p.unit}，{KIND_TEXT[kind]} {_fmt(bound or 0)}{p.unit}"
                        f"（{_hm(cur.start, tz)} 起，{_fmt(cur.minutes)} 分钟）"
                    ),
                    point=p.code,
                    kiln=p.kiln,
                    kiln_run=run.object_id if run else None,
                    detail={"value": ss[-1].value, "limit": bound, "since": cur.start.isoformat()},
                    data_mode=mode,
                )
            )
        if run is None or p.kind != "temperature":
            continue
        code, curve, _ = await ms.curve_for_run(session, tenant_id, cfg, run)
        if curve is None:
            continue
        start = ms.run_start(run)
        ss = [s for s in ss if s.ts >= start]
        if not ss:
            continue
        dev = cv.spans(ss, cv.curve_excess(curve, start, ss), gap)
        cur = dev[-1] if dev and dev[-1].end == ss[-1].ts else None
        if cur and (p.source == "manual" or cur.minutes >= a.deviation_minutes):
            exp = cv.expected(curve, (ss[-1].ts - start).total_seconds() / 3600) or 0
            d = ss[-1].value - exp
            out.append(
                Condition(
                    key=f"deviation:{p.code}",
                    kind="deviation",
                    message=(
                        f"{label} {_fmt(ss[-1].value)}{p.unit}，"
                        f"烧成制度 {code} 此时应为 {_fmt(exp)}{p.unit}，"
                        f"{'高' if d > 0 else '低'} {_fmt(abs(d))}{p.unit}（允许 ±{_fmt(curve.tolerance)}，"
                        f"{_hm(cur.start, tz)} 起 {_fmt(cur.minutes)} 分钟）"
                    ),
                    point=p.code,
                    kiln=p.kiln,
                    kiln_run=run.object_id,
                    detail={"value": ss[-1].value, "expected": round(exp, 1), "curve": code},
                    data_mode=mode,
                )
            )
    for edge, plist in offline.items():
        seen = edges.get(edge)
        names = "、".join(p.name for p in plist[:5]) + (f" 等 {len(plist)} 个点位" if len(plist) > 5 else "")
        when = f"最后上行 {_hm(seen, tz)}" if seen else "从未上行"
        out.append(
            Condition(
                key=f"edge_offline:{edge}",
                kind="edge_offline",
                message=f"采集网关 {edge} 断网（{when}）：{names} 暂无数据；网关有本地缓存，恢复后自动补传",
                edge_id=edge,
                detail={"points": [p.code for p in plist], "last_seen": seen.isoformat() if seen else None},
                data_mode="mock" if all(p.data_mode == "mock" for p in plist) else "real",
            )
        )
    return out, reminders


async def evaluate(
    session: AsyncSession, tenant_id: str, cfg: MonitorConfig, now: datetime | None = None, tz=None
) -> Round:
    """判定一轮并更新告警表（在调用方的事务里）。"""
    now = now or utcnow()
    conds, reminders = await conditions(session, tenant_id, cfg, now, tz)
    current = {c.key: c for c in conds}
    res = Round(reminders=reminders)
    opened = {a.key: a for a in await ms.open_alarms(session, tenant_id)}
    for key, a in opened.items():
        if key not in current:
            a.status, a.closed_at = "closed", now
            res.closed.append(a)
    for key, c in current.items():
        a = opened.get(key)
        if a is None:
            a = KilnAlarm(
                tenant_id=tenant_id,
                key=key,
                kind=c.kind,
                point=c.point,
                kiln=c.kiln,
                kiln_run=c.kiln_run,
                edge_id=c.edge_id,
                status="open",
                message=c.message[:500],
                detail=c.detail,
                data_mode=c.data_mode,
                started_at=now,
                last_seen_at=now,
            )
            session.add(a)
            res.opened.append(a)
            res.push.append(a)
            continue
        a.message, a.detail, a.last_seen_at = c.message[:500], c.detail, now
        repeat = timedelta(minutes=cfg.notify.repeat_minutes)
        if a.acked_at is None and (a.notified_at is None or now - ms.aware(a.notified_at) >= repeat):
            res.push.append(a)
    await session.flush()
    return res


def alarm_text(a: KilnAlarm) -> str:
    head = "【模拟数据】" if a.data_mode == "mock" else ""
    tail = "\n发“窑况”查看全部窑炉；处理后在网页上点“已知晓”停止重复提醒。"
    return f"{head}【窑炉告警 · {KIND_TEXT.get(a.kind, a.kind)}】{a.message}{tail}"


def reminder_text(r: Reminder, tz=None) -> str:
    p = r.point
    head = "【模拟数据】" if p.data_mode == "mock" else ""
    where = f"{r.kiln_name} · {p.name}" if r.kiln_name else p.name
    return (
        f"{head}【抄表提醒】{where}（{p.code}）该抄表了（应在 {_hm(r.due, tz)}）。\n"
        f"看仪表后回复：抄表 {p.code} 数值（单位 {p.unit}）"
    )


async def watch_once(
    svc: Services, tenant_id: str, adapter: ChannelAdapter | None, now: datetime | None = None
) -> Round:
    from zoneinfo import ZoneInfo

    cfg: MonitorConfig | None = svc.monitoring.get(tenant_id)
    tenant = svc.registry.get(tenant_id)
    if cfg is None or tenant is None:
        return Round()
    tz = ZoneInfo(tenant.timezone)
    now = now or utcnow()
    outbox: list[tuple] = []
    async with svc.sessionmaker() as session, session.begin():
        res = await evaluate(session, tenant_id, cfg, now, tz)
        if adapter is None:
            return res
        thread_id = await _thread(session, tenant_id)
        if res.push:
            roles = await role_identities(session, tenant_id, cfg.notify.alarm_roles, adapter.name)
            for a in res.push:
                a.notified_at = now
                outbox += [(dm(i), alarm_text(a)) for _, i in roles]
        due = []
        for r in res.reminders:
            trig = Trigger(
                tenant_id=tenant_id,
                source="schedule",
                external_id=f"meter_remind:{r.point.code}:{r.due.isoformat()}"[:160],
                thread_id=thread_id,
                sender_ref="system",
                kind="schedule",
                route="meter_remind",
                status="done",
                payload={"point": r.point.code, "due": r.due.isoformat()},
                created_at=utcnow(),
            )
            try:
                async with session.begin_nested():
                    session.add(trig)
                due.append(r)
            except IntegrityError:
                continue
        if due:
            roles = await role_identities(session, tenant_id, cfg.notify.manual_roles, adapter.name)
            outbox += [(dm(i), reminder_text(r, tz)) for r in due for _, i in roles]
        res.reminders = due
    for ref, text in outbox:  # 提交之后再发
        try:
            await adapter.send_text(ref, text)
        except ChannelError as err:
            log.warning("窑炉告警 / 抄表提醒推送失败：%s", err)
    return res


async def monitor_loop(svc: Services, tenant_id: str, adapter: ChannelAdapter) -> None:
    cfg = svc.monitoring.get(tenant_id)
    if cfg is None:
        return
    while True:
        try:
            r = await watch_once(svc, tenant_id, adapter)
            if r.opened or r.closed or r.reminders:
                log.info(
                    "%s 窑炉监控：新告警 %d，关闭 %d，抄表提醒 %d",
                    tenant_id,
                    len(r.opened),
                    len(r.closed),
                    len(r.reminders),
                )
        except Exception:  # 一次失败不影响下一轮
            log.exception("%s 窑炉监控判定失败", tenant_id)
        await asyncio.sleep(cfg.alarms.poll_seconds)
