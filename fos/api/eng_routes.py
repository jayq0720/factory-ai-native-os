"""工程：技术文件 → 工程项目 → 选材匹配 → 方案 / BOM / 报价 → 审核 → 发布（方案 4.2、4.7）。

- 建项目、改工况、发布报价都是账本事件（Ledger.append）；发布报价受 policy.yaml 审批规则约束。
- 方案草稿（L1）存在 eng_designs，每次修改生成新版本；数字全部由 fos/engineering 确定性计算。
- 模拟数据（模拟 OCR、演示知识库）只能演示：不能发布、不能导出正式文件。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from fos.api.deps import SessionDep, require
from fos.db.models import Approval, EngDesign, Event, ObjectState
from fos.engineering import service as es
from fos.engineering.config import EngineeringConfig
from fos.engineering.export import proposal_sections, to_docx, to_xlsx
from fos.engineering.heat import HeatError, Layer, solve
from fos.engineering.kb import load_kb
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, ApprovalRequired, Conflict, Forbidden, Invalid, NotFound

router = APIRouter(prefix="/v1")

Reader = Annotated[Principal, Depends(require("eng.project:read"))]
Writer = Annotated[Principal, Depends(require("eng.project:write"))]
Quoter = Annotated[Principal, Depends(require("eng.quote:write"))]
KbReader = Annotated[Principal, Depends(require("kb.product:read"))]
_LEDGER_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}
STAGES = {"matching": "待匹配", "editing": "方案编辑中", "reviewing": "待审批", "quoted": "已发布报价"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _cfg(request: Request, principal: Principal) -> EngineeringConfig:
    cfg = request.app.state.services.engineering.get(principal.tenant_id)
    if cfg is None:
        raise HTTPException(404, detail="本租户没有启用工程模块（行业包缺少衬里模板）")
    return cfg


def _err(e: Exception) -> HTTPException:
    if isinstance(e, es.EngError):
        return HTTPException(e.status, detail=str(e))
    return HTTPException(_LEDGER_STATUS[type(e)], detail=str(e))


async def _designs(
    session, tenant_id: str, project_id: str, *, all_versions: bool = False
) -> list[EngDesign]:
    q = select(EngDesign).where(EngDesign.tenant_id == tenant_id, EngDesign.project_id == project_id)
    if not all_versions:
        q = q.where(EngDesign.status != "superseded")
    return list(await session.scalars(q.order_by(EngDesign.label, EngDesign.version)))


async def _stage(session, project: ObjectState, designs: list[EngDesign]) -> str:
    if project.state.get("status") == "quoted":
        return "quoted"
    for d in designs:
        if d.status == "submitted" and d.approval_id:
            ap = await session.get(Approval, d.approval_id)
            if ap is not None and ap.status == "pending":
                return "reviewing"
    return "editing" if designs else "matching"


def _project_out(o: ObjectState) -> dict[str, Any]:
    out = {
        "id": o.object_id,
        **o.state,
        "data_mode": o.data_mode,
        "version": o.version,
        "updated_at": o.updated_at.isoformat(),
    }
    if o.data_mode == "mock":
        out["notice"] = "模拟数据，非真实识别结果"
    return out


# ---------------- 工程项目 ----------------


@router.get("/eng/projects")
async def list_projects(session: SessionDep, principal: Reader, request: Request) -> dict[str, Any]:
    _cfg(request, principal)
    rows = await session.scalars(
        select(ObjectState)
        .where(ObjectState.tenant_id == principal.tenant_id, ObjectState.object_type == "eng_project")
        .order_by(ObjectState.updated_seq.desc())
        .limit(200)
    )
    items = []
    for o in rows:
        ds = await _designs(session, principal.tenant_id, o.object_id)
        stage = await _stage(session, o, ds)
        items.append({**_project_out(o), "stage": stage, "stage_name": STAGES[stage], "designs": len(ds)})
    return {"items": items}


class ProjectIn(BaseModel):
    document_id: str
    overrides: dict[str, Any] = Field(default_factory=dict)  # 文件里缺的工况可在建项目时补充


@router.post("/eng/projects", status_code=201)
async def create_project(body: ProjectIn, request: Request, session: SessionDep, principal: Writer) -> Any:
    _cfg(request, principal)
    try:
        res = await es.create_project(
            session, principal, request.app.state.ledger, body.document_id, body.overrides
        )
    except (es.EngError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    o = await es.get_project(session, principal.tenant_id, res.object_id)
    out = {"project": _project_out(o), "event_id": res.event_id, "duplicate": res.duplicate}
    return JSONResponse(out, status_code=200 if res.duplicate else 201)


@router.get("/eng/projects/{project_id}")
async def get_project(
    project_id: str, request: Request, session: SessionDep, principal: Reader
) -> dict[str, Any]:
    _cfg(request, principal)
    try:
        o = await es.get_project(session, principal.tenant_id, project_id)
    except es.EngError as e:
        raise _err(e) from e
    ds = await _designs(session, principal.tenant_id, project_id)
    stage = await _stage(session, o, ds)
    events = await session.scalars(
        select(Event)
        .where(
            Event.tenant_id == principal.tenant_id,
            Event.object_type == "eng_project",
            Event.object_id == project_id,
        )
        .order_by(Event.seq)
    )
    return {
        "project": _project_out(o),
        "stage": stage,
        "stage_name": STAGES[stage],
        "designs": [es.design_out(d, o, full=False) for d in ds],
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


@router.patch("/eng/projects/{project_id}")
async def update_project(
    project_id: str, body: dict[str, Any], request: Request, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    """改工况（客户补充了信息、人工纠正）。已有方案会标“工况已变”，需要重新计算。"""
    _cfg(request, principal)
    try:
        o = await es.get_project(session, principal.tenant_id, project_id)
        note = str(body.pop("note", "") or "")[:200]
        res = await request.app.state.ledger.append(
            session,
            principal,
            AppendRequest(
                type="eng.project_updated",
                object_id=project_id,
                payload=body,
                data_mode=o.data_mode,
                evidence=[
                    {"kind": "manual", "ref": f"eng_project:{project_id}", "note": note or "人工修改工况"}
                ],
                expected_version=o.version,
            ),
        )
    except ApprovalRequired as e:
        raise HTTPException(409, detail=str(e)) from e
    except (es.EngError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    o = await es.get_project(session, principal.tenant_id, project_id)
    return {"project": _project_out(o), "version": res.version}


@router.post("/eng/projects/{project_id}/match")
async def match(project_id: str, request: Request, session: SessionDep, principal: Writer) -> dict[str, Any]:
    cfg = _cfg(request, principal)
    try:
        r = await es.match_project(session, principal, project_id, cfg)
    except es.EngError as e:
        raise _err(e) from e
    o = await es.get_project(session, principal.tenant_id, project_id)
    return {
        "templates": r["templates"],
        "tried": r["tried"],
        "feasible": r["feasible"],
        "rejected": r["rejected"],
        "designs": [es.design_out(d, o, full=False) for d in r["designs"]],
    }


@router.get("/eng/projects/{project_id}/designs")
async def list_designs(
    project_id: str, session: SessionDep, principal: Reader, all: bool = False
) -> dict[str, Any]:
    o = await session.get(ObjectState, (principal.tenant_id, "eng_project", project_id))
    ds = await _designs(session, principal.tenant_id, project_id, all_versions=all)
    return {"items": [es.design_out(d, o, full=False) for d in ds]}


# ---------------- 方案 ----------------


async def _load(session, principal: Principal, design_id: str) -> tuple[EngDesign, ObjectState]:
    try:
        d = await es.get_design(session, principal.tenant_id, design_id)
        o = await es.get_project(session, principal.tenant_id, d.project_id)
    except es.EngError as e:
        raise _err(e) from e
    return d, o


@router.get("/eng/designs/{design_id}")
async def get_design(design_id: str, session: SessionDep, principal: Reader) -> dict[str, Any]:
    d, o = await _load(session, principal, design_id)
    out = es.design_out(d, o)
    out["proposal"] = [
        {"title": t, "blocks": blocks} for t, blocks in proposal_sections(o.state, d.data, d.label)
    ]
    return out


class LayerIn(BaseModel):
    role: str = Field(default="", max_length=16)
    product_id: str = Field(min_length=1, max_length=64)
    thickness_mm: float = Field(ge=0, le=2000)


class DesignPatch(BaseModel):
    layers: list[LayerIn] | None = Field(default=None, min_length=1, max_length=8)
    options: dict[str, float] | None = None
    note: str = Field(default="", max_length=500)


@router.patch("/eng/designs/{design_id}")
async def revise(
    design_id: str, body: DesignPatch, request: Request, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    """改厚度 / 换产品 / 改报价选项，或空提交（工况变了之后重算）。生成新版本。"""
    cfg = _cfg(request, principal)
    try:
        nd = await es.revise_design(
            session,
            principal,
            design_id,
            cfg,
            layers=[x.model_dump() for x in body.layers] if body.layers else None,
            options=body.options,
            note=body.note,
        )
    except es.EngError as e:
        raise _err(e) from e
    o = await es.get_project(session, principal.tenant_id, nd.project_id)
    return es.design_out(nd, o)


class DisposeIn(BaseModel):
    disposition: Literal["resolved", "accepted", "na"] | None
    note: str = Field(default="", max_length=200)


@router.patch("/eng/designs/{design_id}/review/{item_id}")
async def dispose(
    design_id: str, item_id: str, body: DisposeIn, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    d, _o = await _load(session, principal, design_id)
    await es.reopen_if_returned(session, d)
    try:
        it = es.dispose(d, principal, item_id, body.disposition, body.note)
    except es.EngError as e:
        raise _err(e) from e
    from fos.engineering.review import summary

    return {"item": it, "review_summary": summary(d.review)}


class PublishIn(BaseModel):
    quote_no: str | None = Field(default=None, max_length=64)


@router.post("/eng/designs/{design_id}/publish", responses={202: {"description": "已提交审批"}})
async def publish(
    design_id: str, request: Request, session: SessionDep, principal: Quoter, body: PublishIn | None = None
) -> Any:
    """发布报价：审核清单全部处理完 → eng.quote_issued 事件。命中审批规则时提交审批单（202）。"""
    from fos.api.channel_routes import lookup_for
    from fos.api.inbox_routes import approval_out
    from fos.policy.approvals import send_notices

    d, o = await _load(session, principal, design_id)
    await es.reopen_if_returned(session, d)  # 驳回 / 撤回 / 过期后可以重新提交
    if d.status == "submitted":
        raise HTTPException(409, detail="这个方案已经提交审批")
    try:
        req = es.publish_request(d, o, body.quote_no if body else None)
        res = await request.app.state.ledger.append(session, principal, req)
    except ApprovalRequired as e:
        lookup = lookup_for(request)
        sub = await request.app.state.approvals.submit(
            session, principal, req, e.decision, source=f"eng_design:{d.id}", lookup=lookup
        )
        d.status, d.approval_id = "submitted", sub.approval.id
        await session.commit()
        await send_notices(lookup, principal.tenant_id, sub.notices)
        return JSONResponse(
            {"status": "pending_approval", "message": sub.message(), "approval": approval_out(sub.approval)},
            status_code=202,
        )
    except (es.EngError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    d.status = "published"
    return {"status": "published", "event_id": res.event_id, "quote_no": req.payload["quote_no"]}


@router.get("/eng/designs/{design_id}/export")
async def export(
    design_id: str,
    session: SessionDep,
    principal: Reader,
    format: Literal["xlsx", "docx", "json"] = Query("xlsx"),
) -> Response:
    d, o = await _load(session, principal, design_id)
    if format == "json":
        return JSONResponse(es.design_out(d, o))
    if d.data_mode == "mock":
        raise HTTPException(409, detail="模拟数据不能导出正式文件")
    base = f"{o.state.get('name', o.object_id)}-{d.label}-v{d.version}"
    if format == "docx":
        data, mime, name = to_docx(o.state, d.data, d.label, d.version), DOCX, f"{base}-技术方案.docx"
    else:
        data, mime, name = (
            to_xlsx(o.state, d.data, d.review, d.label, d.version),
            XLSX,
            f"{base}-BOM报价.xlsx",
        )
    return Response(
        data, media_type=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
    )


# ---------------- 计算与知识库 ----------------


class CalcLayer(BaseModel):
    name: str = Field(default="", max_length=32)
    thickness_mm: float = Field(gt=0, le=2000)
    tc_a: float = Field(gt=0, le=50)
    tc_b: float = Field(default=0.0, ge=-0.01, le=0.01)


class CalcIn(BaseModel):
    layers: list[CalcLayer] = Field(min_length=1, max_length=8)
    hot_temp: float = Field(gt=0, le=2500)
    ambient: float = Field(default=25, ge=-40, le=60)
    geometry: Literal["flat", "cylinder"] = "flat"
    inner_radius_m: float | None = Field(default=None, gt=0, le=50)
    emissivity: float = Field(default=0.8, gt=0, le=1)


@router.post("/eng/calc")
async def calc(body: CalcIn, principal: Reader) -> dict[str, Any]:
    """无状态传热计算（供 Agent 工具和网页试算）。"""
    try:
        r = solve(
            [
                Layer(x.name or f"第{i + 1}层", x.thickness_mm, x.tc_a, x.tc_b)
                for i, x in enumerate(body.layers)
            ],
            body.hot_temp,
            body.ambient,
            emissivity=body.emissivity,
            geometry=body.geometry,
            inner_radius_m=body.inner_radius_m,
        )
    except HeatError as e:
        raise HTTPException(422, detail=str(e)) from e
    return r.as_dict()


@router.get("/kb/products")
async def kb_products(request: Request, session: SessionDep, principal: KbReader) -> dict[str, Any]:
    kb = await load_kb(session, principal.tenant_id)
    return {"items": [p.as_dict() for p in sorted(kb.values(), key=lambda p: p.code)]}


@router.get("/kb/linings")
async def kb_linings(request: Request, principal: KbReader) -> dict[str, Any]:
    return _cfg(request, principal).model_dump()
