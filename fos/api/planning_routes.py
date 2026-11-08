"""计划与排窑接口（W16，说明见 docs/planning.md）。

- POST /v1/planning/draft       排窑建议（求解器，不入账）：计划窑次、订单预计可发货、窑炉利用率、原料缺口
- GET  /v1/planning/plan        当前确认的排窑计划
- POST /v1/planning/plans       确认排窑计划（可以改过再提交；全有或全无，逐行过账本守卫）；有缺料推给采购
- POST /v1/planning/evaluate    交期评估：已有订单或假设的新订单放进去重排，能不能按期、会推迟谁、缺什么料
- GET  /v1/planning/materials   按当前计划算的物料需求和缺口

求解和计算都是确定性代码；确认以调用人身份 Ledger.append()。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from fos.api.deps import SessionDep, require
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.ledger import Conflict, Forbidden, Invalid, NotFound
from fos.planning.demand import load_context
from fos.planning.materials import shortage_lines
from fos.planning.notify import dm, role_identities, send_texts
from fos.planning.service import (
    CandidateIn,
    PlanIn,
    PlanningError,
    confirm_plan,
    current_plan,
    draft_plan,
    evaluate,
)

router = APIRouter(prefix="/v1/planning")
_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422}

PlanReader = Annotated[Principal, Depends(require("pmc.kiln_plan:read"))]
PlanWriter = Annotated[Principal, Depends(require("pmc.plan:write"))]
Mode = Annotated[Literal["real", "mock"], Query()]


def _env(request: Request, principal: Principal):
    st = request.app.state
    tid = principal.tenant_id
    cfg = st.planning.get(tid)
    if cfg is None:
        raise HTTPException(404, detail="这个租户没有开计划排窑模块")
    tz = ZoneInfo(st.registry.get(tid).timezone)
    return cfg, st.orders[tid], tz


def _err(e: Exception) -> HTTPException:
    if isinstance(e, PlanningError):
        return HTTPException(e.status, detail=str(e))
    return HTTPException(_STATUS[type(e)], detail=str(e))


async def _ctx(request: Request, session, principal: Principal, mode: str):
    cfg, ocfg, tz = _env(request, principal)
    return await load_context(
        session, tenant=principal.tenant_id, cfg=cfg, ocfg=ocfg, tz=tz, now=utcnow(), mode=mode
    )


@router.post("/draft")
async def draft(request: Request, session: SessionDep, principal: PlanReader, mode: Mode = "real") -> Any:
    return await draft_plan(session, await _ctx(request, session, principal, mode))


@router.get("/plan")
async def plan(request: Request, session: SessionDep, principal: PlanReader, mode: Mode = "real") -> Any:
    return await current_plan(session, await _ctx(request, session, principal, mode))


@router.get("/materials")
async def materials(request: Request, session: SessionDep, principal: PlanReader, mode: Mode = "real") -> Any:
    p = await current_plan(session, await _ctx(request, session, principal, mode))
    if p is None:
        raise HTTPException(404, detail="还没有确认的排窑计划")
    return {"plan": p["plan"], **p["materials"]}


@router.post("/plans", status_code=201)
async def confirm(
    body: PlanIn, request: Request, session: SessionDep, principal: PlanWriter
) -> dict[str, Any]:
    cfg, ocfg, tz = _env(request, principal)
    try:
        out = await confirm_plan(
            session, request.app.state.ledger, principal, body, cfg=cfg, ocfg=ocfg, tz=tz
        )
    except (PlanningError, Forbidden, NotFound, Conflict, Invalid) as e:
        raise _err(e) from e
    p = await current_plan(
        session,
        await load_context(
            session, tenant=principal.tenant_id, cfg=cfg, ocfg=ocfg, tz=tz, now=utcnow(), mode=body.data_mode
        ),
    )
    assert p is not None
    mp = p["materials"]
    out["shortages"] = mp["shortages"]
    out["orders"] = p["orders"]
    texts = []
    if mp["shortages"] and body.data_mode == "real":
        msg = f"排窑计划 {out['plan_no']} 已确认，以下原料不够：\n" + "\n".join(shortage_lines(mp))
        msg += f"\n（{mp['note']}）"
        texts = [
            (dm(i), msg)
            for _, i in await role_identities(session, principal.tenant_id, cfg.shortage_notify_roles)
        ]
    out["notified"] = len(texts)
    await session.commit()  # 先提交再推送
    if texts:
        from fos.api.channel_routes import lookup_for

        await send_texts(lookup_for(request), principal.tenant_id, texts)
    return out


class EvaluateIn(BaseModel):
    order: str | None = Field(default=None, max_length=64)
    candidate: CandidateIn | None = None
    data_mode: Literal["real", "mock"] = "real"

    @model_validator(mode="after")
    def _one(self):
        if bool(self.order) == bool(self.candidate):
            raise ValueError("order 和 candidate 二选一")
        return self


@router.post("/evaluate")
async def evaluate_route(
    body: EvaluateIn, request: Request, session: SessionDep, principal: PlanReader
) -> dict[str, Any]:
    cfg, ocfg, tz = _env(request, principal)
    try:
        return await evaluate(
            session,
            tenant=principal.tenant_id,
            cfg=cfg,
            ocfg=ocfg,
            tz=tz,
            now=utcnow(),
            mode=body.data_mode,
            order_id=body.order,
            candidate=body.candidate,
        )
    except PlanningError as e:
        raise _err(e) from e
