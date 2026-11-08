"""仓储写操作：领料（指定批次或按先进先出自动分配）、退料、盘点（预览差异 → 确认调整）。

人确认后以确认人身份 Ledger.append()；守卫（guards.py）在账本层再查一次。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from fos.ledger import AppendRequest, Evidence
from fos.orders.config import OrdersConfig
from fos.orders.data import get
from fos.orders.service import new_id
from fos.supply.config import SupplyConfig
from fos.supply.guards import COUNT_EVENT, ISSUE_EVENT, RETURN_EVENT
from fos.supply.purchase import SupplyError, SupplyNotFound
from fos.supply.stock import EPS, fifo, lot_balance, lot_balances


class IssueIn(BaseModel):
    material: str | None = Field(default=None, max_length=64)  # 只填物料：按先进先出分配批次
    lot: str | None = Field(default=None, max_length=64)  # 指定批次
    qty: float = Field(gt=0)
    batch: str | None = Field(default=None, max_length=64)  # 领给哪个生产批次（追溯用）
    moved_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list)  # 领料单照片（可选）
    data_mode: Literal["real", "mock"] = "real"

    @model_validator(mode="after")
    def _one(self):
        if bool(self.material) == bool(self.lot):
            raise ValueError("material 和 lot 二选一")
        return self


def _move(event: str, lot, kind: str, qty: float, at: str, tz: ZoneInfo, **extra) -> AppendRequest:
    payload: dict[str, Any] = {
        "lot": lot.object_id,
        "material": lot.state.get("material"),
        "kind": kind,
        "qty": round(qty, 6),
        "unit": lot.state.get("unit"),
        "moved_at": at,
    }
    evidence = extra.pop("evidence", None) or []
    payload.update({k: v for k, v in extra.items() if v})
    return AppendRequest(
        type=event, object_id=new_id("MV", tz), payload=payload, evidence=evidence, data_mode=lot.data_mode
    )


async def issue_requests(
    session: AsyncSession, tenant: str, cfg: SupplyConfig, ocfg: OrdersConfig, body: IssueIn, tz: ZoneInfo
) -> tuple[list[AppendRequest], list[dict[str, Any]]]:
    at = (body.moved_at or datetime.now(tz)).isoformat()
    extra = {"batch": body.batch, "note": body.note, "evidence": body.evidence}
    if body.lot:
        lot = await get(session, tenant, "material_lot", body.lot)
        if lot is None:
            raise SupplyNotFound(f"原料批次 {body.lot} 不存在")
        req = _move(ISSUE_EVENT, lot, "issue", body.qty, at, tz, **extra)
        return [req], [{"lot": lot.object_id, "qty": body.qty}]
    m = await get(session, tenant, "material", body.material)
    if m is None:
        raise SupplyNotFound(f"物料 {body.material} 不存在")
    bals = [
        b
        for b in (
            await lot_balances(session, tenant, material_ids={m.object_id}, mode=body.data_mode)
        ).values()
        if b.status in cfg.stock.usable_statuses
        and b.on_hand > EPS
        and ocfg.same_unit(b.lot.state.get("unit"), m.state.get("unit"))
    ]
    left, reqs, alloc = body.qty, [], []
    for b in fifo(bals):
        if left <= EPS:
            break
        take = min(left, b.on_hand)
        reqs.append(_move(ISSUE_EVENT, b.lot, "issue", take, at, tz, **extra))
        alloc.append({"lot": b.lot.object_id, "qty": round(take, 6)})
        left -= take
    if left > EPS:
        have = sum(b.on_hand for b in bals)
        raise SupplyError(
            f"{m.state.get('name')} 可用现存 {have:g} {m.state.get('unit')}，不够领 {body.qty:g}"
            "（待检、不合格的批次不能领）"
        )
    return reqs, alloc


class ReturnIn(BaseModel):
    lot: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0)
    batch: str | None = Field(default=None, max_length=64)
    moved_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list)


async def return_request(session: AsyncSession, tenant: str, body: ReturnIn, tz: ZoneInfo) -> AppendRequest:
    lot = await get(session, tenant, "material_lot", body.lot)
    if lot is None:
        raise SupplyNotFound(f"原料批次 {body.lot} 不存在")
    at = (body.moved_at or datetime.now(tz)).isoformat()
    return _move(
        RETURN_EVENT,
        lot,
        "return",
        body.qty,
        at,
        tz,
        batch=body.batch,
        note=body.note,
        evidence=body.evidence,
    )


class CountLine(BaseModel):
    lot: str = Field(min_length=1, max_length=64)
    counted: float = Field(ge=0)


class CountIn(BaseModel):
    lines: list[CountLine] = Field(min_length=1, max_length=200)
    count_no: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list)  # 确认时必填：盘点照片 / 盘点表


async def count_preview(session: AsyncSession, tenant: str, body: CountIn) -> dict[str, Any]:
    rows = []
    for ln in body.lines:
        b = await lot_balance(session, tenant, ln.lot)
        if b is None:
            raise SupplyNotFound(f"原料批次 {ln.lot} 不存在")
        diff = round(ln.counted - b.on_hand, 6)
        rows.append(
            {
                "lot": ln.lot,
                "material": b.lot.state.get("material"),
                "unit": b.lot.state.get("unit"),
                "book": b.on_hand,
                "counted": ln.counted,
                "diff": diff,
                "kind": "count_gain" if diff > EPS else "count_loss" if diff < -EPS else None,
            }
        )
    return {
        "lines": rows,
        "differences": sum(1 for r in rows if r["kind"]),
        "note": "差异 = 盘点数 − 账面现存；确认后按差异记盘盈 / 盘亏（要附盘点照片）",
    }


async def count_requests(
    session: AsyncSession, tenant: str, body: CountIn, tz: ZoneInfo
) -> tuple[list[AppendRequest], dict[str, Any]]:
    if not body.evidence:
        raise SupplyError("盘点调整要附盘点照片或盘点表")
    pv = await count_preview(session, tenant, body)
    count_no = body.count_no or new_id("PD", tz)
    at = datetime.now(tz).isoformat()
    reqs = []
    for r in pv["lines"]:
        if not r["kind"]:
            continue
        lot = await get(session, tenant, "material_lot", r["lot"])
        reqs.append(
            _move(
                COUNT_EVENT,
                lot,
                r["kind"],
                abs(r["diff"]),
                at,
                tz,
                count_no=count_no,
                note=body.note,
                evidence=body.evidence,
            )
        )
    return reqs, {**pv, "count_no": count_no}
