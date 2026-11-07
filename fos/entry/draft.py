"""录单草稿：把一份单据的抽取字段，按 Schema 的 entry 映射变成一个待确认的账本事件。

全部是确定性代码，不调用模型：
- 数值、日期按本体属性类型转换（数字不让模型算，规则 3）；
- 引用（物料、供应商、批次、窑次……）按编号或名称查已有对象，查不到、查到多个都如实标出；
- 每个字段带原文页码和片段，作为事件证据。

草稿有问题（缺必填、查不到引用、值不合法）时不生成事件，提示去控制台补全或重拍。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.cards import Card, CardButton, CardField
from fos.db.models import Document, DocumentField, ObjectState
from fos.entry.spec import EntryValue
from fos.extraction.schema import ExtractionSchema
from fos.ledger.service import AppendRequest, Evidence
from fos.ontology import Ontology, OntologyError
from fos.ontology.schema import PropertyDef

NUM = re.compile(r"-?\d+(?:\.\d+)?")
DATE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")
TIME = re.compile(r"([上下]午)?\s*(\d{1,2})\s*[:：时点]\s*(\d{1,2})?")
SPLIT = re.compile(r"[、,，;；/\s]+")
OBJECT_ID = re.compile(r"^[\w\-.]{1,64}$")
MAX_LOOKUP = 5000
MAX_EVIDENCE = 8
TRUE_WORDS = {"是", "有", "合格", "true", "yes", "y", "1", "√"}
FALSE_WORDS = {"否", "无", "没有", "false", "no", "n", "0", "×"}


def norm(s: str) -> str:
    """查找用：全角转半角、去空白、大写。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s)).upper()


@dataclass
class DraftRow:
    key: str  # 事件字段
    name: str
    value: Any = None  # 写入事件的值
    display: str = ""  # 给人看的值
    source: str | None = None  # 抽取字段 key
    page: int | None = None
    quote: str | None = None
    status: str = "ok"  # ok / default / missing / invalid / not_found / ambiguous
    note: str = ""

    def out(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class EntryDraft:
    document_id: str
    schema_id: str
    schema_name: str
    event: str
    event_name: str
    object_type: str
    op: str
    object_id: str | None
    data_mode: str
    rows: list[DraftRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    request: AppendRequest | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.request is not None

    @property
    def idempotency_key(self) -> str:
        return f"entry:{self.document_id}:{self.schema_id}"

    def out(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "schema_id": self.schema_id,
            "schema_name": self.schema_name,
            "event": self.event,
            "event_name": self.event_name,
            "object_type": self.object_type,
            "op": self.op,
            "object_id": self.object_id,
            "data_mode": self.data_mode,
            "ok": self.ok,
            "problems": self.problems,
            "rows": [r.out() for r in self.rows],
        }


# ---------------- 取值与转换 ----------------


def parse_datetime(raw: str, tz: ZoneInfo, *, date_only: bool = False) -> datetime | date | None:
    m = DATE.search(raw)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if date_only:
        return d
    t = time(0, 0)
    tm = TIME.search(raw[m.end() :])
    if tm:
        h, mi = int(tm.group(2)), int(tm.group(3) or 0)
        if tm.group(1) == "下午" and h < 12:
            h += 12
        if 0 <= h < 24 and 0 <= mi < 60:
            t = time(h, mi)
    return datetime.combine(d, t, tzinfo=tz)


def convert(p: PropertyDef, raw: str, tz: ZoneInfo) -> tuple[Any, str | None]:
    """原文 → 属性值。返回 (值, 错误说明)。"""
    raw = raw.strip()
    match p.type:
        case "number" | "integer":
            m = NUM.search(raw.replace(",", "").replace("，", ""))
            if not m:
                return None, f"“{raw}”不是数字"
            v = float(m.group(0))
            if p.type == "integer":
                if v != int(v):
                    return None, f"“{raw}”应为整数"
                return int(v), None
            return v, None
        case "date":
            d = parse_datetime(raw, tz, date_only=True)
            return (d.isoformat(), None) if d else (None, f"“{raw}”不是日期")
        case "datetime":
            dt = parse_datetime(raw, tz)
            return (dt.isoformat(), None) if dt else (None, f"“{raw}”不是日期时间")
        case "bool":
            w = norm(raw).lower()
            if w in TRUE_WORDS:
                return True, None
            if w in FALSE_WORDS:
                return False, None
            return None, f"“{raw}”不是是/否"
        case "enum":
            if raw in (p.values or []):
                return raw, None
            return None, f"“{raw}”不在可选值 {p.values} 里"
        case _:
            return raw, None


def _now_value(p: PropertyDef, word: str, tz: ZoneInfo) -> Any:
    now = datetime.now(tz)
    if p.type == "date":
        return now.date().isoformat()
    if p.type == "datetime":
        return now.replace(microsecond=0).isoformat()
    return word


def _display(v: Any) -> str:
    if isinstance(v, list):
        return "、".join(map(str, v))
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


async def find_objects(
    session: AsyncSession, tenant: str, object_type: str, text: str, keys: list[str], data_mode: str
) -> list[ObjectState]:
    """按对象编号，再按指定属性（名称、牌号、供方批号……）精确匹配。真实单据不匹配模拟对象。"""
    text = text.strip()
    ok_modes = {"real"} if data_mode == "real" else {"real", "mock"}
    if OBJECT_ID.match(text):
        hit = await session.get(ObjectState, (tenant, object_type, text))
        if hit is not None and hit.data_mode in ok_modes:
            return [hit]
    if not keys:
        return []
    want = norm(text)
    rows = (
        await session.scalars(
            select(ObjectState)
            .where(ObjectState.tenant_id == tenant, ObjectState.object_type == object_type)
            .order_by(ObjectState.updated_seq.desc())
            .limit(MAX_LOOKUP)
        )
    ).all()
    return [
        o
        for o in rows
        if o.data_mode in ok_modes and any(norm(str(o.state.get(k) or "")) == want for k in keys)
    ]


def _label(o: ObjectState) -> str:
    s = o.state
    name = s.get("name") or s.get("code") or s.get("run_no") or s.get("supplier_lot_no")
    return f"{name}（{o.object_id}）" if name and name != o.object_id else o.object_id


# ---------------- 草稿 ----------------


async def build_draft(
    session: AsyncSession,
    onto: Ontology,
    schema: ExtractionSchema,
    doc: Document,
    fields: list[DocumentField],
    *,
    tz: ZoneInfo,
) -> EntryDraft:
    spec = schema.entry
    if spec is None:
        raise ValueError(f"Schema {schema.id} 没有录单映射")
    ev = onto.events[spec.event]
    obj = onto.objects[ev.object]
    by_key = {f.key: f for f in fields}
    draft = EntryDraft(
        document_id=doc.id,
        schema_id=schema.id,
        schema_name=schema.name,
        event=spec.event,
        event_name=ev.name,
        object_type=ev.object,
        op=ev.op,
        object_id=None,
        data_mode=doc.data_mode,
    )

    def source_of(v: EntryValue) -> tuple[str | None, DocumentField | None]:
        if not v.source:
            return None, None
        f = by_key.get(v.source)
        if f is None or not f.value:
            return None, f
        raw = f.unit if v.part == "unit" else f.value
        return (raw.strip() if raw else None), f

    def cite(row: DraftRow, f: DocumentField | None) -> None:
        if f is not None and f.sources:
            row.page, row.quote = f.sources[0].get("page"), (f.sources[0].get("quote") or "")[:200]

    # 对象编号
    if spec.object_id is not None:
        raw, f = source_of(spec.object_id)
        row = DraftRow(key="object_id", name=f"{obj.name}编号", source=spec.object_id.source)
        cite(row, f)
        if ev.op == "update":
            if raw is None:
                row.status, row.note = "missing", "单据上没有找到编号"
            else:
                hits = await find_objects(
                    session, doc.tenant_id, ev.object, raw, spec.object_id.lookup or [], doc.data_mode
                )
                row.status, row.note, row.value = _pick(hits, raw, obj.name)
                if row.value:
                    draft.object_id = row.value.object_id
                    row.display = _label(row.value)
                    row.value = row.value.object_id
        else:
            oid = raw.replace(" ", "") if raw else None
            if oid is None and spec.object_id.auto:
                stamp = (doc.created_at or datetime.now(UTC)).astimezone(tz).strftime("%y%m%d")
                oid, row.status = f"{spec.object_id.auto}{stamp}-{doc.id[:4].upper()}", "default"
                row.note = "自动编号"
            if oid is None:
                row.status, row.note = "missing", "单据上没有找到编号"
            elif not OBJECT_ID.match(oid):
                row.status, row.note = "invalid", f"编号“{oid}”含有不能用的字符"
            elif await session.get(ObjectState, (doc.tenant_id, ev.object, oid)) is not None:
                row.status, row.note = "invalid", f"{obj.name} {oid} 已经存在（可能已录过）"
            else:
                draft.object_id, row.value = oid, oid
            row.display = oid or ""
        draft.rows.append(row)

    # 事件字段
    payload: dict[str, Any] = {}
    for key, v in spec.fields.items():
        p = obj.properties[key]
        row = DraftRow(key=key, name=p.name, source=v.source)
        raw, f = source_of(v)
        cite(row, f)
        if v.const is not None:
            value: Any = v.const
            row.status, row.note = "default", "固定值"
        elif raw is not None:
            raw = v.map.get(raw, v.map.get(norm(raw), raw))
            if p.type == "ref":
                value = await _resolve_ref(session, onto, doc, p, raw, v.lookup or [], row)
            else:
                value, err = convert(p, raw, tz)
                if err:
                    row.status, row.note = "invalid", err
        elif v.default is not None:
            value = _now_value(p, v.default, tz) if v.default in ("now", "today") else v.default
            row.status = "default"
            row.note = "单据上没有，按录单时间" if v.default in ("now", "today") else "单据上没有，用默认值"
        else:
            value = None
            required = (ev.op == "create" and p.required) or bool(
                v.source and (fd := schema.field(v.source)) and fd.required
            )
            row.status = "missing"
            row.note = "必填，单据上没有找到" if required else "单据上没有，不写入"
            if not required:
                row.status = "ok"
        if value is not None and row.status in ("ok", "default"):
            payload[key] = value
            if not row.display:
                row.display = _display(value)
        draft.rows.append(row)

    for r in draft.rows:
        if r.status in ("missing", "invalid", "not_found", "ambiguous"):
            draft.problems.append(f"{r.name}：{r.note}")

    if not draft.problems:
        try:
            onto.check_payload(spec.event, payload, creating=ev.op == "create")
        except OntologyError as e:
            draft.problems.append(str(e))
    if draft.problems:
        return draft

    occurred = None
    if spec.occurred_at and isinstance(payload.get(spec.occurred_at), str):
        try:
            occurred = datetime.fromisoformat(payload[spec.occurred_at])
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=tz)
        except ValueError:
            occurred = None
    kind = "photo" if doc.format == "image" else "document"
    evidence = [Evidence(kind=kind, ref=f"document:{doc.id}", note=doc.filename[:200])]
    for r in draft.rows:
        if r.page and r.quote and len(evidence) < MAX_EVIDENCE:
            evidence.append(
                Evidence(kind=kind, ref=f"document:{doc.id}", page=r.page, quote=r.quote, note=r.name)
            )
    draft.request = AppendRequest(
        type=spec.event,
        object_id=draft.object_id,
        payload=payload,
        evidence=evidence,
        occurred_at=occurred,
        idempotency_key=draft.idempotency_key,
        data_mode=doc.data_mode,  # type: ignore[arg-type]
    )
    return draft


def _pick(hits: list[ObjectState], raw: str, type_name: str) -> tuple[str, str, Any]:
    if not hits:
        return "not_found", f"没有找到{type_name}“{raw}”，请先登记或核对编号", None
    if len(hits) > 1:
        ids = "、".join(h.object_id for h in hits[:3])
        return "ambiguous", f"“{raw}”对应多个{type_name}（{ids}），请改成编号", None
    return "ok", "", hits[0]


async def _resolve_ref(
    session: AsyncSession,
    onto: Ontology,
    doc: Document,
    p: PropertyDef,
    raw: str,
    keys: list[str],
    row: DraftRow,
) -> Any:
    assert p.target is not None
    type_name = onto.objects[p.target].name
    parts = [x for x in SPLIT.split(raw) if x] if p.many else [raw]
    ids: list[str] = []
    labels: list[str] = []
    for part in parts:
        hits = await find_objects(session, doc.tenant_id, p.target, part, keys, doc.data_mode)
        status, note, hit = _pick(hits, part, type_name)
        if hit is None:
            row.status, row.note = status, note
            return None
        ids.append(hit.object_id)
        labels.append(_label(hit))
    row.display = "、".join(labels)
    return ids if p.many else ids[0]


# ---------------- 卡片 ----------------


def draft_card(draft: EntryDraft, doc: Document) -> Card:
    """录单卡片：每个字段带页码；有问题时不给确认按钮。"""
    rows = [r for r in draft.rows if r.display or r.status not in ("ok",)]
    fields = []
    for r in rows[:12]:
        if r.status in ("ok", "default"):
            v = r.display + (f"（第{r.page}页）" if r.page and doc.page_count > 1 else "")
            if r.status == "default" and r.note:
                v += f"〔{r.note}〕"
        else:
            v = f"⚠ {r.note}"
        fields.append(CardField(label=r.name[:20], value=v[:200]))
    if draft.ok:
        return Card(
            title=f"录单确认：{draft.schema_name}"[:40],
            text=f"识别为「{draft.schema_name}」，确认后记为“{draft.event_name}”。请对照照片核对数字。",
            fields=fields,
            buttons=[
                CardButton(id="confirm", label="确认", style="primary"),
                CardButton(id="cancel", label="取消"),
            ],
            data_mode=draft.data_mode,  # type: ignore[arg-type]
            footer=f"文档 {doc.id[:8]} · 原件已保存",
        )
    problems = "\n".join(f"- {p}" for p in draft.problems[:6])
    return Card(
        title=f"录单待补全：{draft.schema_name}"[:40],
        text=f"还不能入账：\n{problems}\n可以重拍更清楚的照片，或在控制台修改后提交。"[:500],
        fields=fields,
        data_mode=draft.data_mode,  # type: ignore[arg-type]
        footer=f"文档 {doc.id[:8]} · 原件已保存",
    )
