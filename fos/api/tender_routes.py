"""招标拆解：招标文件 → 招标项目 → 需求矩阵 / 偏离表 / 评分点 / 资料清单 → 转工程项目 → 提交投标（方案 4.3）。

- 登记、修改、提交、结果都是账本事件（Ledger.append）；提交投标受 policy.yaml 审批规则与红线约束。
- 拆解结果（L1）存在 tender_analyses，每次重新拆解生成新版本，人工处理结果按条款带过去。
- 模拟数据（模拟 OCR、演示资料库）只能演示：不能提交投标、不能导出正式文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from fos.api.deps import SessionDep, require
from fos.db.models import Event, ObjectState, TenderAnalysis, Todo, utcnow
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, ApprovalRequired, Conflict, Evidence, Forbidden, Invalid, NotFound
from fos.tender import service as ts
from fos.tender.config import TenderConfig
from fos.tender.export import to_xlsx

router = APIRouter(prefix="/v1")

Reader = Annotated[Principal, Depends(require("sales.tender:read"))]
Writer = Annotated[Principal, Depends(require("sales.tender:write"))]
CredReader = Annotated[Principal, Depends(require("kb.credential:read"))]
_LEDGER_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUS_NAME = {
    "open": "投标准备中",
    "submitted": "已投标",
    "won": "中标",
    "lost": "未中标",
    "abandoned": "放弃",
}


def _cfg(request: Request, principal: Principal) -> TenderConfig:
    cfg = request.app.state.services.tenders.get(principal.tenant_id)
    if cfg is None:
        raise HTTPException(404, detail="本租户没有启用招标拆解（行业包缺少 tender.yaml）")
    return cfg


def _tz(request: Request, principal: Principal) -> ZoneInfo:
    t = request.app.state.registry.get(principal.tenant_id)
    return ZoneInfo(t.timezone if t else "Asia/Shanghai")


def _err(e: Exception) -> HTTPException:
    if isinstance(e, ts.TenderError):
        return HTTPException(e.status, detail=str(e))
    return HTTPException(_LEDGER_STATUS[type(e)], detail=str(e))


def _today(tz: ZoneInfo):
    return datetime.now(tz).date()


async def _analysis_view(
    session, a: TenderAnalysis, tender: ObjectState, cfg: TenderConfig
) -> dict[str, Any]:
    await ts.reopen_if_returned(session, a)
    out = ts.analysis_out(a)
    if tender.state.get("analysis") == a.id:
        out["status"] = "accepted"  # 已以这个版本提交投标
    out["stale"] = a.tender_version != tender.version
    out["risks"] = ts.risks(a, tender, cfg, utcnow())
    return out


def _aware(dt: datetime | None) -> str | None:
    """SQLite 读回来不带时区（存的是 UTC）。"""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


# ---------------- 招标项目 ----------------


@router.get("/tenders")
async def list_tenders(request: Request, session: SessionDep, principal: Reader) -> dict[str, Any]:
    _cfg(request, principal)
    rows = await session.scalars(
        select(ObjectState)
        .where(ObjectState.tenant_id == principal.tenant_id, ObjectState.object_type == "tender")
        .order_by(ObjectState.updated_seq.desc())
        .limit(200)
    )
    now = utcnow()
    items = []
    for o in rows:
        a = await ts.latest(session, principal.tenant_id, o.object_id)
        items.append(
            {
                **ts.tender_out(o, now=now),
                "status_name": STATUS_NAME.get(o.state.get("status", ""), ""),
                "analysis": ts.analysis_out(a, full=False) if a else None,
            }
        )
    items.sort(key=lambda x: (x.get("status") != "open", x.get("bid_deadline") or ""))
    return {"items": items}


class TenderIn(BaseModel):
    document_id: str
    owner: str | None = Field(default=None, max_length=64)  # 负责人登录名，默认登记人
    overrides: dict[str, Any] = Field(default_factory=dict)  # 文件里缺的信息可在登记时补充


@router.post("/tenders", status_code=201)
async def create_tender(body: TenderIn, request: Request, session: SessionDep, principal: Writer) -> Any:
    """登记招标项目（截止时间生成提醒待办），并立即做第一次拆解。"""
    cfg, tz = _cfg(request, principal), _tz(request, principal)
    try:
        res = await ts.register(
            session,
            principal,
            request.app.state.ledger,
            body.document_id,
            cfg,
            tz,
            owner=body.owner,
            overrides=body.overrides,
        )
        o = await ts.get_tender(session, principal.tenant_id, res.object_id)
        a = await ts.latest(session, principal.tenant_id, o.object_id)
        if a is None:
            a = await ts.analyze(session, principal, o.object_id, cfg, today=_today(tz))
    except (ts.TenderError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    out = {
        "tender": ts.tender_out(o, now=utcnow()),
        "analysis": await _analysis_view(session, a, o, cfg),
        "event_id": res.event_id,
        "duplicate": res.duplicate,
    }
    return JSONResponse(out, status_code=200 if res.duplicate else 201)


@router.get("/tenders/{tender_id}")
async def get_tender(
    tender_id: str, request: Request, session: SessionDep, principal: Reader
) -> dict[str, Any]:
    cfg = _cfg(request, principal)
    try:
        o = await ts.get_tender(session, principal.tenant_id, tender_id)
    except ts.TenderError as e:
        raise _err(e) from e
    a = await ts.latest(session, principal.tenant_id, tender_id)
    events = await session.scalars(
        select(Event)
        .where(
            Event.tenant_id == principal.tenant_id,
            Event.object_type == "tender",
            Event.object_id == tender_id,
        )
        .order_by(Event.seq)
    )
    todos = await session.scalars(
        select(Todo).where(
            Todo.tenant_id == principal.tenant_id, Todo.object_type == "tender", Todo.object_id == tender_id
        )
    )
    return {
        "tender": {
            **ts.tender_out(o, now=utcnow()),
            "status_name": STATUS_NAME.get(o.state.get("status", ""), ""),
        },
        "analysis": await _analysis_view(session, a, o, cfg) if a else None,
        "reminders": [
            {"id": t.id, "title": t.title, "due_at": _aware(t.due_at), "status": t.status} for t in todos
        ],
        "history": [
            {
                "seq": e.seq,
                "type": e.type,
                "actor": e.actor_name,
                "at": e.recorded_at.isoformat(),
                "payload": e.payload,
                "evidence": e.evidence,
            }
            for e in events
        ],
    }


@router.patch("/tenders/{tender_id}")
async def update_tender(
    tender_id: str, body: dict[str, Any], request: Request, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    """改关键信息（发了澄清 / 补遗、人工纠正）。截止时间改了会重新生成提醒；拆解结果需要重新拆解。"""
    cfg = _cfg(request, principal)
    note = str(body.pop("note", "") or "")[:200]
    body.pop("eng_project", None)  # 只能通过“转工程项目”设置
    try:
        o = await ts.get_tender(session, principal.tenant_id, tender_id)
        if o.state.get("status") != "open":
            raise ts.TenderConflict("招标项目已经提交或结束")
        res = await request.app.state.ledger.append(
            session,
            principal,
            AppendRequest(
                type="sales.tender_updated",
                object_id=tender_id,
                payload=body,
                evidence=[
                    Evidence(kind="manual", ref=f"tender:{tender_id}", note=note or "人工修改招标信息")
                ],
                correlation_id=f"tender:{tender_id}"[:64],
                data_mode=o.data_mode,
                expected_version=o.version,
            ),
        )
    except ApprovalRequired as e:
        raise HTTPException(409, detail=str(e)) from e
    except (ts.TenderError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    await session.refresh(o)
    if {"bid_deadline", "clarification_deadline", "owner"} & set(body):
        await ts.deadline_todos(session, principal, o, cfg)
    return {"tender": ts.tender_out(o, now=utcnow()), "version": res.version}


@router.post("/tenders/{tender_id}/analyze")
async def analyze(tender_id: str, request: Request, session: SessionDep, principal: Writer) -> dict[str, Any]:
    """重新拆解（补充了文件、资料库或知识库变了）：生成新版本，人工处理结果按条款保留。"""
    cfg, tz = _cfg(request, principal), _tz(request, principal)
    try:
        a = await ts.analyze(session, principal, tender_id, cfg, today=_today(tz))
        o = await ts.get_tender(session, principal.tenant_id, tender_id)
    except ts.TenderError as e:
        raise _err(e) from e
    return await _analysis_view(session, a, o, cfg)


@router.post("/tenders/{tender_id}/eng-project")
async def to_eng_project(tender_id: str, request: Request, session: SessionDep, principal: Writer) -> Any:
    """技术规格 → 工程项目（应用 A：选材、传热、BOM、报价）。需要同时有工程项目写权限。"""
    _cfg(request, principal)
    if not principal.can("eng.project:write"):
        raise HTTPException(403, detail="需要权限 eng.project:write")
    try:
        pid, created = await ts.to_eng_project(session, principal, request.app.state.ledger, tender_id)
    except (ts.TenderError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    return JSONResponse({"eng_project": pid, "created": created}, status_code=201 if created else 200)


class CloseIn(BaseModel):
    status: Literal["won", "lost", "abandoned"]
    note: str = Field(default="", max_length=500)


@router.post("/tenders/{tender_id}/close")
async def close(
    tender_id: str, body: CloseIn, request: Request, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    """投标结果：中标 / 未中标 / 放弃（放弃可以在提交前）。中标后按工程报价登记合同（W14）。"""
    _cfg(request, principal)
    try:
        o = await ts.get_tender(session, principal.tenant_id, tender_id)
        cur = o.state.get("status")
        if body.status in ("won", "lost") and cur != "submitted":
            raise ts.TenderConflict("还没有提交投标")
        if cur not in ("open", "submitted"):
            raise ts.TenderConflict("招标项目已经结束")
        await request.app.state.ledger.append(
            session,
            principal,
            AppendRequest(
                type="sales.tender_closed",
                object_id=tender_id,
                payload={"status": body.status, "result_note": body.note},
                correlation_id=f"tender:{tender_id}"[:64],
                data_mode=o.data_mode,
                expected_version=o.version,
            ),
        )
    except ApprovalRequired as e:
        raise HTTPException(409, detail=str(e)) from e
    except (ts.TenderError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    if body.status == "abandoned":
        for t in await session.scalars(
            select(Todo).where(
                Todo.tenant_id == principal.tenant_id,
                Todo.object_type == "tender",
                Todo.object_id == tender_id,
                Todo.status == "open",
            )
        ):
            t.status, t.closed_at, t.note = "dismissed", utcnow(), "已放弃投标"
    await session.refresh(o)
    return {"tender": ts.tender_out(o, now=utcnow())}


# ---------------- 拆解结果 ----------------


async def _load(session, principal: Principal, analysis_id: str) -> tuple[TenderAnalysis, ObjectState]:
    try:
        a = await ts.get_analysis(session, principal.tenant_id, analysis_id)
        o = await ts.get_tender(session, principal.tenant_id, a.tender_id)
    except ts.TenderError as e:
        raise _err(e) from e
    return a, o


@router.get("/tender-analyses/{analysis_id}")
async def get_analysis(
    analysis_id: str, request: Request, session: SessionDep, principal: Reader
) -> dict[str, Any]:
    cfg = _cfg(request, principal)
    a, o = await _load(session, principal, analysis_id)
    return await _analysis_view(session, a, o, cfg)


class ItemIn(BaseModel):
    status: Literal["open", "responded", "accepted", "na"] | None = None
    response: str | None = Field(default=None, max_length=1000)
    assignee: str | None = Field(default=None, max_length=64)
    deviation: Literal["无偏离", "正偏离", "负偏离", "待确认"] | None = None
    note: str | None = Field(default=None, max_length=300)


def _can_edit(principal: Principal) -> None:
    if not (principal.can("sales.tender:write") or principal.can("eng.project:write")):
        raise HTTPException(403, detail="需要权限 sales.tender:write 或 eng.project:write")


@router.patch("/tender-analyses/{analysis_id}/items/{item_id}")
async def update_item(
    analysis_id: str, item_id: str, body: ItemIn, request: Request, session: SessionDep, principal: Reader
) -> dict[str, Any]:
    """逐条处理需求：响应、负责人、状态；技术人员可以人工改偏离结论（留痕）。"""
    _cfg(request, principal)
    _can_edit(principal)
    a, _ = await _load(session, principal, analysis_id)
    await ts.reopen_if_returned(session, a)
    try:
        it = ts.update_item(a, principal, item_id, body.model_dump(exclude_none=True))
    except ts.TenderError as e:
        raise _err(e) from e
    return {"item": it, "summary": ts.summary(a)}


class DocIn(BaseModel):
    disposition: Literal["prepared", "na"] | None = None
    note: str = Field(default="", max_length=200)


@router.patch("/tender-analyses/{analysis_id}/documents/{doc_id}")
async def update_document(
    analysis_id: str, doc_id: str, body: DocIn, request: Request, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    _cfg(request, principal)
    a, _ = await _load(session, principal, analysis_id)
    await ts.reopen_if_returned(session, a)
    try:
        d = ts.update_document(a, principal, doc_id, body.disposition, body.note)
    except ts.TenderError as e:
        raise _err(e) from e
    return {"document": d, "summary": ts.summary(a)}


class SubmitIn(BaseModel):
    bid_amount: float | None = None


@router.post("/tender-analyses/{analysis_id}/submit", responses={202: {"description": "已提交审批"}})
async def submit(
    analysis_id: str, request: Request, session: SessionDep, principal: Writer, body: SubmitIn | None = None
) -> Any:
    """提交投标：★/▲/必须条款处理完、资料齐 → sales.tender_submitted。命中审批规则时提交审批单（202）。"""
    from fos.api.channel_routes import lookup_for
    from fos.api.inbox_routes import approval_out
    from fos.policy.approvals import send_notices

    _cfg(request, principal)
    a, o = await _load(session, principal, analysis_id)
    await ts.reopen_if_returned(session, a)
    if a.status == "submitted":
        raise HTTPException(409, detail="投标已经提交审批")
    try:
        req = ts.submit_request(a, o, body.bid_amount if body else None, utcnow())
        res = await request.app.state.ledger.append(session, principal, req)
    except ApprovalRequired as e:
        lookup = lookup_for(request)
        sub = await request.app.state.approvals.submit(
            session, principal, req, e.decision, source=f"tender_analysis:{a.id}", lookup=lookup
        )
        a.status, a.approval_id = "submitted", sub.approval.id
        await session.commit()
        await send_notices(lookup, principal.tenant_id, sub.notices)
        return JSONResponse(
            {"status": "pending_approval", "message": sub.message(), "approval": approval_out(sub.approval)},
            status_code=202,
        )
    except (ts.TenderError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    a.status = "submitted"
    return {"status": "submitted", "event_id": res.event_id}


@router.get("/tender-analyses/{analysis_id}/export")
async def export(
    analysis_id: str,
    request: Request,
    session: SessionDep,
    principal: Reader,
    format: Literal["xlsx", "json"] = Query("xlsx"),
) -> Response:
    cfg = _cfg(request, principal)
    a, o = await _load(session, principal, analysis_id)
    view = await _analysis_view(session, a, o, cfg)
    if format == "json":
        return JSONResponse(view)
    if a.data_mode == "mock":
        raise HTTPException(409, detail="模拟数据不能导出正式文件")
    name = f"招标拆解-{o.state.get('tender_no') or o.object_id}-v{a.version}.xlsx"
    return Response(
        to_xlsx(o.state, view),
        media_type=XLSX,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ---------------- 资质资料库 ----------------


@router.get("/credentials")
async def list_credentials(request: Request, session: SessionDep, principal: CredReader) -> dict[str, Any]:
    """资质与资料（登记 / 更新走 POST /v1/events：kb.credential_registered / kb.credential_updated）。"""
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == principal.tenant_id, ObjectState.object_type == "credential"
        )
    )
    today = _today(_tz(request, principal)).isoformat()
    items = []
    for c in rows:
        vu = c.state.get("valid_until")
        items.append(
            {"id": c.object_id, **c.state, "data_mode": c.data_mode, "expired": bool(vu and vu < today)}
        )
    items.sort(key=lambda x: (x.get("kind") or "", x.get("valid_until") or "9999"))
    return {"items": items}
