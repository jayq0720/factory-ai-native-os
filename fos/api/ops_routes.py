"""全厂统一 Watchdog 巡检与 SaaS 运营后台 API（W28）。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request

from fos.api.deps import SessionDep, require
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.ops import service as ops
from fos.ops import watchdog as wd

router = APIRouter(prefix="/v1/ops", tags=["ops"])

OpsAdmin = Annotated[Principal, Depends(require("platform.ops:admin"))]


def _today(request: Request, tid: str) -> date:
    t = request.app.state.registry.get(tid)
    tz = ZoneInfo(t.timezone if t else "Asia/Shanghai")
    return utcnow().astimezone(tz).date()


@router.get("/health")
async def get_tenant_health(request: Request, session: SessionDep, p: OpsAdmin) -> dict[str, Any]:
    return await ops.tenant_health_snapshot(
        session, request.app.state.services, p.tenant_id, _today(request, p.tenant_id)
    )


@router.get("/security")
async def get_security_baseline(request: Request, p: OpsAdmin) -> dict[str, Any]:
    return ops.security_baseline(request.app.state.services, p.tenant_id)


@router.post("/watchdog/run")
async def run_watchdog_ep(
    request: Request, session: SessionDep, p: OpsAdmin, mode: Literal["real", "mock"] = "real"
) -> dict[str, Any]:
    if mode == "mock" and request.app.state.settings.env == "prod":
        raise HTTPException(403, detail="生产环境不允许写入模拟数据")
    return await wd.run_tenant_watchdog(
        session, request.app.state.services, p.tenant_id, _today(request, p.tenant_id), mode=mode
    )


@router.get("/tenants")
async def get_saas_overview(request: Request, session: SessionDep, p: OpsAdmin) -> dict[str, Any]:
    if "*.*:*" not in p.permissions:
        raise HTTPException(403, detail="仅平台超级管理员可查看多租户 SaaS 运营总览")
    return await ops.saas_overview(session, request.app.state.services, _today(request, p.tenant_id))


@router.get("/replication/{tenant_id}")
async def check_replication(tenant_id: str, request: Request, p: OpsAdmin) -> dict[str, Any]:
    if tenant_id != p.tenant_id and "*.*:*" not in p.permissions:
        raise HTTPException(403, detail="无权校验其他租户配置")
    st = request.app.state.settings
    try:
        return ops.verify_tenant_replication(
            packs_dir=st.packs_dir, tenants_dir=st.tenants_dir, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(404, detail=str(e)) from e
