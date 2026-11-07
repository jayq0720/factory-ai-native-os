"""工程项目与方案草稿的服务层：从文件字段建项目（Ledger.append）→ 匹配 → 方案版本 → 审核 → 发布报价。

路由（fos/api/eng_routes.py）只做参数与权限，业务都在这里，方便 Agent 工具和 CLI 复用。
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Approval, Document, DocumentField, EngDesign, ObjectState, utcnow
from fos.engineering import review as rv
from fos.engineering.config import EngineeringConfig
from fos.engineering.design import Conditions, QuoteOptions, default_options, evaluate
from fos.engineering.kb import load_kb
from fos.engineering.match import customer_spec, search
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger

HUMAN = {"confirmed", "corrected"}
# 参与计算的字段：有值就必须人工核对过（确认或改正），否则不能建项目
CRITICAL = ["normal_temp", "max_temp", "atmosphere", "shell_temp", "area", "total_thickness"]
# 文件字段 → 工程项目属性
TEXT_MAP = {
    "customer_name": "customer_name",
    "project_location": "location",
    "furnace_type": "furnace_type",
    "position": "positions",
    "atmosphere": "atmosphere",
    "standards": "standards",
    "delivery_period": "delivery_period",
    "payment_terms": "payment_terms",
    "quote_validity": "quote_validity",
}
NUM_MAP = {
    "normal_temp": "normal_temp",
    "max_temp": "max_temp",
    "shell_temp": "shell_temp",
    "total_thickness": "thickness_limit",
    "tax_rate": "tax_rate",
}
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


class EngError(Exception):
    status = 422


class EngNotFound(EngError):
    status = 404


class EngConflict(EngError):
    status = 409


def _num(v: str | None) -> float | None:
    m = _NUM.search(v or "")
    return float(m.group()) if m else None


def _thickness_rule(quote: str) -> str:
    if re.search(r"不小于|不低于|不少于|≥|以上", quote):
        return "min"
    if re.search(r"不大于|不超过|不高于|≤|以内", quote):
        return "max"
    return "ref"


def new_project_id() -> str:
    day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%y%m%d")
    return f"EP-{day}-{secrets.token_hex(2).upper()}"


async def _fields(session: AsyncSession, tenant_id: str, doc_ids: list[str]) -> list[DocumentField]:
    if not doc_ids:
        return []
    rows = await session.scalars(
        select(DocumentField).where(
            DocumentField.tenant_id == tenant_id, DocumentField.document_id.in_(doc_ids)
        )
    )
    return list(rows)


def field_dict(f: DocumentField) -> dict[str, Any]:
    return {
        "document_id": f.document_id,
        "key": f.key,
        "name": f.name,
        "value": f.value,
        "unit": f.unit,
        "status": f.status,
        "required": f.required,
        "conflict": f.conflict,
        "sources": f.sources or [],
        "candidates": f.candidates or [],
    }


def project_payload(
    doc: Document, fields: list[DocumentField]
) -> tuple[dict[str, Any], list[Evidence], list[str]]:
    """把文件字段映射成工程项目属性。返回 (payload, 证据, 还没人工核对的关键字段名)。"""
    by = {f.key: f for f in fields if f.value}
    payload: dict[str, Any] = {}
    evidence: list[Evidence] = []
    unchecked = [by[k].name for k in CRITICAL if k in by and (by[k].status not in HUMAN)] + [
        f.name for f in fields if f.conflict and f.status not in HUMAN and f.key not in CRITICAL and f.value
    ]

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
    payload["name"] = name.value if name else doc.filename.rsplit(".", 1)[0]
    if name:
        cite(name)
    for k, prop in TEXT_MAP.items():
        if k in by:
            payload[prop] = by[k].value
            cite(by[k])
    for k, prop in NUM_MAP.items():
        if k in by and (v := _num(by[k].value)) is not None:
            payload[prop] = v
            cite(by[k])
    if "area" in by:
        nums = [float(x) for x in _NUM.findall(by["area"].value or "")]
        if nums:
            payload["area"] = round(sum(nums), 3)
            cite(by["area"])
    if "total_thickness" in by and "thickness_limit" in payload:
        q = " ".join(s.get("quote") or "" for s in by["total_thickness"].sources or []) or (
            by["total_thickness"].value or ""
        )
        payload["thickness_rule"] = _thickness_rule(q)
    if "shell_temp" in by:
        q = " ".join(s.get("quote") or "" for s in by["shell_temp"].sources or [])
        m = re.search(r"环境温度\s*(-?\d+(?:\.\d+)?)", q)
        if m:
            payload["ambient_temp"] = float(m.group(1))
    spec = [
        f"{label}：{by[k].value}"
        for k, label in (("hot_face", "工作层"), ("insulation_layer", "隔热层"))
        if k in by
    ]
    if spec:
        payload["lining_spec"] = "；".join(spec)
    payload["source_documents"] = doc.id
    payload["status"] = "open"
    return payload, evidence, unchecked


async def create_project(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    doc_id: str,
    overrides: dict[str, Any] | None = None,
) -> AppendResult:
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != principal.tenant_id:
        raise EngNotFound("文档不存在")
    if doc.status != "parsed":
        raise EngConflict("文档还没有解析完成")
    fields = await _fields(session, principal.tenant_id, [doc.id])
    if not fields:
        raise EngConflict("这份文档没有抽取字段，请先选择技术文件 Schema 抽取")
    payload, evidence, unchecked = project_payload(doc, fields)
    if unchecked:
        raise EngConflict("请先在字段核对页确认或改正这些字段：" + "、".join(unchecked))
    for k, v in (overrides or {}).items():
        if v in (None, ""):
            continue
        payload[k] = v
        evidence.append(Evidence(kind="manual", ref=f"eng_project:{k}", note=f"建项目时人工填写 {k}"))
    req = AppendRequest(
        type="eng.project_created",
        object_id=new_project_id(),
        payload=payload,
        evidence=evidence,
        idempotency_key=f"eng-project:{doc.id}",
        correlation_id=f"document:{doc.id}"[:64],
        data_mode=doc.data_mode,
    )
    return await ledger.append(session, principal, req)


async def get_project(session: AsyncSession, tenant_id: str, project_id: str) -> ObjectState:
    o = await session.get(ObjectState, (tenant_id, "eng_project", project_id))
    if o is None:
        raise EngNotFound(f"工程项目 {project_id} 不存在")
    return o


# ---------------- 方案 ----------------


async def _context(session: AsyncSession, project: ObjectState, cfg: EngineeringConfig):
    kb = await load_kb(session, project.tenant_id)
    try:
        cond = Conditions.from_project(project.state, cfg)
    except ValueError as e:
        raise EngConflict(str(e)) from e
    docs = [d for d in (project.state.get("source_documents") or "").split(",") if d]
    fields = [field_dict(f) for f in await _fields(session, project.tenant_id, docs)]
    return kb, cond, fields


def _new_design(
    principal: Principal,
    project: ObjectState,
    label: str,
    title: str,
    version: int,
    inputs: dict[str, Any],
    data: dict[str, Any],
    review: list[dict[str, Any]],
    *,
    parent_id: str | None = None,
    note: str = "",
) -> EngDesign:
    now = utcnow()
    return EngDesign(
        tenant_id=project.tenant_id,
        project_id=project.object_id,
        label=label,
        title=title[:64],
        version=version,
        parent_id=parent_id,
        status="draft",
        data_mode=data["data_mode"],
        inputs=inputs,
        data=data,
        review=review,
        project_version=project.version,
        note=note[:500],
        created_by=principal.subject_id,
        created_by_name=principal.display_name,
        created_at=now,
        updated_at=now,
    )


async def match_project(
    session: AsyncSession, principal: Principal, project_id: str, cfg: EngineeringConfig
) -> dict[str, Any]:
    """知识库匹配并生成方案草稿（第 1 版）。重新匹配时旧草稿标为 superseded。"""
    project = await get_project(session, principal.tenant_id, project_id)
    kb, cond, fields = await _context(session, project, cfg)
    result = search(cond, cfg, kb)
    options = default_options(cond, cfg)
    proposals = [
        (a["label"], a["title"], {"layers": a["layers"], "template_id": a["template_id"]}, a)
        for a in result["alternatives"]
    ]
    spec = customer_spec(project.state.get("lining_spec"), kb)
    if spec:
        proposals.append(("客户方案", "按客户文件要求", {"layers": spec, "template_id": None}, None))
    if not proposals:
        return {**result, "designs": []}

    old = await session.scalars(
        select(EngDesign).where(
            EngDesign.tenant_id == principal.tenant_id,
            EngDesign.project_id == project_id,
            EngDesign.status == "draft",
        )
    )
    for d in old:
        d.status, d.updated_at = "superseded", utcnow()
    versions = await _max_versions(session, principal.tenant_id, project_id)

    designs = []
    for label, title, inputs, alt in proposals:
        inputs = {**inputs, "options": options.as_dict()}
        data = evaluate(cond, cfg, kb, inputs["layers"], options, project_mode=project.data_mode)
        if alt:
            data["reasons"], data["notes"] = alt["reasons"], alt["notes"]
            data["template"] = {"id": alt["template_id"], "name": alt["template_name"]}
        else:
            data["reasons"] = ["按客户文件写明的衬里结构计算，用于对照校核（不一定满足全部约束）。"]
            data["notes"] = []
        review = rv.build_review(project.state, data, fields, cfg)
        d = _new_design(principal, project, label, title, versions.get(label, 0) + 1, inputs, data, review)
        session.add(d)
        designs.append(d)
    await session.flush()
    return {**result, "designs": designs}


async def _max_versions(session: AsyncSession, tenant_id: str, project_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(EngDesign.label, EngDesign.version).where(
            EngDesign.tenant_id == tenant_id, EngDesign.project_id == project_id
        )
    )
    out: dict[str, int] = {}
    for label, v in rows:
        out[label] = max(out.get(label, 0), v)
    return out


async def get_design(session: AsyncSession, tenant_id: str, design_id: str) -> EngDesign:
    d = await session.get(EngDesign, design_id)
    if d is None or d.tenant_id != tenant_id:
        raise EngNotFound("方案不存在")
    return d


async def revise_design(
    session: AsyncSession,
    principal: Principal,
    design_id: str,
    cfg: EngineeringConfig,
    *,
    layers: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    note: str = "",
) -> EngDesign:
    """修改厚度 / 产品 / 报价选项（或工况变了之后重算）：生成新版本，用量和报价全部重算。"""
    d = await get_design(session, principal.tenant_id, design_id)
    await reopen_if_returned(session, d)
    if d.status != "draft":
        raise EngConflict(
            "只能修改最新的草稿版本" if d.status == "superseded" else "方案已提交审批或已发布，不能修改"
        )
    project = await get_project(session, principal.tenant_id, d.project_id)
    kb, cond, fields = await _context(session, project, cfg)
    opts = {**d.inputs.get("options", {}), **(options or {})}
    qo = QuoteOptions(**{k: float(v) for k, v in opts.items() if k in QuoteOptions.__dataclass_fields__})
    if not (
        0 <= qo.discount_pct < 100
        and 0 <= qo.tax_rate <= 30
        and qo.freight_per_t >= 0
        and qo.install_per_m2 >= 0
    ):
        raise EngError("报价选项超出范围")
    new_layers = layers if layers is not None else d.inputs["layers"]
    try:
        data = evaluate(cond, cfg, kb, new_layers, qo, project_mode=project.data_mode)
    except ValueError as e:
        raise EngError(str(e)) from e
    for k in ("reasons", "notes", "template"):
        if k in d.data:
            data[k] = d.data[k]
    review = rv.build_review(project.state, data, fields, cfg, previous=d.review)
    nd = _new_design(
        principal,
        project,
        d.label,
        d.title,
        d.version + 1,
        {"layers": new_layers, "options": qo.as_dict(), "template_id": d.inputs.get("template_id")},
        data,
        review,
        parent_id=d.id,
        note=note,
    )
    d.status, d.updated_at = "superseded", utcnow()
    session.add(nd)
    await session.flush()
    return nd


async def reopen_if_returned(session: AsyncSession, d: EngDesign) -> None:
    """审批被驳回 / 撤回 / 过期的方案回到草稿，可以修改后重新提交。"""
    if d.status == "submitted" and d.approval_id:
        ap = await session.get(Approval, d.approval_id)
        if ap is None or ap.status not in ("pending", "approved"):
            d.status, d.updated_at = "draft", utcnow()


def dispose(
    design: EngDesign, principal: Principal, item_id: str, disposition: str | None, note: str = ""
) -> dict:
    if design.status != "draft":
        raise EngConflict("只能处理最新草稿版本的审核条目")
    items = [dict(i) for i in design.review]
    for it in items:
        if it["id"] != item_id:
            continue
        if disposition and disposition not in rv.DISPOSITIONS:
            raise EngError("处理结果应为 resolved / accepted / na")
        if it["blocking"] and disposition:
            raise EngConflict("这条必须修正数据后重新计算，不能直接标记")
        it.update(
            disposition=disposition,
            by=principal.subject_id,
            by_name=principal.display_name,
            at=datetime.now(UTC).isoformat(),
            note=note[:200],
        )
        design.review = items
        design.updated_at = utcnow()
        return it
    raise EngNotFound("审核条目不存在")


def publish_request(design: EngDesign, project: ObjectState, quote_no: str | None) -> AppendRequest:
    """发布前检查，生成 eng.quote_issued 事件。审批规则在 Ledger.append() 里生效。"""
    if design.status == "superseded":
        raise EngConflict("这是旧版本，请发布最新版本")
    if design.status == "published" or project.state.get("quote_design") == design.id:
        raise EngConflict("这个方案已经发布")
    if design.data_mode == "mock":
        raise EngConflict("模拟数据不能发布报价")
    if design.project_version != project.version:
        raise EngConflict("工程项目的工况在方案计算之后改过，请先重新计算")
    s = rv.summary(design.review)
    if s["blocking"]:
        raise EngConflict(f"还有 {s['blocking']} 条必须修正的问题（见审核清单）")
    if s["open"]:
        raise EngConflict(f"审核清单还有 {s['open']} 条没有处理")
    q = design.data["quote"]
    if q.get("gross_margin_pct") is None:
        raise EngConflict("缺成本价，无法计算毛利率")
    letter = design.label.replace("方案", "").strip() or "S"
    return AppendRequest(
        type="eng.quote_issued",
        object_id=project.object_id,
        payload={
            "status": "quoted",
            "quote_no": (quote_no or f"Q{project.object_id[2:]}-{letter}{design.version}")[:64],
            "quote_amount": q["total"],
            "gross_margin_pct": q["gross_margin_pct"],
            "quote_design": design.id,
            "quote_prices": "；".join(q.get("prices_used", [])),
        },
        evidence=[
            Evidence(
                kind="calculation", ref=f"eng_design:{design.id}", note=f"{design.label} v{design.version}"
            ),
            *[Evidence(kind="calculation", ref=r, note="价格版本") for r in q.get("prices_used", [])[:20]],
        ],
        idempotency_key=f"eng-quote:{design.id}",
        correlation_id=f"eng_project:{project.object_id}"[:64],
        data_mode="real",
        expected_version=project.version,
    )


def design_status(d: EngDesign, project: ObjectState | None) -> str:
    if project is not None and project.state.get("quote_design") == d.id:
        return "published"
    return d.status


def design_out(d: EngDesign, project: ObjectState | None = None, *, full: bool = True) -> dict[str, Any]:
    q = d.data.get("quote", {})
    out: dict[str, Any] = {
        "id": d.id,
        "project_id": d.project_id,
        "label": d.label,
        "title": d.title,
        "version": d.version,
        "parent_id": d.parent_id,
        "status": design_status(d, project),
        "data_mode": d.data_mode,
        "feasible": d.data.get("feasible"),
        "total_thickness_mm": d.data.get("total_thickness_mm"),
        "shell_temp": (d.data.get("heat_normal") or {}).get("shell_temp"),
        "total": q.get("total"),
        "gross_margin_pct": q.get("gross_margin_pct"),
        "review_summary": rv.summary(d.review),
        "approval_id": d.approval_id,
        "stale": project is not None and d.project_version != project.version,
        "created_by_name": d.created_by_name,
        "created_at": d.created_at.isoformat(),
        "note": d.note,
    }
    if full:
        out.update(inputs=d.inputs, data=d.data, review=d.review)
    if d.data_mode == "mock":
        out["notice"] = "模拟数据，不能发布报价、不能导出正式文件"
    return out


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()
