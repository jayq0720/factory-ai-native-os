"""窑炉监控（方案 3.2）：fos-edge 上行读数、人工抄表、窑况、窑次曲线、结束窑次、能耗、告警。

- 读数只追加到时序表，不走账本；测点 / 窑炉 / 窑次是本体对象（主数据用 /v1/events 或 Excel 导入）；
- 结束窑次按读数算最高温度、超差、能耗，以操作人身份入账；
- 模拟数据（演示读数）算出来的结果带 data_mode=mock。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from fos.api.deps import SessionDep, require
from fos.db.models import KilnAlarm, ObjectState, PointReading, utcnow
from fos.governance.principal import Principal
from fos.ledger import ApprovalRequired, Conflict, Forbidden, Invalid, NotFound
from fos.monitoring import samples as demo
from fos.monitoring import service as ms
from fos.monitoring.config import MonitorConfig

router = APIRouter(prefix="/v1")

Reader = Annotated[Principal, Depends(require("production.kiln:read"))]
Writer = Annotated[Principal, Depends(require("production.kiln:write"))]
Recorder = Annotated[Principal, Depends(require("production.reading:write"))]
Admin = Annotated[Principal, Depends(require("production.monitor_point:write"))]
_LEDGER_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, Invalid: 422, ApprovalRequired: 409}


def _cfg(request: Request, principal: Principal) -> MonitorConfig:
    cfg = request.app.state.services.monitoring.get(principal.tenant_id)
    if cfg is None:
        raise HTTPException(404, detail="本租户没有启用窑炉监控（本体里没有测点 monitor_point）")
    return cfg


def _err(e: Exception) -> HTTPException:
    if isinstance(e, ms.MonitorError):
        return HTTPException(e.status, detail=str(e))
    return HTTPException(_LEDGER_STATUS[type(e)], detail=str(e))


_ERRORS = (ms.MonitorError, Forbidden, NotFound, Conflict, Invalid, ApprovalRequired)


@router.post("/edge/uplink")
async def uplink(batch: ms.UplinkIn, request: Request, session: SessionDep, p: Recorder) -> dict[str, Any]:
    """fos-edge 上行一批读数（crates/fos-protocol 的 UplinkBatch）。重复的 (edge_id, seq) 返回 duplicate。"""
    _cfg(request, p)
    try:
        return await ms.ingest(session, p, batch)
    except _ERRORS as e:
        raise _err(e) from e


class ManualIn(BaseModel):
    point: str = Field(min_length=1, max_length=64)  # 测点编号 / 名称
    value: float
    ts: datetime | None = None
    evidence: str | None = Field(default=None, max_length=512)  # 仪表照片的对象键
    confidence: float | None = Field(default=None, ge=0, le=1)
    quality: Literal["good", "estimated"] = "good"


@router.post("/kiln/readings", status_code=201)
async def manual_reading(
    body: ManualIn, request: Request, session: SessionDep, p: Recorder
) -> dict[str, Any]:
    """路径 C：人工抄表 / 照片读数（数值由抄表人确认后提交）。"""
    _cfg(request, p)
    try:
        pt, row = await ms.record_manual(
            session,
            p,
            body.point,
            body.value,
            ts=body.ts,
            evidence=body.evidence,
            confidence=body.confidence,
            quality=body.quality,
        )
    except _ERRORS as e:
        raise _err(e) from e
    return {"point": pt.code, "name": pt.name, **ms.reading_out(row)}


@router.get("/kiln/board")
async def board(request: Request, session: SessionDep, p: Reader) -> dict[str, Any]:
    return await ms.board(session, p.tenant_id, _cfg(request, p))


@router.get("/kiln/points")
async def points(request: Request, session: SessionDep, p: Reader) -> dict[str, Any]:
    _cfg(request, p)
    rows = await ms.objects(session, p.tenant_id, "monitor_point")
    return {
        "points": [
            {"id": o.object_id, **o.state, "data_mode": o.data_mode}
            for o in sorted(rows, key=lambda o: o.state.get("code", ""))
        ]
    }


@router.get("/kiln/points/{code}/readings")
async def point_readings(
    code: str,
    request: Request,
    session: SessionDep,
    p: Reader,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 2000,
) -> dict[str, Any]:
    _cfg(request, p)
    end = ms.aware(end) if end else utcnow()
    start = ms.aware(start) if start else end - timedelta(hours=24)
    q = (
        select(PointReading)
        .where(
            PointReading.tenant_id == p.tenant_id,
            PointReading.point == code,
            PointReading.ts >= start,
            PointReading.ts <= end,
        )
        .order_by(PointReading.ts)
        .limit(limit)
    )
    rows = list(await session.scalars(q))
    return {"point": code, "readings": [ms.reading_out(r) for r in rows]}


@router.get("/kiln/runs/{run_id}/curve")
async def run_curve(run_id: str, request: Request, session: SessionDep, p: Reader) -> dict[str, Any]:
    try:
        return await ms.run_curve(session, p.tenant_id, _cfg(request, p), run_id)
    except _ERRORS as e:
        raise _err(e) from e


class FinishIn(BaseModel):
    ended_at: datetime | None = None
    status: Literal["finished", "aborted"] = "finished"
    note: str | None = Field(default=None, max_length=500)


@router.post("/kiln/runs/{run_id}/finish")
async def finish(
    run_id: str, body: FinishIn, request: Request, session: SessionDep, p: Writer
) -> dict[str, Any]:
    """结束窑次：按读数算最高温度、超差时长、能耗，一起入账。"""
    svc = request.app.state.services
    try:
        return await ms.finish_run(
            session,
            svc.ledger,
            p,
            _cfg(request, p),
            run_id,
            ended_at=body.ended_at,
            status=body.status,
            note=body.note,
        )
    except _ERRORS as e:
        raise _err(e) from e


@router.get("/kiln/energy")
async def energy(
    request: Request,
    session: SessionDep,
    p: Reader,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    _cfg(request, p)
    end = ms.aware(end) if end else utcnow()
    start = ms.aware(start) if start else end - timedelta(days=1)
    if start >= end:
        raise HTTPException(422, detail="开始时间必须早于结束时间")
    return await ms.energy(session, p.tenant_id, start, end)


@router.get("/kiln/alarms")
async def alarms(
    request: Request,
    session: SessionDep,
    p: Reader,
    status: Literal["open", "closed", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    _cfg(request, p)
    q = select(KilnAlarm).where(KilnAlarm.tenant_id == p.tenant_id)
    if status != "all":
        q = q.where(KilnAlarm.status == status)
    rows = await session.scalars(q.order_by(KilnAlarm.started_at.desc()).limit(limit))
    return {"alarms": [ms.alarm_out(a) for a in rows]}


@router.post("/kiln/alarms/{alarm_id}/ack")
async def ack(alarm_id: str, request: Request, session: SessionDep, p: Writer) -> dict[str, Any]:
    """已知晓：停止重复提醒（条件消失后告警仍会自动关闭）。"""
    _cfg(request, p)
    try:
        return ms.alarm_out(await ms.ack_alarm(session, p, alarm_id))
    except _ERRORS as e:
        raise _err(e) from e


@router.post("/kiln/demo", status_code=201)
async def load_demo(request: Request, session: SessionDep, p: Admin) -> dict[str, Any]:
    """写入演示窑炉、测点、在烧窑次和最近 30 小时读数（模拟数据）。已经有演示窑炉时不重复写。"""
    cfg = _cfg(request, p)
    svc = request.app.state.services
    if await session.get(ObjectState, (p.tenant_id, "kiln", demo.KILN)) is not None:
        return {"created": False, "kiln": demo.KILN, "run": demo.RUN}
    now = utcnow()
    try:
        for req in demo.ledger_requests(now):
            await svc.ledger.append(session, p, req)
    except _ERRORS as e:
        raise _err(e) from e
    rows = demo.readings(p.tenant_id, cfg, now - timedelta(hours=demo.HOURS), now)
    session.add_all(rows)
    await session.flush()
    return {"created": True, "kiln": demo.KILN, "run": demo.RUN, "readings": len(rows) - 1}
