"""Policy、审批单、待办与统一收件箱（W8，方案 5.1 原则 5–6、5.2）。

收件箱是少数几个固定界面之一：所有职能“需要我处理的事”都汇总在这里——
- 等我审批的审批单（我是当前这一级的审批人）；
- 等我确认的卡片（L2：Agent 提议的记账，我有对应权限，或卡片指定由我确认）；
- 指派给我（或我的角色）的待办。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from fos.api.channel_routes import lookup_for
from fos.api.deps import PrincipalDep, SessionDep, require
from fos.channels.cards import Card
from fos.db.models import Approval, CardRecord, Todo, User, utcnow
from fos.governance.principal import Principal
from fos.ledger.service import AppendRequest
from fos.policy.approvals import (
    STATUS_TEXT,
    ApprovalService,
    Notice,
    dm_ref,
    identities,
    is_open,
    payload_fields,
    send_notices,
)
from fos.triggers.cards import parse_action

router = APIRouter(prefix="/v1")

PolicyReader = Annotated[Principal, Depends(require("platform.policy:read"))]
ApprovalAuditor = Annotated[Principal, Depends(require("platform.approval:read"))]
TodoWriter = Annotated[Principal, Depends(require("platform.todo:write"))]

_FAR = datetime.max.replace(tzinfo=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def approval_out(ap: Approval) -> dict[str, Any]:
    status = ap.status
    if status == "pending" and not is_open(ap):
        status = "expired"
    return {
        "id": ap.id,
        "title": ap.title,
        "status": status,
        "status_text": STATUS_TEXT.get(status, status),
        "rules": ap.rules,
        "steps": ap.steps,
        "current_step": ap.current_step,
        "decisions": ap.decisions,
        "requester": {"id": ap.requester_id, "name": ap.requester_name},
        "source": ap.source,
        "request": ap.request,
        "data_mode": ap.data_mode,
        "result": ap.result,
        "created_at": _iso(ap.created_at),
        "expires_at": _iso(ap.expires_at),
        "decided_at": _iso(ap.decided_at),
    }


def _todo_out(t: Todo) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body,
        "assignee_user_id": t.assignee_user_id,
        "assignee_role": t.assignee_role,
        "object": {"type": t.object_type, "id": t.object_id} if t.object_type else None,
        "due_at": _iso(t.due_at),
        "status": t.status,
        "source": t.source,
        "data_mode": t.data_mode,
        "created_by": t.created_by,
        "closed_by": t.closed_by,
        "closed_at": _iso(t.closed_at),
        "note": t.note,
        "created_at": _iso(t.created_at),
    }


def _svc(request: Request) -> ApprovalService:
    return request.app.state.approvals


# ---------------- Policy ----------------


@router.get("/policy")
async def get_policy(request: Request, principal: PolicyReader) -> dict[str, Any]:
    return request.app.state.policies[principal.tenant_id].describe()


# ---------------- 收件箱 ----------------


@router.get("/inbox")
async def inbox(
    request: Request, session: SessionDep, principal: PrincipalDep, box: Literal["todo", "sent"] = "todo"
) -> dict[str, Any]:
    """box=todo：需要我处理的事；box=sent：我提交的审批单。"""
    tid = principal.tenant_id
    if box == "sent":
        rows = (
            await session.scalars(
                select(Approval)
                .where(Approval.tenant_id == tid, Approval.requester_id == principal.subject_id)
                .order_by(Approval.created_at.desc())
                .limit(100)
            )
        ).all()
        return {"box": "sent", "items": [{"kind": "approval", **approval_out(a)} for a in rows]}

    svc = _svc(request)
    onto = request.app.state.ontologies[tid]
    me = await session.get(User, principal.subject_id)
    login = me.login if me else None
    items: list[dict[str, Any]] = []

    pending = (
        await session.scalars(
            select(Approval)
            .where(Approval.tenant_id == tid, Approval.status == "pending")
            .order_by(Approval.created_at)
            .limit(500)
        )
    ).all()
    for ap in pending:
        if not is_open(ap) or not svc._eligible(ap, principal, login):
            continue
        step = ap.steps[ap.current_step]
        level = f"第 {ap.current_step + 1}/{len(ap.steps)} 级「{step['name']}」"
        req = AppendRequest.model_validate(ap.request)
        items.append(
            {
                "kind": "approval",
                "id": ap.id,
                "title": ap.title,
                "summary": f"{ap.requester_name} 提交，{level}",
                "fields": [f.model_dump() for f in payload_fields(onto, req, 6)],
                "from": ap.requester_name,
                "deadline": _iso(ap.expires_at),
                "created_at": _iso(ap.created_at),
                "data_mode": ap.data_mode,
                "actions": ["approve", "reject"],
            }
        )

    cards = (
        await session.scalars(
            select(CardRecord)
            .where(CardRecord.tenant_id == tid, CardRecord.status == "open")
            .order_by(CardRecord.created_at.desc())
            .limit(300)
        )
    ).all()
    now = utcnow()
    for c in cards:
        if (_aware(c.expires_at) or now) <= now or not _card_for_me(c, principal, onto):
            continue
        card = Card.model_validate(c.card)
        items.append(
            {
                "kind": "card",
                "id": c.id,
                "title": card.title,
                "summary": card.text,
                "fields": [f.model_dump() for f in card.fields[:6]],
                "from": c.created_by,
                "short_code": c.short_code,
                "deadline": _iso(c.expires_at),
                "created_at": _iso(c.created_at),
                "data_mode": card.data_mode,
                "actions": [b.id for b in card.buttons],
            }
        )

    todos = (
        await session.scalars(
            select(Todo)
            .where(
                Todo.tenant_id == tid,
                Todo.status == "open",
                or_(
                    Todo.assignee_user_id == principal.subject_id,
                    Todo.assignee_role.in_(list(principal.roles) or [""]),
                ),
            )
            .limit(300)
        )
    ).all()
    for t in todos:
        items.append(
            {
                "kind": "todo",
                "id": t.id,
                "title": t.title,
                "summary": t.body,
                "object": {"type": t.object_type, "id": t.object_id} if t.object_type else None,
                "from": t.source,
                "deadline": _iso(t.due_at),
                "created_at": _iso(t.created_at),
                "data_mode": t.data_mode,
                "actions": ["done", "dismiss"],
            }
        )

    # 有截止时间的排前面（越早越靠前），其余按创建时间
    items.sort(
        key=lambda i: (datetime.fromisoformat(i["deadline"]) if i["deadline"] else _FAR, i["created_at"])
    )
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in ("approval", "card", "todo")}
    return {"box": "todo", "counts": counts, "items": items}


def _card_for_me(c: CardRecord, principal: Principal, onto) -> bool:
    try:
        action = parse_action(c.action)
    except ValueError:
        return False
    if action is not None and action.kind == "approval.decide":
        return False  # 审批卡片：审批单本身已在收件箱里
    if c.allowed_user_id:
        return c.allowed_user_id == principal.subject_id
    if c.required_permission and not principal.can(c.required_permission):
        return False
    if action is not None:
        ev = onto.events.get(action.request.type)
        return ev is not None and principal.can(ev.permission)
    return bool(c.required_permission)


# ---------------- 审批单 ----------------


async def _load(session, principal: Principal, approval_id: str) -> Approval:
    ap = await session.get(Approval, approval_id)
    if ap is None or ap.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="审批单不存在")
    return ap


@router.get("/approvals")
async def list_approvals(
    session: SessionDep, principal: ApprovalAuditor, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    q = select(Approval).where(Approval.tenant_id == principal.tenant_id)
    if status:
        q = q.where(Approval.status == status)
    rows = (
        await session.scalars(q.order_by(Approval.created_at.desc()).limit(min(max(limit, 1), 200)))
    ).all()
    return {"items": [approval_out(a) for a in rows]}


@router.get("/approvals/{approval_id}")
async def get_approval(
    approval_id: str, request: Request, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    """申请人、审批过的人、当前这一级的审批人和审计员可以看。"""
    ap = await _load(session, principal, approval_id)
    visible = (
        ap.requester_id == principal.subject_id
        or any(d["user_id"] == principal.subject_id for d in ap.decisions)
        or principal.can("platform.approval:read")
        or await _svc(request).eligible(session, principal, ap)
    )
    if not visible:
        raise HTTPException(404, detail="审批单不存在")
    out = approval_out(ap)
    out["approvers"] = [u.display_name for u in await _svc(request).approvers(session, ap)]
    return out


class DecideIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=200)


_DECIDE_HTTP = {"not_eligible": 403, "closed": 409}


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: str, body: DecideIn, request: Request, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    ap = await _load(session, principal, approval_id)
    lookup = lookup_for(request)
    r = await _svc(request).decide(
        session, principal, ap, body.decision, comment=body.comment, via="api", lookup=lookup
    )
    if r.status in _DECIDE_HTTP:
        await session.commit()  # 过期标记要保存
        raise HTTPException(_DECIDE_HTTP[r.status], detail=r.message)
    await session.commit()
    errors = await send_notices(lookup, principal.tenant_id, r.notices)
    return {"outcome": r.status, "message": r.message, "approval": approval_out(ap), "send_errors": errors}


@router.post("/approvals/{approval_id}/cancel")
async def cancel_approval(
    approval_id: str, request: Request, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    ap = await _load(session, principal, approval_id)
    r = await _svc(request).cancel(session, principal, ap)
    if r.status in _DECIDE_HTTP:
        raise HTTPException(_DECIDE_HTTP[r.status], detail=r.message)
    return {"outcome": r.status, "message": r.message, "approval": approval_out(ap)}


# ---------------- 待办 ----------------


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(default="", max_length=2000)
    assignee_login: str | None = None
    assignee_role: str | None = None
    object_type: str | None = None
    object_id: str | None = Field(default=None, max_length=64)
    due_at: datetime | None = None
    data_mode: Literal["real", "mock"] = "real"


@router.post("/todos", status_code=201)
async def create_todo(
    body: TodoIn, request: Request, session: SessionDep, principal: TodoWriter
) -> dict[str, Any]:
    """指派一条待办给某个人或某个角色，并推送到被指派人的 IM。"""
    tid = principal.tenant_id
    if bool(body.assignee_login) == bool(body.assignee_role):
        raise HTTPException(422, detail="assignee_login 和 assignee_role 必须且只能给一个")
    if body.due_at is not None and body.due_at.tzinfo is None:
        raise HTTPException(422, detail="due_at 必须带时区")
    if (body.object_type is None) != (body.object_id is None):
        raise HTTPException(422, detail="object_type 和 object_id 要一起给")
    if body.object_type and body.object_type not in request.app.state.ontologies[tid].objects:
        raise HTTPException(422, detail=f"未知对象类型：{body.object_type}")
    users: list[User] = []
    assignee_user_id = None
    if body.assignee_login:
        u = (
            await session.scalars(
                select(User).where(User.tenant_id == tid, User.login == body.assignee_login)
            )
        ).one_or_none()
        if u is None:
            raise HTTPException(404, detail="被指派的用户不存在")
        users, assignee_user_id = [u], u.id
    else:
        tenant = request.app.state.registry.get(tid)
        if body.assignee_role not in tenant.roles:
            raise HTTPException(422, detail=f"角色 {body.assignee_role} 不存在")
        all_users = (
            await session.scalars(select(User).where(User.tenant_id == tid, User.is_active.is_(True)))
        ).all()
        users = [u for u in all_users if body.assignee_role in u.roles]
    t = Todo(
        tenant_id=tid,
        title=body.title,
        body=body.body,
        assignee_user_id=assignee_user_id,
        assignee_role=body.assignee_role,
        object_type=body.object_type,
        object_id=body.object_id,
        due_at=body.due_at,
        status="open",
        source="api",
        data_mode=body.data_mode,
        created_by=principal.subject_id,
        note="",
        created_at=utcnow(),
    )
    session.add(t)
    await session.flush()
    lookup = lookup_for(request)
    due = f"，截止 {body.due_at:%m-%d %H:%M}" if body.due_at else ""
    mock = "【模拟】" if body.data_mode == "mock" else ""
    notices: list[Notice] = []
    for u in users:
        for i in await identities(session, tid, u.id):
            if lookup(tid, i.channel) is not None:
                notices.append(
                    Notice(
                        dm_ref(i), text=f"{mock}新待办：{body.title}{due}（{principal.display_name} 指派）"
                    )
                )
    await session.commit()
    errors = await send_notices(lookup, tid, notices)
    return {**_todo_out(t), "notified": len(notices), "send_errors": errors}


class TodoCloseIn(BaseModel):
    status: Literal["done", "dismissed"] = "done"
    note: str = Field(default="", max_length=500)


@router.post("/todos/{todo_id}/close")
async def close_todo(
    todo_id: str, body: TodoCloseIn, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    """被指派人（或同角色的人）、创建人可以关闭。"""
    t = await session.get(Todo, todo_id)
    if t is None or t.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="待办不存在")
    mine = (
        t.assignee_user_id == principal.subject_id
        or (t.assignee_role is not None and t.assignee_role in principal.roles)
        or t.created_by == principal.subject_id
    )
    if not mine:
        raise HTTPException(403, detail="这条待办不是你的")
    if t.status != "open":
        raise HTTPException(409, detail="这条待办已经关闭")
    t.status, t.note, t.closed_by, t.closed_at = body.status, body.note, principal.subject_id, utcnow()
    return _todo_out(t)
