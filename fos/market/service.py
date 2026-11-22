"""市场商机线索与经营决策驾驶舱核心服务（W27c）。

- 市场商机漏斗：新建、跟进阶段更新、赢单/丢单结案（50 万及以上大额丢单触发销售负责人复盘确认）；
- 竞品行情情报：记录对手报价与区域动态；
- 经营决策驾驶舱：跨模块确定性汇总财务应收/应付、质量 NCR、设备工单、EHS 隐患、人力、研发、施工与商机漏斗。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.market.analytics import compute_cockpit_kpis, compute_funnel
from fos.market.config import MarketConfig
from fos.market.guards import (
    INTEL_LOG_EVENT,
    LEAD_CLOSE_EVENT,
    LEAD_CREATE_EVENT,
    LEAD_UPDATE_EVENT,
)
from fos.orders.data import get
from fos.orders.service import new_id

INDUSTRY_TEXT = {
    "steel": "钢铁",
    "cement": "水泥",
    "glass": "玻璃",
    "nonferrous": "有色金属",
    "chemical": "化工",
    "power": "电力/焚烧",
    "other": "其他",
}
SOURCE_TEXT = {
    "referral": "老客户转介",
    "exhibition": "行业展会",
    "tender_portal": "招投标平台",
    "cold_call": "主动拜访",
    "existing_customer": "老客户复购",
    "other": "其他",
}
STAGE_TEXT = {
    "new": "新建线索",
    "qualified": "需求确认",
    "quoted": "已报价",
    "negotiation": "商务谈判",
    "won": "赢单",
    "lost": "丢单",
}


class MarketError(ValueError):
    """市场与经营模块业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class MarketNotFound(MarketError):
    def __init__(self, msg: str):
        super().__init__(msg, 404)


async def _scan(session: AsyncSession, tenant: str, obj_type: str, mode: str = "real") -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == tenant,
            ObjectState.object_type == obj_type,
            ObjectState.data_mode == mode,
        )
    )
    return sorted(rows, key=lambda o: o.created_seq)


# ---------------- 市场商机线索 ----------------


class LeadIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=120)
    industry: Literal["steel", "cement", "glass", "nonferrous", "chemical", "power", "other"] = "steel"
    source: Literal["referral", "exhibition", "tender_portal", "cold_call", "existing_customer", "other"] = (
        "other"
    )
    product_interest: str = Field(min_length=1, max_length=120)
    est_amount: FiniteFloat = Field(ge=0.0)
    probability: int | None = Field(default=None, ge=0, le=100)
    owner_name: str = Field(min_length=1, max_length=64)
    stage: Literal["new", "qualified", "quoted", "negotiation"] = "new"
    expected_close_date: date
    note: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class LeadUpdateIn(BaseModel):
    est_amount: FiniteFloat | None = Field(default=None, ge=0.0)
    probability: int | None = Field(default=None, ge=0, le=100)
    stage: Literal["new", "qualified", "quoted", "negotiation"] | None = None
    expected_close_date: date | None = None
    note: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class LeadCloseIn(BaseModel):
    stage: Literal["won", "lost"]
    note: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _lead_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("stage") or "new"
    ind = s.get("industry") or "other"
    src = s.get("source") or "other"
    amt = float(s.get("est_amount") or 0.0)
    prob = int(s.get("probability") or 0)
    return {
        "id": o.object_id,
        "lead_no": s.get("lead_no") or o.object_id,
        "customer_name": s.get("customer_name") or "",
        "industry": ind,
        "industry_text": INDUSTRY_TEXT.get(ind, ind),
        "source": src,
        "source_text": SOURCE_TEXT.get(src, src),
        "product_interest": s.get("product_interest") or "",
        "est_amount": amt,
        "probability": prob,
        "weighted_amount": round(amt * prob / 100.0, 2),
        "owner_name": s.get("owner_name") or "",
        "stage": st,
        "stage_text": STAGE_TEXT.get(st, st),
        "expected_close_date": s.get("expected_close_date"),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def funnel_board(
    session: AsyncSession, tenant: str, *, stage: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "market_lead", mode)
    all_items = [_lead_out(o) for o in reversed(rows)]
    metrics = compute_funnel(all_items)
    items = [it for it in all_items if it["stage"] == stage] if stage else all_items
    return {"data_mode": mode, "metrics": metrics, "items": items}


async def create_lead(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    tz: ZoneInfo,
    cfg: MarketConfig,
    body: LeadIn,
) -> dict[str, Any]:
    lid = new_id("LD", tz)
    prob = body.probability if body.probability is not None else cfg.stage_probabilities.get(body.stage, 10)
    payload: dict[str, Any] = {
        "lead_no": lid,
        "customer_name": body.customer_name.strip(),
        "industry": body.industry,
        "source": body.source,
        "product_interest": body.product_interest.strip(),
        "est_amount": float(body.est_amount),
        "probability": int(prob),
        "owner_name": body.owner_name.strip(),
        "stage": body.stage,
        "expected_close_date": body.expected_close_date.isoformat(),
    }
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"lead:{lid}", note="新建市场商机线索")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=LEAD_CREATE_EVENT,
            object_id=lid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": lid, "event": res.model_dump()}


async def update_lead(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    cfg: MarketConfig,
    lead_id: str,
    body: LeadUpdateIn,
) -> AppendResult:
    o = await get(session, principal.tenant_id, "market_lead", lead_id)
    if o is None:
        raise MarketNotFound(f"市场商机 {lead_id} 不存在")
    payload = body.model_dump(exclude_none=True, exclude={"evidence"})
    if "expected_close_date" in payload:
        payload["expected_close_date"] = payload["expected_close_date"].isoformat()
    if "stage" in payload and "probability" not in payload:
        payload["probability"] = cfg.stage_probabilities.get(payload["stage"], 30)
    if not payload:
        raise MarketError("至少需要提供一个更新字段", 422)
    ev = body.evidence or [Evidence(kind="manual", ref=f"lead:{lead_id}:upd", note="更新商机阶段")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=LEAD_UPDATE_EVENT,
            object_id=lead_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def close_lead_request(
    session: AsyncSession, principal: Principal, lead_id: str, body: LeadCloseIn
) -> AppendRequest:
    """构造商机结案请求（50 万及以上丢单在路由层捕获 ApprovalRequired 转销售负责人复盘确认）。"""
    o = await get(session, principal.tenant_id, "market_lead", lead_id)
    if o is None:
        raise MarketNotFound(f"市场商机 {lead_id} 不存在")
    prob = 100 if body.stage == "won" else 0
    ev = body.evidence or [Evidence(kind="manual", ref=f"lead:{lead_id}:close", note="商机结案")]
    return AppendRequest(
        type=LEAD_CLOSE_EVENT,
        object_id=lead_id,
        payload={"stage": body.stage, "probability": prob, "note": body.note.strip()},
        evidence=ev,
        expected_version=o.version,
        data_mode=o.data_mode,
    )


# ---------------- 竞品行情情报 ----------------


class IntelIn(BaseModel):
    competitor: str = Field(min_length=1, max_length=120)
    product_category: str = Field(min_length=1, max_length=120)
    quoted_price: FiniteFloat | None = Field(default=None, ge=0.0)
    region: str | None = Field(default=None, max_length=64)
    recorded_date: date
    summary: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _intel_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    return {
        "id": o.object_id,
        "intel_no": s.get("intel_no") or o.object_id,
        "competitor": s.get("competitor") or "",
        "product_category": s.get("product_category") or "",
        "quoted_price": s.get("quoted_price"),
        "region": s.get("region"),
        "recorded_date": s.get("recorded_date"),
        "summary": s.get("summary") or "",
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def intel_board(session: AsyncSession, tenant: str, *, mode: str = "real") -> dict[str, Any]:
    rows = await _scan(session, tenant, "competitor_intel", mode)
    items = [_intel_out(o) for o in reversed(rows)]
    return {"data_mode": mode, "counts": {"total": len(items)}, "items": items}


async def log_intel(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: IntelIn
) -> dict[str, Any]:
    iid = new_id("INT", tz)
    payload: dict[str, Any] = {
        "intel_no": iid,
        "competitor": body.competitor.strip(),
        "product_category": body.product_category.strip(),
        "recorded_date": body.recorded_date.isoformat(),
        "summary": body.summary.strip(),
    }
    if body.quoted_price is not None:
        payload["quoted_price"] = float(body.quoted_price)
    if body.region:
        payload["region"] = body.region.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"intel:{iid}", note="竞品行情登记")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=INTEL_LOG_EVENT,
            object_id=iid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": iid, "event": res.model_dump()}


# ---------------- 经营决策驾驶舱 ----------------


async def executive_cockpit(
    session: AsyncSession, tenant: str, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    """跨模块确定性汇总工厂核心经营指标。"""
    funnel = (await funnel_board(session, tenant, mode=mode))["metrics"]
    types = (
        "receivable",
        "payable",
        "ncr",
        "work_order",
        "safety_hazard",
        "employee",
        "rnd_project",
        "formula_version",
        "site_project",
    )
    rows = (
        await session.scalars(
            select(ObjectState).where(
                ObjectState.tenant_id == tenant,
                ObjectState.object_type.in_(types),
                ObjectState.data_mode == mode,
            )
        )
    ).all()
    grouped: dict[str, list[dict[str, Any]]] = {t: [] for t in types}
    for o in rows:
        grouped[o.object_type].append(o.state)

    kpis = compute_cockpit_kpis(
        funnel=funnel,
        receivables=grouped["receivable"],
        payables=grouped["payable"],
        ncrs=grouped["ncr"],
        work_orders=grouped["work_order"],
        hazards=grouped["safety_hazard"],
        employees=grouped["employee"],
        rnd_projects=grouped["rnd_project"],
        formulas=grouped["formula_version"],
        sites=grouped["site_project"],
    )
    return {"date": today.isoformat(), "data_mode": mode, "kpis": kpis, "funnel": funnel}
