"""全厂统一 Watchdog 巡检与 SaaS 运营后台 API（W28）。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request

from fos.api.deps import SessionDep, require
from fos.db.models import utcnow
from fos.eval.runner import run_golden_eval
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


@router.get("/eval")
async def run_eval_ep(request: Request, p: OpsAdmin) -> dict[str, Any]:
    try:
        return run_golden_eval(request.app.state.services, p.tenant_id)
    except ValueError as e:
        raise HTTPException(404, detail=str(e)) from e


@router.get("/metrics")
async def get_prometheus_metrics(request: Request, session: SessionDep, p: OpsAdmin):
    """导出标准 Prometheus OpenMetrics 指标（需平台超级管理员权限）。"""
    from fastapi import Response

    from fos.ops.metrics import HttpMetrics, render_prometheus

    if "*.*:*" not in p.permissions:
        raise HTTPException(403, detail="仅平台超级管理员可抓取系统指标")
    ov = await ops.saas_overview(session, request.app.state.services, _today(request, p.tenant_id))
    hm = getattr(request.app.state, "http_metrics", None) or HttpMetrics()
    body = render_prometheus(hm, ov)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/audit")
async def get_audit_log(
    request: Request,
    session: SessionDep,
    p: OpsAdmin,
    event_prefix: str | None = None,
    object_type: str | None = None,
    actor_id: str | None = None,
    data_mode: Literal["real", "mock"] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """查询当前租户不可变事件账本审计流水（倒序分页）。"""
    from fos.ops.metrics import query_audit_log

    return await query_audit_log(
        session,
        p.tenant_id,
        event_prefix=event_prefix,
        object_type=object_type,
        actor_id=actor_id,
        data_mode=data_mode,
        limit=limit,
        offset=offset,
    )


@router.get("/audit.xlsx")
async def export_audit_xlsx(
    request: Request,
    session: SessionDep,
    p: OpsAdmin,
    event_prefix: str | None = None,
    object_type: str | None = None,
    actor_id: str | None = None,
    data_mode: Literal["real", "mock"] = "real",
    limit: int = 200,
):
    """导出合规审计流水 Excel（默认仅含真实数据，全部单元格防公式注入）。"""
    from urllib.parse import quote

    from fastapi import Response

    from fos.ops.backup import render_audit_xlsx
    from fos.ops.metrics import query_audit_log

    data = await query_audit_log(
        session,
        p.tenant_id,
        event_prefix=event_prefix,
        object_type=object_type,
        actor_id=actor_id,
        data_mode=data_mode,
        limit=limit,
        offset=0,
    )
    xlsx = render_audit_xlsx(data)
    today = _today(request, p.tenant_id).isoformat()
    fname = f"账本审计流水_{p.tenant_id}_{today}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.get("/backup.jsonl")
async def export_ledger_backup(
    request: Request,
    session: SessionDep,
    p: OpsAdmin,
    include_mock: bool = False,
):
    """下载当前租户防篡改 JSONL 事件账本灾备包（含 SHA-256 校验尾）。"""
    from urllib.parse import quote

    from fastapi import Response

    from fos.ops.backup import export_tenant_ledger_jsonl

    if "*.*:*" not in p.permissions:
        raise HTTPException(403, detail="仅平台超级管理员可下载全量账本灾备包")
    mode = None if include_mock else "real"
    content, summary = await export_tenant_ledger_jsonl(session, p.tenant_id, data_mode=mode)
    today = _today(request, p.tenant_id).isoformat()
    fname = f"ledger_backup_{p.tenant_id}_{today}.jsonl"
    return Response(
        content=content,
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}",
            "X-Ledger-Sha256": summary["sha256"],
            "X-Ledger-Count": str(summary["count"]),
        },
    )
