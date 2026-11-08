"""招标拆解的服务层：登记招标项目（Ledger.append）→ 拆解（L1 草稿版本）→ 逐条处理 → 转工程项目 → 提交投标。

路由（fos/api/tender_routes.py）只做参数与权限，业务都在这里，IM 卡片和定时提醒也复用。
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import (
    Approval,
    Document,
    DocumentField,
    DocumentPage,
    EngDesign,
    ObjectState,
    TenderAnalysis,
    Todo,
    User,
    utcnow,
)
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.tender import decompose as dc
from fos.tender.config import Capability, TenderConfig

HUMAN = {"confirmed", "corrected"}
# 登记前必须人工核对的字段：截止时间错了就是丢标
CRITICAL = ["bid_deadline"]
TEXT_MAP = {
    "tender_no": "tender_no",
    "purchaser": "purchaser",
    "agency": "agency",
    "evaluation_method": "evaluation_method",
    "delivery_period": "delivery_period",
    "delivery_place": "delivery_place",
    "qualification": "qualification",
    "payment_terms": "payment_terms",
    "warranty": "warranty",
    "contact": "contact",
    "phone": "phone",
}
MONEY_MAP = {"budget": "budget", "bid_bond": "bid_bond"}
TIME_MAP = {
    "bid_deadline": "bid_deadline",
    "opening_time": "opening_time",
    "clarification_deadline": "clarification_deadline",
}
ITEM_STATUS = {"open", "responded", "accepted", "na"}  # 未处理 / 已响应 / 接受偏离 / 不适用
DEVIATIONS = {"无偏离", "正偏离", "负偏离", "待确认"}
DOC_DISPOSITIONS = {"prepared", "na"}  # 资料：已备齐（线下补办）/ 不适用
# 必须处理的条款级别（普通条款可以不逐条标）
MUST_HANDLE = {"star", "key", "must"}
LEVEL_TEXT = {"star": "★实质性", "key": "▲重要", "must": "必须/不得", "normal": "一般"}
_DATE = re.compile(
    r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?\s*(?:(\d{1,2})\s*[:：时点]\s*(\d{1,2})?)?"
)
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


class TenderError(Exception):
    status = 422


class TenderNotFound(TenderError):
    status = 404


class TenderConflict(TenderError):
    status = 409


# ---------------- 解析字段值 ----------------


def parse_money(value: str | None, unit: str | None = None) -> float | None:
    """“186.5 万元”→ 1865000（元）。"""
    text = f"{value or ''}{unit or ''}"
    m = _NUM.search((value or "").replace(",", ""))
    if not m:
        return None
    v = float(m.group())
    if "亿" in text:
        v *= 1e8
    elif "万" in text:
        v *= 1e4
    return round(v, 2)


def parse_time(value: str | None, tz: ZoneInfo) -> datetime | None:
    """“2026年12月18日 09:30”→ 带时区的时间。没写时刻按 0 点（宁早勿晚）。"""
    m = _DATE.search(value or "")
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), tzinfo=tz)
    except ValueError:
        return None


def new_tender_id() -> str:
    day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%y%m%d")
    return f"TD-{day}-{secrets.token_hex(2).upper()}"


async def _fields(session: AsyncSession, tenant_id: str, doc_ids: list[str]) -> list[DocumentField]:
    if not doc_ids:
        return []
    rows = await session.scalars(
        select(DocumentField).where(
            DocumentField.tenant_id == tenant_id, DocumentField.document_id.in_(doc_ids)
        )
    )
    return list(rows)


def tender_payload(
    doc: Document, fields: list[DocumentField], tz: ZoneInfo
) -> tuple[dict[str, Any], list[Evidence], list[str]]:
    """招标文件字段 → 招标项目属性。返回 (payload, 证据, 还没人工核对的字段名)。"""
    by = {f.key: f for f in fields if f.value}
    known = set(TEXT_MAP) | set(MONEY_MAP) | set(TIME_MAP) | {"project_name"}
    unchecked = [by[k].name for k in CRITICAL if k in by and by[k].status not in HUMAN] + [
        f.name
        for f in fields
        if f.conflict and f.status not in HUMAN and f.key in known - set(CRITICAL) and f.value
    ]
    payload: dict[str, Any] = {}
    evidence: list[Evidence] = []

    def cite(f: DocumentField) -> None:
        src = (f.sources or [{}])[0]
        if src.get("page"):
            evidence.append(
                Evidence(
                    kind="document",
                    ref=f"document:{doc.id}",
                    page=src["page"],
                    quote=(src.get("quote") or "")[:500] or None,
                    note=f.name,
                )
            )
        else:
            evidence.append(
                Evidence(kind="manual", ref=f"document:{doc.id}#{f.key}", note=f"{f.name}（人工填写）")
            )

    name = by.get("project_name")
    payload["name"] = name.value[:200] if name else doc.filename.rsplit(".", 1)[0]
    if name:
        cite(name)
    for k, prop in TEXT_MAP.items():
        if k in by:
            payload[prop] = by[k].value
            cite(by[k])
    for k, prop in MONEY_MAP.items():
        if k in by and (v := parse_money(by[k].value, by[k].unit)) is not None:
            payload[prop] = v
            cite(by[k])
    for k, prop in TIME_MAP.items():
        if k in by and (t := parse_time(by[k].value, tz)) is not None:
            payload[prop] = t.isoformat()
            cite(by[k])
    payload["source_documents"] = doc.id
    payload["status"] = "open"
    return payload, evidence, unchecked


# ---------------- 登记 ----------------


async def _user(session: AsyncSession, tenant_id: str, login: str) -> User | None:
    return (
        await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
    ).one_or_none()


async def register(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    doc_id: str,
    cfg: TenderConfig,
    tz: ZoneInfo,
    *,
    owner: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppendResult:
    """从核对过的招标文件字段登记招标项目，并按截止时间生成提醒待办。"""
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != principal.tenant_id:
        raise TenderNotFound("文档不存在")
    if doc.status != "parsed":
        raise TenderConflict("文档还没有解析完成")
    fields = await _fields(session, principal.tenant_id, [doc.id])
    if not fields:
        raise TenderConflict("这份文档没有抽取字段，请先选择招标文件 Schema 抽取")
    payload, evidence, unchecked = tender_payload(doc, fields, tz)
    if unchecked:
        raise TenderConflict("请先在字段核对页确认或改正这些字段：" + "、".join(unchecked))
    for k, v in (overrides or {}).items():
        if v in (None, ""):
            continue
        payload[k] = v
        evidence.append(Evidence(kind="manual", ref=f"tender:{k}", note=f"登记时人工填写 {k}"))
    if not payload.get("bid_deadline"):
        raise TenderConflict("没有投标截止时间：请在字段核对页填写（截止时间必须设提醒）")
    if owner:
        if await _user(session, principal.tenant_id, owner) is None:
            raise TenderError(f"负责人 {owner} 不存在")
    else:
        me = await session.get(User, principal.subject_id)
        owner = me.login if me is not None else None
    if owner:
        payload["owner"] = owner
    req = AppendRequest(
        type="sales.tender_registered",
        object_id=new_tender_id(),
        payload=payload,
        evidence=evidence,
        idempotency_key=f"tender:{doc.id}",
        correlation_id=f"document:{doc.id}"[:64],
        data_mode=doc.data_mode,
    )
    res = await ledger.append(session, principal, req)
    if not res.duplicate:
        o = await get_tender(session, principal.tenant_id, res.object_id)
        await deadline_todos(session, principal, o, cfg)
    return res


async def deadline_todos(
    session: AsyncSession, principal: Principal, tender: ObjectState, cfg: TenderConfig
) -> list[Todo]:
    """截止时间 → 待办（负责人）。截止时间改了之后旧待办作废、生成新的。"""
    s = tender.state
    owner = await _user(session, tender.tenant_id, s.get("owner") or "")
    old = await session.scalars(
        select(Todo).where(
            Todo.tenant_id == tender.tenant_id,
            Todo.object_type == "tender",
            Todo.object_id == tender.object_id,
            Todo.source == "system:tender",
            Todo.status == "open",
        )
    )
    for t in old:
        t.status, t.closed_at, t.note = "dismissed", utcnow(), "截止时间变更，已重新生成"
    out = []
    now = utcnow()
    for key, label in (("clarification_deadline", "澄清 / 质疑截止"), ("bid_deadline", "投标截止")):
        if not s.get(key):
            continue
        due = datetime.fromisoformat(s[key])
        if due <= now:
            continue
        t = Todo(
            tenant_id=tender.tenant_id,
            title=f"{label}：{s.get('name', '')}"[:80],
            body=f"招标编号 {s.get('tender_no') or '-'}，{label} {due:%Y-%m-%d %H:%M}。",
            assignee_user_id=owner.id if owner else None,
            assignee_role=None if owner else (cfg.reminders.notify_roles or [None])[0],
            object_type="tender",
            object_id=tender.object_id,
            due_at=due.astimezone(UTC),
            status="open",
            source="system:tender",
            data_mode=tender.data_mode,
            created_by=principal.subject_id,
            note="",
            created_at=now,
        )
        session.add(t)
        out.append(t)
    await session.flush()
    return out


async def get_tender(session: AsyncSession, tenant_id: str, tender_id: str) -> ObjectState:
    o = await session.get(ObjectState, (tenant_id, "tender", tender_id))
    if o is None:
        raise TenderNotFound(f"招标项目 {tender_id} 不存在")
    return o


# ---------------- 拆解 ----------------


def _docs(tender: ObjectState) -> list[str]:
    return [d for d in (tender.state.get("source_documents") or "").split(",") if d]


async def _pages(
    session: AsyncSession, tenant_id: str, doc_ids: list[str]
) -> tuple[list[tuple[int, str]], str]:
    """来源文件的页（多份文件时页码按文件顺序接续）。返回 (页, data_mode)。"""
    pages: list[tuple[int, str]] = []
    mode = "real"
    offset = 0
    for did in doc_ids:
        doc = await session.get(Document, did)
        if doc is None or doc.tenant_id != tenant_id:
            continue
        rows = list(
            await session.scalars(
                select(DocumentPage).where(DocumentPage.document_id == did).order_by(DocumentPage.page)
            )
        )
        for p in rows:
            pages.append((offset + p.page, p.markdown))
            if p.data_mode == "mock":
                mode = "mock"
        offset += max((p.page for p in rows), default=0)
        if doc.data_mode == "mock":
            mode = "mock"
    return pages, mode


async def _objects(session: AsyncSession, tenant_id: str, object_type: str) -> list[ObjectState]:
    return list(
        await session.scalars(
            select(ObjectState).where(
                ObjectState.tenant_id == tenant_id, ObjectState.object_type == object_type
            )
        )
    )


def _deadline_date(tender: ObjectState) -> date:
    return datetime.fromisoformat(tender.state["bid_deadline"]).date()


def _age_months(issued: str | None, on: date) -> float | None:
    if not issued:
        return None
    d = date.fromisoformat(issued)
    return round((on - d).days / 30.44, 1)


def match_documents(
    needs: list[dict[str, Any]], creds: list[ObjectState], deadline: date
) -> tuple[list[dict[str, Any]], bool]:
    """资料清单 × 资质资料库：已有 / 过期 / 时效不够 / 数量不够 / 缺失 / 按格式编制。

    返回 (清单, 是否用到模拟数据)。"""
    out = []
    mock = False
    for n in needs:
        row = {**n, "matches": [], "disposition": None}
        kind = n.get("credential_kind")
        if not kind:
            row["status"], row["status_text"] = "prepare", "按招标文件格式编制"
            out.append(row)
            continue
        cands = [c for c in creds if c.state.get("kind") == kind]
        valid = []
        for c in sorted(cands, key=lambda c: c.state.get("issued_on") or "", reverse=True):
            s = c.state
            problem = None
            if s.get("valid_until") and date.fromisoformat(s["valid_until"]) < deadline:
                problem = f"有效期至 {s['valid_until']}，早于投标截止"
            age = _age_months(s.get("issued_on"), deadline)
            if (
                not problem
                and n.get("max_age_months") is not None
                and (age is None or age > n["max_age_months"])
            ):
                problem = f"签发于 {s.get('issued_on') or '未知'}，超过 {n['max_age_months']:g} 个月时效"
            row["matches"].append(
                {
                    "id": c.object_id,
                    "name": s.get("name"),
                    "number": s.get("number"),
                    "issued_on": s.get("issued_on"),
                    "valid_until": s.get("valid_until"),
                    "problem": problem,
                    "data_mode": c.data_mode,
                }
            )
            if not problem:
                valid.append(c)
        if any(c.data_mode == "mock" for c in valid):
            mock = True
        need_n = n.get("min_count") or 1
        if len(valid) >= need_n:
            row["status"], row["status_text"] = "ok", f"资料库已有 {len(valid)} 份"
        elif valid:
            row["status"], row["status_text"] = (
                "insufficient",
                f"要求不少于 {need_n} 份，资料库只有 {len(valid)} 份有效",
            )
        elif cands:
            row["status"], row["status_text"] = "expired", "资料库里的都已过期或超过时效"
        else:
            row["status"], row["status_text"] = "missing", "资料库里没有"
        out.append(row)
    return out, mock


def _kb_value(cap: Capability, products: list[ObjectState], offered: bool) -> tuple[float | None, str, bool]:
    """知识库能力值：已选定产品（已发布报价的方案）取最差的一个；没选定取知识库里最好的一个。"""
    vals = [(p, p.state.get(cap.property or "")) for p in products]
    vals = [(p, float(v)) for p, v in vals if isinstance(v, int | float)]
    if not vals:
        return None, "知识库里没有这项参数", False
    pick_max = (cap.better == "higher") != offered
    p, v = max(vals, key=lambda x: x[1]) if pick_max else min(vals, key=lambda x: x[1])
    code = p.state.get("code") or p.object_id
    basis = (f"所报方案产品 {code}" if offered else f"知识库 {code}（尚未选定产品）") + f"：{v:g}{cap.unit}"
    return v, basis, p.data_mode == "mock"


def _cred_age(cap: Capability, creds: list[ObjectState], deadline: date) -> tuple[float | None, str, bool]:
    cs = [c for c in creds if c.state.get("kind") == cap.credential_kind and c.state.get("issued_on")]
    if not cs:
        return None, "资料库里没有这类资料", False
    c = max(cs, key=lambda c: c.state["issued_on"])
    age = _age_months(c.state["issued_on"], deadline)
    return (
        age,
        f"最新一份 {c.state.get('name')} 签发于 {c.state['issued_on']}（截止时约 {age:g} 个月）",
        c.data_mode == "mock",
    )


async def _offered_products(session: AsyncSession, tender: ObjectState) -> list[ObjectState] | None:
    """转了工程项目且已发布报价的，取报价方案里的产品。"""
    pid = tender.state.get("eng_project")
    if not pid:
        return None
    proj = await session.get(ObjectState, (tender.tenant_id, "eng_project", pid))
    if proj is None or not proj.state.get("quote_design"):
        return None
    d = await session.get(EngDesign, proj.state["quote_design"])
    if d is None:
        return None
    out = []
    for layer in d.inputs.get("layers", []):
        p = await session.get(ObjectState, (tender.tenant_id, "product", layer.get("product_id")))
        if p is not None:
            out.append(p)
    return out or None


def suggest_response(
    item: dict[str, Any], dev: dict[str, Any] | None, docs: dict[str, dict[str, Any]]
) -> str:
    """建议响应（模板拼装，人可以改）。"""
    if dev:
        s = dev["status"]
        if s in ("无偏离", "正偏离"):
            return f"满足（{s}）：{dev['basis']}"
        if s == "负偏离":
            return f"不满足：{dev['basis']}，要求 {dev['required']}。需调整产品或接受偏离"
        return f"待确认：{dev['basis']}"
    linked = [
        d
        for d in docs.values()
        if any(s["quote"] in item["text"] or item["text"] in s["quote"] for s in d["sources"])
    ]
    if linked:
        return "；".join(f"提供{d['name']}（{d['status_text']}）" for d in linked)
    if item["category"] == "格式":
        return "按要求编制投标文件"
    return "完全响应，承诺满足"


async def build(
    session: AsyncSession, tender: ObjectState, cfg: TenderConfig, *, today: date
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """拆解一份招标项目的来源文件。返回 (data, items, data_mode)。纯读取，不写任何东西。"""
    pages, mode = await _pages(session, tender.tenant_id, _docs(tender))
    if not pages:
        raise TenderConflict("来源文件没有解析结果")
    if tender.data_mode == "mock":
        mode = "mock"
    lines, sections = dc.lines_of(pages, cfg)
    raw = dc.clauses(lines, cfg)
    deadline = _deadline_date(tender)
    creds = await _objects(session, tender.tenant_id, "credential")
    checklist, cred_mock = match_documents(dc.requested_documents(lines, cfg), creds, deadline)
    docs = {d["id"]: d for d in checklist}
    offered = await _offered_products(session, tender)
    products = offered or [p for p in await _objects(session, tender.tenant_id, "product")]
    used_mock = cred_mock

    def ours(cap: Capability) -> tuple[float | None, str, bool]:
        if cap.source == "value":
            return cap.value, f"我方承诺 {cap.value:g}{cap.unit}（配置值）", False
        if cap.source == "kb":
            return _kb_value(cap, products, offered is not None)
        return _cred_age(cap, creds, deadline)

    def deviation(cap: Capability, req: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        nonlocal used_mock
        v, basis, m = ours(cap)
        used_mock = used_mock or m
        word = "不低于" if req["rule"] == "min" else "不超过"
        return {
            "capability": cap.id,
            "name": cap.name,
            "required": f"{word} {req['value']:g}{cap.unit}",
            "rule": req["rule"],
            "required_value": req["value"],
            "ours": v,
            "unit": cap.unit,
            "basis": basis,
            "status": dc.compare(req, v, cap),
            "source": source,
        }

    items: list[dict[str, Any]] = []
    deviations: list[dict[str, Any]] = []
    for c in raw:
        dev = None
        for cap in cfg.capabilities:
            if (req := dc.parse_requirement(c["text"], cap)) is not None:
                dev = deviation(cap, req, {"item": c["id"], "page": c["page"], "quote": c["text"][:200]})
                deviations.append({**dev, "item": c["id"], "no": c["no"], "level": c["level"]})
                break
        items.append(
            {
                **c,
                "deviation": dev,
                "response": suggest_response(c, dev, docs),
                "status": "open",
                "assignee": None,
                "note": "",
                "by": None,
                "by_name": None,
                "at": None,
            }
        )
    # 关键信息里的交货期、质保期也进偏离表（原文是“交货期：45 日历天”这种陈述句，不在需求矩阵里）
    covered = {d["capability"] for d in deviations}
    for cap in cfg.capabilities:
        if not cap.key_field or cap.id in covered or not tender.state.get(cap.key_field):
            continue
        text = f"{cap.match[0]}：{tender.state[cap.key_field]}"
        if (req := dc.parse_requirement(text, cap)) is not None:
            deviations.append(
                {
                    **deviation(cap, req, {"field": cap.key_field, "quote": text}),
                    "item": None,
                    "no": "关键信息",
                    "level": "normal",
                }
            )

    score = dc.scoring(lines, cfg)
    data = {
        "sections": sections,
        "scoring": score,
        "checklist": checklist,
        "deviations": deviations,
        "pages": len(pages),
        "used_mock": used_mock,
    }
    return data, items, "mock" if (mode == "mock" or used_mock) else "real"


async def latest(session: AsyncSession, tenant_id: str, tender_id: str) -> TenderAnalysis | None:
    return (
        await session.scalars(
            select(TenderAnalysis)
            .where(TenderAnalysis.tenant_id == tenant_id, TenderAnalysis.tender_id == tender_id)
            .order_by(TenderAnalysis.version.desc())
            .limit(1)
        )
    ).one_or_none()


_KEEP = ("status", "response", "assignee", "note", "by", "by_name", "at")


async def analyze(
    session: AsyncSession,
    principal: Principal,
    tender_id: str,
    cfg: TenderConfig,
    *,
    today: date,
    note: str = "",
) -> TenderAnalysis:
    """拆解（或重新拆解）：生成新版本。人工处理过的条款、资料按 id 带到新版本。"""
    tender = await get_tender(session, principal.tenant_id, tender_id)
    if tender.state.get("status") != "open":
        raise TenderConflict("招标项目已经提交或结束，不能重新拆解")
    prev = await latest(session, principal.tenant_id, tender_id)
    if prev is not None:
        await reopen_if_returned(session, prev)
        if prev.status == "submitted":
            raise TenderConflict("投标正在审批中，不能重新拆解")
    data, items, mode = await build(session, tender, cfg, today=today)
    if prev is not None:
        old = {i["id"]: i for i in prev.items}
        for it in items:
            o = old.get(it["id"])
            if o and o.get("by"):
                it.update({k: o.get(k) for k in _KEEP})
                if (o.get("deviation") or {}).get("manual") and it.get("deviation"):
                    it["deviation"] = {
                        **it["deviation"],
                        **{k: o["deviation"][k] for k in ("status", "manual", "manual_note")},
                    }
        old_docs = {d["id"]: d for d in prev.data.get("checklist", [])}
        for d in data["checklist"]:
            if (o := old_docs.get(d["id"])) and o.get("disposition"):
                d.update({k: o.get(k) for k in ("disposition", "disposition_note", "disposition_by")})
        prev.status, prev.updated_at = "superseded", utcnow()
    now = utcnow()
    a = TenderAnalysis(
        tenant_id=principal.tenant_id,
        tender_id=tender_id,
        version=(prev.version + 1) if prev else 1,
        status="draft",
        data_mode=mode,
        data=data,
        items=items,
        tender_version=tender.version,
        note=note[:500],
        created_by=principal.subject_id,
        created_by_name=principal.display_name,
        created_at=now,
        updated_at=now,
    )
    session.add(a)
    await session.flush()
    return a


async def get_analysis(session: AsyncSession, tenant_id: str, analysis_id: str) -> TenderAnalysis:
    a = await session.get(TenderAnalysis, analysis_id)
    if a is None or a.tenant_id != tenant_id:
        raise TenderNotFound("拆解结果不存在")
    return a


async def reopen_if_returned(session: AsyncSession, a: TenderAnalysis) -> None:
    """投标审批被驳回 / 撤回 / 过期的回到草稿。"""
    if a.status == "submitted" and a.approval_id:
        ap = await session.get(Approval, a.approval_id)
        if ap is None or ap.status not in ("pending", "approved"):
            a.status, a.updated_at = "draft", utcnow()


def _editable(a: TenderAnalysis) -> None:
    if a.status != "draft":
        raise TenderConflict(
            "只能修改最新的草稿版本" if a.status == "superseded" else "投标已提交审批，不能修改"
        )


def update_item(
    a: TenderAnalysis, principal: Principal, item_id: str, change: dict[str, Any]
) -> dict[str, Any]:
    """改一条需求：状态、建议响应、负责人、备注；也可以人工改偏离结论（留痕）。"""
    _editable(a)
    items = [dict(i) for i in a.items]
    for it in items:
        if it["id"] != item_id:
            continue
        if "status" in change:
            if change["status"] not in ITEM_STATUS:
                raise TenderError("状态应为 open / responded / accepted / na")
            it["status"] = change["status"]
        if "response" in change:
            it["response"] = str(change["response"] or "")[:1000]
        if "assignee" in change:
            it["assignee"] = (str(change["assignee"]) or None) if change["assignee"] else None
        if "note" in change:
            it["note"] = str(change["note"] or "")[:300]
        if "deviation" in change:
            if change["deviation"] not in DEVIATIONS:
                raise TenderError("偏离应为 无偏离 / 正偏离 / 负偏离 / 待确认")
            dev = dict(it.get("deviation") or {"name": "人工判断", "required": "-", "basis": "人工判断"})
            dev.update(
                status=change["deviation"], manual=True, manual_note=str(change.get("note") or "")[:300]
            )
            it["deviation"] = dev
        it.update(by=principal.subject_id, by_name=principal.display_name, at=datetime.now(UTC).isoformat())
        a.items = items
        a.updated_at = utcnow()
        return it
    raise TenderNotFound("条款不存在")


def update_document(
    a: TenderAnalysis, principal: Principal, doc_id: str, disposition: str | None, note: str = ""
) -> dict[str, Any]:
    """资料清单：缺的资料线下补齐后标“已备齐”，或标“不适用”。"""
    _editable(a)
    if disposition is not None and disposition not in DOC_DISPOSITIONS:
        raise TenderError("处理结果应为 prepared / na")
    data = dict(a.data)
    rows = [dict(d) for d in data.get("checklist", [])]
    for d in rows:
        if d["id"] != doc_id:
            continue
        d.update(disposition=disposition, disposition_note=note[:200], disposition_by=principal.display_name)
        data["checklist"] = rows
        a.data = data
        a.updated_at = utcnow()
        return d
    raise TenderNotFound("资料不存在")


# ---------------- 汇总、风险、提交 ----------------


def _dev_status(it: dict[str, Any]) -> str | None:
    return (it.get("deviation") or {}).get("status")


def summary(a: TenderAnalysis) -> dict[str, Any]:
    items = a.items
    must = [i for i in items if i["level"] in MUST_HANDLE or i["disqualify"]]
    docs_bad = [
        d
        for d in a.data.get("checklist", [])
        if d["status"] in ("missing", "expired", "insufficient") and not d.get("disposition")
    ]
    out = {
        "total": len(items),
        "star": sum(i["level"] == "star" for i in items),
        "key": sum(i["level"] == "key" for i in items),
        "disqualify": sum(i["disqualify"] for i in items),
        "open": sum(i["status"] == "open" for i in items),
        "must_open": sum(i["status"] == "open" for i in must),
        "star_open": sum(i["status"] == "open" for i in items if i["level"] == "star"),
        "negative": sum(_dev_status(i) == "负偏离" for i in items)
        + sum(d["status"] == "负偏离" for d in a.data.get("deviations", []) if d.get("item") is None),
        "star_negative": sum(_dev_status(i) == "负偏离" for i in items if i["level"] == "star"),
        "pending_deviation": sum(_dev_status(i) == "待确认" and i["status"] == "open" for i in items),
        "docs_missing": len(docs_bad),
        "docs_total": len(a.data.get("checklist", [])),
    }
    out["ready"] = out["must_open"] == 0 and out["docs_missing"] == 0
    return out


def risks(a: TenderAnalysis, tender: ObjectState, cfg: TenderConfig, now: datetime) -> list[dict[str, Any]]:
    """方案 4.5 招标风险：★条款未响应、负偏离、资格材料缺失或过期、截止时间临近。每条带页码。"""
    out: list[dict[str, Any]] = []
    if a.data_mode == "mock":
        out.append(
            {
                "type": "模拟数据",
                "level": "block",
                "text": "用了模拟数据（模拟 OCR 或演示资料库），不能提交投标、不能导出正式文件",
            }
        )
    deadline = datetime.fromisoformat(tender.state["bid_deadline"])
    left = deadline - now
    if left <= timedelta(0):
        out.append(
            {"type": "截止时间", "level": "block", "text": f"投标截止时间 {deadline:%m-%d %H:%M} 已过"}
        )
    elif left <= timedelta(days=cfg.reminders.warn_days):
        out.append(
            {
                "type": "截止时间",
                "level": "warn",
                "text": f"离投标截止只剩 {left.total_seconds() / 3600:.0f} 小时（{deadline:%m-%d %H:%M}）",
            }
        )
    for i in a.items:
        if i["level"] == "star" and i["status"] == "open":
            out.append(
                {
                    "type": "★未响应",
                    "level": "block",
                    "page": i["page"],
                    "item": i["id"],
                    "text": i["text"][:120],
                }
            )
        if _dev_status(i) == "负偏离":
            star = i["level"] == "star"
            out.append(
                {
                    "type": "★负偏离" if star else "负偏离",
                    "level": "block" if star else "warn",
                    "page": i["page"],
                    "item": i["id"],
                    "text": f"{i['text'][:80]}（{i['deviation']['basis']}）",
                }
            )
    for d in a.data.get("deviations", []):
        if d.get("item") is None and d["status"] == "负偏离":
            out.append(
                {
                    "type": "负偏离",
                    "level": "warn",
                    "text": f"{d['name']}：要求 {d['required']}，{d['basis']}",
                }
            )
    for d in a.data.get("checklist", []):
        if d["status"] in ("missing", "expired", "insufficient") and not d.get("disposition"):
            page = d["sources"][0]["page"] if d["sources"] else None
            out.append(
                {
                    "type": "资料缺失",
                    "level": "block",
                    "page": page,
                    "text": f"{d['name']}：{d['status_text']}",
                }
            )
    return out


def submit_request(
    a: TenderAnalysis, tender: ObjectState, bid_amount: float | None, now: datetime
) -> AppendRequest:
    """提交前检查，生成 sales.tender_submitted 事件。审批规则与红线在 Ledger.append() 里生效。"""
    if a.status == "superseded":
        raise TenderConflict("这是旧版本，请在最新的拆解结果上提交")
    if a.status != "draft" or tender.state.get("status") != "open":
        raise TenderConflict("投标已经提交")
    if a.data_mode == "mock":
        raise TenderConflict("模拟数据不能提交投标")
    if a.tender_version != tender.version:
        raise TenderConflict("招标项目在拆解之后改过，请先重新拆解")
    if datetime.fromisoformat(tender.state["bid_deadline"]) <= now:
        raise TenderConflict("已过投标截止时间")
    s = summary(a)
    if s["must_open"]:
        raise TenderConflict(f"还有 {s['must_open']} 条★/▲/必须条款没有处理")
    if s["docs_missing"]:
        raise TenderConflict(f"资料清单还有 {s['docs_missing']} 项缺失或过期")
    payload: dict[str, Any] = {
        "status": "submitted",
        "analysis": a.id,
        "star_total": s["star"],
        "star_negative": s["star_negative"],
        "docs_missing": s["docs_missing"],
    }
    if bid_amount is not None:
        if bid_amount <= 0:
            raise TenderError("投标报价必须大于 0")
        payload["bid_amount"] = round(float(bid_amount), 2)
    return AppendRequest(
        type="sales.tender_submitted",
        object_id=tender.object_id,
        payload=payload,
        evidence=[
            Evidence(
                kind="calculation",
                ref=f"tender_analysis:{a.id}",
                note=f"拆解 v{a.version}：{s['total']} 条需求，★ {s['star']} 条，负偏离 {s['negative']} 条",
            )
        ],
        idempotency_key=f"tender-submit:{a.id}",
        correlation_id=f"tender:{tender.object_id}"[:64],
        data_mode="real",
        expected_version=tender.version,
    )


# ---------------- 转工程项目（应用 A） ----------------


async def to_eng_project(
    session: AsyncSession, principal: Principal, ledger: Ledger, tender_id: str
) -> tuple[str, bool]:
    """技术规格里的工况、工程量 → 工程项目（选材、传热、报价走 W10–W12 的流程）。

    返回 (工程项目 id, 是否新建)。"""
    from fos.engineering import service as es

    tender = await get_tender(session, principal.tenant_id, tender_id)
    if tender.state.get("eng_project"):
        return tender.state["eng_project"], False
    docs = _docs(tender)
    if not docs:
        raise TenderConflict("招标项目没有来源文件")
    overrides = {
        "customer_name": tender.state.get("purchaser"),
        "delivery_period": tender.state.get("delivery_period"),
    }
    try:
        res = await es.create_project(session, principal, ledger, docs[0], overrides)
    except es.EngError as e:
        err = TenderError(str(e))
        err.status = e.status
        raise err from e
    await ledger.append(
        session,
        principal,
        AppendRequest(
            type="sales.tender_updated",
            object_id=tender_id,
            payload={"eng_project": res.object_id},
            evidence=[
                Evidence(kind="manual", ref=f"eng_project:{res.object_id}", note="招标技术规格转工程项目")
            ],
            correlation_id=f"tender:{tender_id}"[:64],
            data_mode=tender.data_mode,
            expected_version=tender.version,
        ),
    )
    return res.object_id, not res.duplicate


def tender_out(o: ObjectState, tz: ZoneInfo | None = None, now: datetime | None = None) -> dict[str, Any]:
    out = {
        "id": o.object_id,
        **o.state,
        "data_mode": o.data_mode,
        "version": o.version,
        "updated_at": o.updated_at.isoformat(),
    }
    if o.state.get("bid_deadline") and now is not None:
        left = datetime.fromisoformat(o.state["bid_deadline"]) - now
        out["hours_left"] = round(left.total_seconds() / 3600, 1)
    if o.data_mode == "mock":
        out["notice"] = "模拟数据，非真实识别结果"
    return out


def analysis_out(a: TenderAnalysis, *, full: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": a.id,
        "tender_id": a.tender_id,
        "version": a.version,
        "status": a.status,
        "data_mode": a.data_mode,
        "summary": summary(a),
        "approval_id": a.approval_id,
        "created_by_name": a.created_by_name,
        "created_at": a.created_at.isoformat(),
        "note": a.note,
    }
    if full:
        out.update(items=a.items, **a.data)
    if a.data_mode == "mock":
        out["notice"] = "模拟数据，不能提交投标、不能导出正式文件"
    return out
