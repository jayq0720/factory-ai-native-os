"""生产日报（方案 3.3 P0「工厂问答与日报」，W13 试点上线）。

一天发生了什么，全部从事件账本确定性统计（规则 3：数字不让模型算）：
- 统计项写在 packs/<pack>/reports.yaml（租户可在 tenants/<id>/reports.yaml 替换），不写 Python；
- 告警沿用 trace.yaml 的 alerts，作用在当天有事件的对象上；
- 另附治理待办：待审批、未确认的录单卡片、未完成的待办，提醒管理者“还有什么卡着”；
- 收报人只看到自己有读权限的统计项和告警；
- 模拟与真实不混算：有真实事件就只统计真实数据，只有模拟事件时整份日报标“模拟数据”。

IM 里发「日报」「昨天日报」「日报 11-06」走 Fast Path（零模型）；每天定时推送见 fos/reports/schedule.py。
"""

from __future__ import annotations

import re
import time as _time
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.cards import Card, CardField
from fos.db.models import Approval, CardRecord, Event, ObjectState, Todo, utcnow
from fos.governance.principal import Principal
from fos.ledger.report import TraceConfig
from fos.ontology import Ontology
from fos.tenancy.config import TenantConfig

MAX_GROUPS = 4  # 卡片上每项最多列几组，其余合并为“等”
MAX_ALERTS = 8


class ReportItem(BaseModel):
    title: str = Field(min_length=1, max_length=20)
    events: list[str] = Field(min_length=1)
    measure: str = "count"  # count 或数值字段
    unit: str | None = None  # 单位字段
    unit_text: str | None = None  # 固定单位（如 元）
    noun: str = "条"
    group_by: str | None = None

    @model_validator(mode="after")
    def _unit(self) -> ReportItem:
        if self.unit and self.unit_text:
            raise ValueError("unit 与 unit_text 只能给一个")
        return self


class DailyConfig(BaseModel):
    items: list[ReportItem] = Field(default_factory=list)

    def check(self, onto: Ontology) -> list[str]:
        errs: list[str] = []
        for it in self.items:
            missing = [e for e in it.events if e not in onto.events]
            if missing:
                errs.append(f"reports.daily.{it.title}: 没有事件 {missing}")
                continue
            objs = {onto.events[e].object for e in it.events}
            if len(objs) != 1:
                errs.append(f"reports.daily.{it.title}: 事件必须作用于同一类对象")
                continue
            props = onto.objects[objs.pop()].properties
            for f in (it.measure if it.measure != "count" else None, it.unit, it.group_by):
                if f and f not in props:
                    errs.append(f"reports.daily.{it.title}: 对象没有属性 {f}")
            if (
                it.measure != "count"
                and it.measure in props
                and props[it.measure].type
                not in (
                    "number",
                    "integer",
                )
            ):
                errs.append(f"reports.daily.{it.title}: {it.measure} 不是数值")
        return errs


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_daily_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> DailyConfig:
    pack = _read(packs_dir / tenant.industry_pack / "reports.yaml").get("daily") or {}
    own = _read(tenants_dir / tenant.id / "reports.yaml").get("daily") or {}
    return DailyConfig.model_validate({"items": own.get("items", pack.get("items", []))})


# ---------------- 日期 ----------------

DAILY = re.compile(r"^(?:@\S+\s+)*(今天|今日|昨天|昨日|前天)?\s*的?\s*(?:生产)?日报\s*[:：]?\s*(\S*)$")
_MD = re.compile(r"^(?:(\d{4})\s*[-/.年]\s*)?(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$")
_REL = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2}


def parse_day(word: str | None, arg: str, today: date) -> date | None:
    """「日报」=今天，「昨天日报」，「日报 11-06」「日报 2026-11-06」「日报 11月6日」。认不出返回 None。"""
    arg = arg.strip()
    if arg in _REL:
        return today - timedelta(days=_REL[arg])
    if not arg:
        return today - timedelta(days=_REL.get(word or "今天", 0))
    m = _MD.match(arg)
    if not m:
        return None
    try:
        d = date(int(m[1]) if m[1] else today.year, int(m[2]), int(m[3]))
    except ValueError:
        return None
    if not m[1] and d > today:  # 「日报 12-31」在 1 月初说的是去年
        d = d.replace(year=d.year - 1)
    return d


def day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """工厂时区的一天，换成 UTC（账本里的时间统一存 UTC）。"""
    start = datetime.combine(day, time(), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


# ---------------- 统计 ----------------


def _fmt(n: float) -> str:
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _display(onto: Ontology, names: dict[str, str], obj_type: str, field: str, val: Any, refs) -> str:
    p = onto.objects[obj_type].properties[field]
    if val is None or val == "":
        return "未填"
    if p.type == "ref":
        o = refs.get((p.target, val))
        if o is None:
            return str(val)
        s = o.state
        return str(s.get("name") or s.get("code") or s.get("run_no") or s.get("supplier_lot_no") or val)
    if p.type == "bool":
        return "是" if val else "否"
    return names.get(str(val), str(val))


async def daily_report(
    session: AsyncSession,
    *,
    onto: Ontology,
    cfg: DailyConfig,
    trace: TraceConfig | None,
    principal: Principal,
    tz: ZoneInfo,
    day: date,
) -> dict[str, Any]:
    t0 = _time.perf_counter()
    tenant = principal.tenant_id
    start, end = day_window(day, tz)
    rows = (
        await session.scalars(
            select(Event)
            .where(
                Event.tenant_id == tenant,
                Event.occurred_at >= start,
                Event.occurred_at < end,
            )
            .order_by(Event.seq)
        )
    ).all()
    # 模拟与真实不混算
    modes = {e.data_mode for e in rows}
    mode: Literal["real", "mock"] = "real" if "real" in modes or not modes else "mock"
    rows = [e for e in rows if e.data_mode == mode]

    # 当天涉及的对象（当前状态）+ 它们引用的对象（显示名称用）
    keys = {(e.object_type, e.object_id) for e in rows}
    states = await _load(session, tenant, keys)
    ref_keys: set[tuple[str, str]] = set()
    for (t, _), o in states.items():
        for rel, p in onto.ref_props(t).items():
            v = o.state.get(rel)
            for x in (v if isinstance(v, list) else [v]) if v else []:
                ref_keys.add((p.target or "", x))
    refs = await _load(session, tenant, ref_keys)
    names = trace.value_names if trace else {}

    def can_read(obj_type: str) -> bool:
        return principal.can(f"{onto.objects[obj_type].domain}.{obj_type}:read")

    items: list[dict[str, Any]] = []
    for it in cfg.items:
        obj_type = onto.events[it.events[0]].object
        if not can_read(obj_type):
            continue
        evs = [e for e in rows if e.type in it.events]
        if not evs:
            items.append({"title": it.title, "count": 0, "totals": [], "groups": []})
            continue
        totals: dict[str, float] = defaultdict(float)
        groups: dict[tuple[str, str], float] = defaultdict(float)
        for e in evs:
            st = states.get((e.object_type, e.object_id))
            cur = st.state if st else {}

            def get(f: str, cur: dict = cur, e: Event = e) -> Any:
                return e.payload.get(f, cur.get(f))

            if it.measure == "count":
                val, unit = 1.0, it.noun
            else:
                raw = get(it.measure)
                if not isinstance(raw, int | float) or isinstance(raw, bool):
                    continue
                val = float(raw)
                unit = it.unit_text or (str(get(it.unit) or "") if it.unit else "")
            totals[unit] += val
            g = _display(onto, names, obj_type, it.group_by, get(it.group_by), refs) if it.group_by else ""
            groups[(g, unit)] += val
        items.append(
            {
                "title": it.title,
                "count": len(evs),
                "totals": [{"value": v, "unit": u} for u, v in totals.items()],
                "groups": [
                    {"label": g, "value": v, "unit": u}
                    for (g, u), v in sorted(groups.items(), key=lambda kv: -kv[1])
                    if g
                ],
            }
        )

    alerts: list[dict[str, Any]] = []
    for (t, oid), o in states.items():
        if trace is None or not can_read(t):
            continue
        for a in trace.alerts:
            if a.object == t and o.state.get(a.field) in a.values:
                alerts.append({"text": a.text, "object_type": t, "object_id": oid, "label": _label(o)})
    alerts = alerts[:MAX_ALERTS]

    pending = await _pending(session, tenant, mode)
    return {
        "date": day.isoformat(),
        "timezone": str(tz),
        "data_mode": mode,
        "events": len(rows),
        "items": items,
        "alerts": alerts,
        "pending": pending,
        "generated_at": utcnow().isoformat(),
        "elapsed_ms": round((_time.perf_counter() - t0) * 1000, 1),
    }


def _label(o: ObjectState) -> str:
    s = o.state
    name = s.get("name") or s.get("code") or s.get("run_no") or s.get("report_no") or s.get("supplier_lot_no")
    return f"{name}（{o.object_id}）" if name and name != o.object_id else o.object_id


async def _load(session: AsyncSession, tenant: str, keys: set[tuple[str, str]]):
    out: dict[tuple[str, str], ObjectState] = {}
    by_type: dict[str, list[str]] = defaultdict(list)
    for t, i in keys:
        by_type[t].append(i)
    for t, ids in by_type.items():
        for k in range(0, len(ids), 500):
            q = select(ObjectState).where(
                ObjectState.tenant_id == tenant,
                ObjectState.object_type == t,
                ObjectState.object_id.in_(ids[k : k + 500]),
            )
            for o in (await session.scalars(q)).all():
                out[(t, o.object_id)] = o
    return out


async def _pending(session: AsyncSession, tenant: str, mode: str) -> dict[str, int]:
    now = utcnow()

    async def n(q) -> int:
        return int((await session.execute(q)).scalar_one())

    return {
        "approvals": await n(
            select(func.count())
            .select_from(Approval)
            .where(Approval.tenant_id == tenant, Approval.status == "pending", Approval.data_mode == mode)
        ),
        "cards": await n(
            select(func.count())
            .select_from(CardRecord)
            .where(
                CardRecord.tenant_id == tenant,
                CardRecord.status == "open",
                CardRecord.action.is_not(None),
                CardRecord.expires_at > now,
            )
        ),
        "todos": await n(
            select(func.count()).select_from(Todo).where(Todo.tenant_id == tenant, Todo.status == "open")
        ),
    }


# ---------------- 展示 ----------------


def item_text(it: dict[str, Any]) -> str:
    if not it["count"]:
        return "无"
    total = "，".join(f"{_fmt(t['value'])} {t['unit']}".strip() for t in it["totals"]) or f"{it['count']} 条"
    gs = it["groups"]
    if not gs or (len(gs) == 1 and len(it["totals"]) == 1):
        detail = f"（{gs[0]['label']}）" if gs else ""
        return total + detail
    shown = "、".join(f"{g['label']} {_fmt(g['value'])}" for g in gs[:MAX_GROUPS])
    more = f" 等 {len(gs)} 项" if len(gs) > MAX_GROUPS else ""
    return f"{total}（{shown}{more}）"


def report_lines(rep: dict[str, Any]) -> list[str]:
    lines = [f"{it['title']}：{item_text(it)}" for it in rep["items"] if it["count"]]
    if not lines:
        lines = ["没有记录到生产、仓库或质检事件"]
    lines += [f"⚠ {a['text']}：{a['label']}" for a in rep["alerts"]]
    p = rep["pending"]
    lines.append(f"待审批 {p['approvals']} · 待确认录单卡片 {p['cards']} · 未完成待办 {p['todos']}")
    return lines


def report_card(rep: dict[str, Any]) -> Card:
    d = date.fromisoformat(rep["date"])
    busy = [it for it in rep["items"] if it["count"]]
    fields = [CardField(label=it["title"][:20], value=item_text(it)[:200]) for it in busy][:11]
    p = rep["pending"]
    if p["approvals"] or p["cards"] or p["todos"]:
        fields.append(
            CardField(
                label="待处理",
                value=f"审批 {p['approvals']} · 录单卡片 {p['cards']} · 待办 {p['todos']}",
            )
        )
    if rep["alerts"]:
        text = "**告警**\n" + "\n".join(f"- {a['text']}：{a['label']}" for a in rep["alerts"])
    elif not busy:
        text = "这一天没有记录到生产、仓库或质检事件。"
    else:
        text = "没有告警。"
    if len(text) > 500:
        text = text[:497] + "…"
    return Card(
        title=f"生产日报 {d.month}月{d.day}日",
        text=text,
        fields=fields,
        data_mode=rep["data_mode"],
        footer=f"共 {rep['events']} 条记录，由账本统计（不经模型），只含你有权查看的内容",
    )
