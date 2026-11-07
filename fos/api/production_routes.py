"""拍照录单与批次追溯的接口（W9）。

- GET  /v1/documents/{id}/entry：按单据抽取结果生成的录单草稿（字段、页码、问题）；
- POST /v1/documents/{id}/entry：控制台核对后提交（以提交人身份 Ledger.append，同一张单据只入账一次）；
- GET  /v1/objects/{type}/{id}/trace/report：批次追溯报告（上游 / 下游 / 关联 + 告警）；
- GET  /v1/trace?q=编号：按生产批号、窑次号、原料批号、报告编号找对象并出报告。
"""

from __future__ import annotations

from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from fos.api.deps import PrincipalDep, SessionDep, require
from fos.db.models import Document, Event, ObjectState
from fos.entry.draft import EntryDraft, build_draft
from fos.extraction.service import load_fields
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, NotFound
from fos.ledger.report import find_root, report_lines, trace_report

router = APIRouter(prefix="/v1")
Reader = Annotated[Principal, Depends(require("doc.document:read"))]
Writer = Annotated[Principal, Depends(require("doc.document:write"))]
_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}


async def _draft(request: Request, session, principal: Principal, doc_id: str) -> tuple[EntryDraft, Document]:
    st = request.app.state
    d = await session.get(Document, doc_id)
    if d is None or d.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="文档不存在")
    schema = st.extractor.schema_for(d.tenant_id, d.kind, d.schema_id) if d.schema_id else None
    if schema is None or schema.entry is None:
        raise HTTPException(422, detail="这份文档不是可录单的单据（在“重新抽取”里选一种单据 Schema）")
    tenant = st.registry.get(d.tenant_id)
    onto = st.ontologies[d.tenant_id]
    draft = await build_draft(
        session, onto, schema, d, await load_fields(session, d.id), tz=ZoneInfo(tenant.timezone)
    )
    return draft, d


async def _entered(session, tenant: str, key: str) -> Event | None:
    q = select(Event).where(Event.tenant_id == tenant, Event.idempotency_key == key)
    return (await session.scalars(q)).one_or_none()


@router.get("/documents/{doc_id}/entry")
async def get_entry(doc_id: str, request: Request, session: SessionDep, principal: Reader) -> dict[str, Any]:
    draft, _ = await _draft(request, session, principal, doc_id)
    out = draft.out()
    prev = await _entered(session, principal.tenant_id, draft.idempotency_key)
    out["entered"] = (
        {"event_id": prev.id, "seq": prev.seq, "object_id": prev.object_id, "by": prev.actor_name}
        if prev
        else None
    )
    if prev:  # 已入账：对象已存在不算问题
        out["ok"], out["problems"] = True, []
    out["permission"] = request.app.state.ontologies[principal.tenant_id].events[draft.event].permission
    return out


@router.post("/documents/{doc_id}/entry", status_code=201)
async def commit_entry(
    doc_id: str, request: Request, response: Response, session: SessionDep, principal: Writer
) -> Any:
    """控制台提交录单。网页上点“提交入账”本身就是人的确认（L2），以提交人的权限写入。"""
    st = request.app.state
    draft, _ = await _draft(request, session, principal, doc_id)
    prev = await _entered(session, principal.tenant_id, draft.idempotency_key)
    if prev is not None:
        response.status_code = status.HTTP_200_OK
        return {"event_id": prev.id, "seq": prev.seq, "object_id": prev.object_id, "duplicate": True}
    if not draft.ok or draft.request is None:
        raise HTTPException(422, detail="；".join(draft.problems) or "草稿不完整")
    try:
        res = await st.ledger.append(session, principal, draft.request)
    except ApprovalRequired as e:
        from fos.api.channel_routes import lookup_for
        from fos.api.inbox_routes import approval_out
        from fos.policy.approvals import send_notices

        lookup = lookup_for(request)
        sub = await st.approvals.submit(
            session, principal, draft.request, e.decision, source="api", lookup=lookup
        )
        await session.commit()
        await send_notices(lookup, principal.tenant_id, sub.notices)
        return JSONResponse(
            {"status": "pending_approval", "message": sub.message(), "approval": approval_out(sub.approval)},
            status_code=202,
        )
    except (Forbidden, NotFound, Conflict, Invalid) as e:
        raise HTTPException(_STATUS[type(e)], detail=str(e)) from e
    return {**res.model_dump(), "duplicate": res.duplicate}


# ---------------- 追溯 ----------------


def _report_out(request: Request, principal: Principal, rep: dict[str, Any]) -> dict[str, Any]:
    st = request.app.state
    onto, cfg = st.ontologies[principal.tenant_id], st.traces[principal.tenant_id]
    rep["lines"] = [{"group": g, "text": t} for g, t in report_lines(onto, cfg, rep)]
    return rep


@router.get("/objects/{object_type}/{object_id}/trace/report")
async def get_trace_report(
    object_type: str,
    object_id: str,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    depth: Annotated[int, Query(ge=1, le=5)] = 3,
) -> dict[str, Any]:
    st = request.app.state
    onto = st.ontologies[principal.tenant_id]
    if object_type not in onto.objects:
        raise HTTPException(404, detail=f"未知对象类型：{object_type}")
    perm = f"{onto.objects[object_type].domain}.{object_type}:read"
    if not principal.can(perm):
        raise HTTPException(403, detail=f"需要权限 {perm}")
    if await session.get(ObjectState, (principal.tenant_id, object_type, object_id)) is None:
        raise HTTPException(404, detail="对象不存在")
    cfg = st.traces[principal.tenant_id]
    rep = await trace_report(session, onto, cfg, principal, object_type, object_id, depth=depth)
    return _report_out(request, principal, rep)


@router.get("/trace")
async def search_trace(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=64)],
) -> dict[str, Any]:
    st = request.app.state
    onto, cfg = st.ontologies[principal.tenant_id], st.traces[principal.tenant_id]
    hits = await find_root(session, onto, cfg, principal, q)
    if not hits:
        raise HTTPException(404, detail=f"没有找到“{q}”")
    if len(hits) > 1:
        return {
            "matches": [{"object_type": h.object_type, "object_id": h.object_id} for h in hits[:20]],
            "report": None,
        }
    h = hits[0]
    rep = await trace_report(session, onto, cfg, principal, h.object_type, h.object_id)
    return {
        "matches": [{"object_type": h.object_type, "object_id": h.object_id}],
        "report": _report_out(request, principal, rep),
    }
