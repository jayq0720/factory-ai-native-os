"""内置工具：查本体、查对象、追溯、读文档页、提议记账。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from fos.db.models import Document, DocumentPage, Event, ObjectState
from fos.ledger.service import Evidence
from fos.ledger.trace import trace
from fos.ontology import OntologyError
from fos.tools.registry import Proposal, ToolContext, ToolError, tool

MAX_LIST = 50


def _read_perm(ctx: ToolContext, object_type: str) -> str:
    obj = ctx.ontology.objects.get(object_type)
    if obj is None:
        raise ToolError(f"没有对象类型 {object_type}，可用：{', '.join(ctx.ontology.objects)}")
    return f"{obj.domain}.{object_type}:read"


def _obj(o: ObjectState) -> dict[str, Any]:
    return {
        "object_type": o.object_type,
        "object_id": o.object_id,
        "state": o.state,
        "data_mode": o.data_mode,
        "version": o.version,
        "updated_at": o.updated_at.isoformat(),
    }


class DescribeArgs(BaseModel):
    object_type: str | None = None


@tool("ontology.describe", "查看对象类型和它们的属性；不给 object_type 时列出全部对象类型名称", DescribeArgs)
async def describe(ctx: ToolContext, a: DescribeArgs) -> dict[str, Any]:
    if a.object_type is None:
        return {"objects": {k: v.name for k, v in ctx.ontology.objects.items()}}
    obj = ctx.ontology.objects.get(a.object_type)
    if obj is None:
        raise ToolError(f"没有对象类型 {a.object_type}")
    return {
        "object_type": a.object_type,
        "name": obj.name,
        "properties": {k: {"name": p.name, "type": p.type} for k, p in obj.properties.items()},
        "events": {k: e.name for k, e in ctx.ontology.events.items() if e.object == a.object_type},
    }


class GetArgs(BaseModel):
    object_type: str
    object_id: str = Field(min_length=1, max_length=64)
    history: bool = False


@tool("objects.get", "按类型和 id 读取一个对象的当前状态；history=true 时附带事件历史（最近 20 条）", GetArgs)
async def get_object(ctx: ToolContext, a: GetArgs) -> dict[str, Any]:
    ctx.require(_read_perm(ctx, a.object_type))
    o = await ctx.session.get(ObjectState, (ctx.tenant_id, a.object_type, a.object_id))
    if o is None:
        raise ToolError(f"{a.object_type} {a.object_id} 不存在")
    out = _obj(o)
    if a.history:
        evs = (
            await ctx.session.scalars(
                select(Event)
                .where(
                    Event.tenant_id == ctx.tenant_id,
                    Event.object_type == a.object_type,
                    Event.object_id == a.object_id,
                )
                .order_by(Event.seq.desc())
                .limit(20)
            )
        ).all()
        out["history"] = [
            {
                "seq": e.seq,
                "type": e.type,
                "payload": e.payload,
                "actor": e.actor_name,
                "at": e.occurred_at.isoformat(),
            }
            for e in reversed(evs)
        ]
    return out


class ListArgs(BaseModel):
    object_type: str
    where: dict[str, str | int | float | bool] = Field(default_factory=dict)  # 属性等值过滤
    limit: int = Field(default=20, ge=1, le=MAX_LIST)


@tool("objects.list", "列出某类对象（按最近更新排序），可用 where 做属性等值过滤", ListArgs)
async def list_objects(ctx: ToolContext, a: ListArgs) -> dict[str, Any]:
    ctx.require(_read_perm(ctx, a.object_type))
    props = ctx.ontology.objects[a.object_type].properties
    bad = [k for k in a.where if k not in props]
    if bad:
        raise ToolError(f"{a.object_type} 没有属性 {bad}")
    q = (
        select(ObjectState)
        .where(ObjectState.tenant_id == ctx.tenant_id, ObjectState.object_type == a.object_type)
        .order_by(ObjectState.updated_seq.desc())
    )
    items: list[dict[str, Any]] = []
    # 过滤在内存里做：单厂规模下足够，且不依赖数据库的 JSON 方言
    for o in (await ctx.session.scalars(q.limit(2000))).all():
        if all(o.state.get(k) == v for k, v in a.where.items()):
            items.append(_obj(o))
            if len(items) >= a.limit:
                break
    return {"count": len(items), "items": items}


class TraceArgs(BaseModel):
    object_type: str
    object_id: str = Field(min_length=1, max_length=64)
    depth: int = Field(default=3, ge=1, le=6)


@tool(
    "objects.trace",
    "沿引用关系双向追溯（批次 → 原料批次 → 窑次 → 检验）；没有读权限的节点只给位置不给内容",
    TraceArgs,
)
async def trace_object(ctx: ToolContext, a: TraceArgs) -> dict[str, Any]:
    ctx.require(_read_perm(ctx, a.object_type))
    if await ctx.session.get(ObjectState, (ctx.tenant_id, a.object_type, a.object_id)) is None:
        raise ToolError(f"{a.object_type} {a.object_id} 不存在")
    out = await trace(ctx.session, ctx.tenant_id, a.object_type, a.object_id, depth=a.depth, limit=60)
    for n in out["nodes"]:
        t = n["object_type"]
        if t not in ctx.ontology.objects or not ctx.allowed(f"{ctx.ontology.objects[t].domain}.{t}:read"):
            n["state"], n["redacted"] = None, True
    return out


class PageArgs(BaseModel):
    document_id: str
    page: int = Field(default=1, ge=1)


@tool("documents.read_page", "读取已上传文件某一页的识别结果（Markdown）", PageArgs)
async def read_page(ctx: ToolContext, a: PageArgs) -> dict[str, Any]:
    ctx.require("doc.document:read")
    d = await ctx.session.get(Document, a.document_id)
    if d is None or d.tenant_id != ctx.tenant_id:
        raise ToolError("文档不存在")
    p = await ctx.session.get(DocumentPage, (d.id, a.page))
    if p is None:
        raise ToolError(f"第 {a.page} 页不存在（共 {d.page_count} 页）")
    return {
        "document_id": d.id,
        "filename": d.filename,
        "page": p.page,
        "page_count": d.page_count,
        "data_mode": p.data_mode,
        "markdown": p.markdown,
    }


class ProposeArgs(BaseModel):
    type: str
    object_id: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(min_length=1, max_length=40)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


@tool(
    "ledger.propose",
    "提议记一个业务事件。不会直接写入：结束后发卡片，由有权限的人点确认才入账。payload 字段必须符合本体。",
    ProposeArgs,
)
async def propose(ctx: ToolContext, a: ProposeArgs) -> dict[str, Any]:
    if a.type not in ctx.skill.proposes:
        raise ToolError(f"当前 Skill 不能提议 {a.type}，只能提议：{ctx.skill.proposes}")
    ev = ctx.ontology.events[a.type]
    try:
        payload = ctx.ontology.check_payload(a.type, a.payload, creating=ev.op == "create")
    except OntologyError as e:
        raise ToolError(str(e)) from e
    if ev.op == "update" and not a.object_id:
        raise ToolError("更新类事件必须给出 object_id")
    try:
        evidence = [Evidence.model_validate(e).model_dump(exclude_none=True) for e in a.evidence]
    except ValueError as e:
        raise ToolError(f"证据格式不对：{e}") from e
    if ev.evidence == "required" and not evidence:
        raise ToolError(f"“{ev.name}”必须附证据（照片、文件页码等）")
    if len(ctx.proposals) >= 3:
        raise ToolError("一次最多提议 3 条")
    ctx.proposals.append(
        Proposal(type=a.type, object_id=a.object_id, payload=payload, title=a.title, evidence=evidence)
    )
    return {"ok": True, "pending_confirmation": True, "event": ev.name, "payload": payload}
