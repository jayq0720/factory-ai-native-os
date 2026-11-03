"""本体、事件与对象的 API。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from fos.api.deps import PrincipalDep, SessionDep
from fos.db.models import Event, ObjectState
from fos.governance.principal import Principal
from fos.ledger import (
    AppendRequest,
    AppendResult,
    ApprovalRequired,
    Conflict,
    Forbidden,
    Invalid,
    Ledger,
    NotFound,
)
from fos.ledger.trace import trace
from fos.ontology import Ontology

router = APIRouter(prefix="/v1")

_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}


def _onto(request: Request, principal: Principal) -> Ontology:
    return request.app.state.ontologies[principal.tenant_id]


def _read_perm(onto: Ontology, object_type: str) -> str:
    return f"{onto.objects[object_type].domain}.{object_type}:read"


def _require_read(onto: Ontology, principal: Principal, object_type: str) -> None:
    if object_type not in onto.objects:
        raise HTTPException(404, detail=f"未知对象类型：{object_type}")
    perm = _read_perm(onto, object_type)
    if not principal.can(perm):
        raise HTTPException(403, detail=f"需要权限 {perm}")


def _event_out(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "seq": e.seq,
        "type": e.type,
        "object_type": e.object_type,
        "object_id": e.object_id,
        "payload": e.payload,
        "evidence": e.evidence,
        "actor": {"kind": e.actor_kind, "id": e.actor_id, "name": e.actor_name},
        "data_mode": e.data_mode,
        "occurred_at": e.occurred_at.isoformat(),
        "recorded_at": e.recorded_at.isoformat(),
        "causation_id": e.causation_id,
        "correlation_id": e.correlation_id,
    }


def _object_out(o: ObjectState) -> dict[str, Any]:
    return {
        "object_type": o.object_type,
        "object_id": o.object_id,
        "state": o.state,
        "data_mode": o.data_mode,
        "version": o.version,
        "created_seq": o.created_seq,
        "updated_seq": o.updated_seq,
        "updated_at": o.updated_at.isoformat(),
    }


@router.get("/ontology")
async def get_ontology(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return _onto(request, principal).describe()


@router.post("/events", status_code=201, responses={202: {"description": "命中审批规则，已提交审批单"}})
async def append_event(
    body: AppendRequest, request: Request, response: Response, principal: PrincipalDep, session: SessionDep
) -> AppendResult:
    """追加事件。命中审批规则时不写入，改为提交审批单并返回 202（审批通过后自动记账）。"""
    from fos.api.channel_routes import lookup_for
    from fos.api.inbox_routes import approval_out
    from fos.policy.approvals import send_notices

    ledger: Ledger = request.app.state.ledger
    try:
        res = await ledger.append(session, principal, body)
    except ApprovalRequired as e:
        lookup = lookup_for(request)
        sub = await request.app.state.approvals.submit(
            session, principal, body, e.decision, source="api", lookup=lookup
        )
        await session.commit()
        await send_notices(lookup, principal.tenant_id, sub.notices)
        return JSONResponse(
            {"status": "pending_approval", "message": sub.message(), "approval": approval_out(sub.approval)},
            status_code=202,
        )
    except (Forbidden, NotFound, Conflict, Invalid) as e:
        raise HTTPException(_STATUS[type(e)], detail=str(e)) from e
    if res.duplicate:
        response.status_code = status.HTTP_200_OK
    return res


@router.get("/events")
async def list_events(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    type: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
) -> dict[str, Any]:
    """按 seq 顺序读事件，只返回调用者有读权限的对象类型上的事件。"""
    onto = _onto(request, principal)
    readable = [t for t in onto.objects if principal.can(_read_perm(onto, t))]
    if object_type is not None:
        _require_read(onto, principal, object_type)
        readable = [object_type]
    q = select(Event).where(
        Event.tenant_id == principal.tenant_id, Event.seq > after_seq, Event.object_type.in_(readable)
    )
    if type:
        q = q.where(Event.type == type)
    if object_id:
        q = q.where(Event.object_id == object_id)
    rows = (await session.scalars(q.order_by(Event.seq).limit(limit))).all()
    return {"events": [_event_out(e) for e in rows], "next_after_seq": rows[-1].seq if rows else after_seq}


@router.get("/objects/{object_type}")
async def list_objects(
    object_type: str,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    onto = _onto(request, principal)
    _require_read(onto, principal, object_type)
    rows = (
        await session.scalars(
            select(ObjectState)
            .where(ObjectState.tenant_id == principal.tenant_id, ObjectState.object_type == object_type)
            .order_by(ObjectState.updated_seq.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {"objects": [_object_out(o) for o in rows]}


@router.get("/objects/{object_type}/{object_id}")
async def get_object(
    object_type: str, object_id: str, request: Request, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    onto = _onto(request, principal)
    _require_read(onto, principal, object_type)
    obj = await session.get(ObjectState, (principal.tenant_id, object_type, object_id))
    if obj is None:
        raise HTTPException(404, detail="对象不存在")
    history = (
        await session.scalars(
            select(Event)
            .where(
                Event.tenant_id == principal.tenant_id,
                Event.object_type == object_type,
                Event.object_id == object_id,
            )
            .order_by(Event.seq)
        )
    ).all()
    return {**_object_out(obj), "history": [_event_out(e) for e in history]}


@router.get("/objects/{object_type}/{object_id}/trace")
async def trace_object(
    object_type: str,
    object_id: str,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    depth: Annotated[int, Query(ge=1, le=6)] = 4,
) -> dict[str, Any]:
    """链式追溯。调用者没有读权限的节点保留位置但隐去内容。"""
    onto = _onto(request, principal)
    _require_read(onto, principal, object_type)
    if await session.get(ObjectState, (principal.tenant_id, object_type, object_id)) is None:
        raise HTTPException(404, detail="对象不存在")
    out = await trace(session, principal.tenant_id, object_type, object_id, depth=depth)
    for n in out["nodes"]:
        t = n["object_type"]
        n["type_name"] = onto.objects[t].name if t in onto.objects else t
        if t not in onto.objects or not principal.can(_read_perm(onto, t)):
            n["state"] = None
            n["redacted"] = True
    return out
