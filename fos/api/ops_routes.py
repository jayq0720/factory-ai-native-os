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


@router.get("/budget")
async def get_tenant_budget(request: Request, session: SessionDep, p: OpsAdmin) -> dict[str, Any]:
    """查询当前租户当日与当月 LLM Token 预算租约状态（ok / warn / exceeded）。"""
    from fos.governance.budget import check_tenant_budget

    tenant = request.app.state.registry.get(p.tenant_id)
    if tenant is None:
        raise HTTPException(404, detail="租户不存在")
    return await check_tenant_budget(session, tenant)


@router.get("/keys")
async def list_tenant_keys(session: SessionDep, p: OpsAdmin, include_revoked: bool = False) -> dict[str, Any]:
    """列出当前租户 API Key 脱敏元数据（永不返回明文或哈希）。"""
    from fos.governance import apikeys

    items = await apikeys.list_keys(session, p.tenant_id, include_revoked=include_revoked)
    return {"tenant_id": p.tenant_id, "count": len(items), "items": items}


@router.post("/keys", status_code=201)
async def issue_tenant_key(body: dict[str, Any], session: SessionDep, p: OpsAdmin) -> dict[str, Any]:
    """为同租户活跃用户签发新 API Key（明文仅返回这一次）。"""
    from sqlalchemy import select

    from fos.db.models import User
    from fos.governance import apikeys

    login = str(body.get("login") or "").strip()
    label = str(body.get("label") or "").strip()[:64]
    edge_id = str(body.get("edge_id") or "").strip() or None
    if not login:
        raise HTTPException(422, detail="login 不能为空")
    u = (
        await session.execute(select(User).where(User.tenant_id == p.tenant_id, User.login == login))
    ).scalar_one_or_none()
    if u is None or not u.is_active:
        raise HTTPException(404, detail=f"同租户活跃用户不存在：{login}")
    issued = await apikeys.issue(session, u, label=label, edge_id=edge_id)
    return {
        "key_id": issued.key_id,
        "plaintext": issued.plaintext,
        "login": u.login,
        "label": label,
        "edge_id": edge_id,
    }


@router.post("/keys/{key_id}/revoke")
async def revoke_tenant_key(key_id: str, session: SessionDep, p: OpsAdmin) -> dict[str, Any]:
    """按租户隔离吊销指定 API Key（跨租户返回 404）。"""
    from fos.governance import apikeys

    k = await apikeys.revoke(session, p.tenant_id, key_id)
    if k is None:
        raise HTTPException(404, detail=f"未找到本租户密钥：{key_id}")
    return {"id": k.id, "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}


def _user_out(u: Any) -> dict[str, Any]:
    return {
        "id": u.id,
        "login": u.login,
        "display_name": u.display_name,
        "roles": list(u.roles or []),
        "is_active": bool(u.is_active),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/users")
async def list_tenant_users(session: SessionDep, p: OpsAdmin) -> dict[str, Any]:
    """列出当前租户全部员工账号与角色状态。"""
    from sqlalchemy import select

    from fos.db.models import User

    rows = (
        await session.scalars(select(User).where(User.tenant_id == p.tenant_id).order_by(User.login.asc()))
    ).all()
    items = [_user_out(u) for u in rows]
    return {"tenant_id": p.tenant_id, "count": len(items), "items": items}


@router.post("/users", status_code=201)
async def create_tenant_user(
    body: dict[str, Any], request: Request, session: SessionDep, p: OpsAdmin
) -> dict[str, Any]:
    """在线创建同租户员工账号，严格校验角色合法性与登录名唯一性。"""
    from sqlalchemy import select

    from fos.db.models import User

    tenant = request.app.state.registry.get(p.tenant_id)
    if tenant is None:
        raise HTTPException(404, detail="租户不存在")
    login = str(body.get("login") or "").strip()
    display_name = str(body.get("display_name") or "").strip()
    roles = [str(r).strip() for r in (body.get("roles") or []) if str(r).strip()]
    if not login or not display_name:
        raise HTTPException(422, detail="login 与 display_name 不能为空")
    unknown = [r for r in roles if r not in tenant.roles]
    if unknown:
        raise HTTPException(422, detail=f"未定义的角色：{unknown}")
    dup = (
        await session.execute(select(User).where(User.tenant_id == p.tenant_id, User.login == login))
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, detail=f"登录名已存在：{login}")
    u = User(tenant_id=p.tenant_id, login=login, display_name=display_name, roles=roles, im_ids={})
    session.add(u)
    await session.flush()
    return _user_out(u)


@router.patch("/users/{login}")
async def update_tenant_user(
    login: str, body: dict[str, Any], request: Request, session: SessionDep, p: OpsAdmin
) -> dict[str, Any]:
    """在线调整同租户员工角色、显示名或启停状态（防管理员误停用自身账号）。"""
    from sqlalchemy import select

    from fos.db.models import User

    tenant = request.app.state.registry.get(p.tenant_id)
    if tenant is None:
        raise HTTPException(404, detail="租户不存在")
    u = (
        await session.execute(select(User).where(User.tenant_id == p.tenant_id, User.login == login))
    ).scalar_one_or_none()
    if u is None:
        raise HTTPException(404, detail=f"本租户用户不存在：{login}")

    if "display_name" in body:
        dn = str(body["display_name"] or "").strip()
        if not dn:
            raise HTTPException(422, detail="display_name 不能为空")
        u.display_name = dn
    if "roles" in body:
        roles = [str(r).strip() for r in (body["roles"] or []) if str(r).strip()]
        unknown = [r for r in roles if r not in tenant.roles]
        if unknown:
            raise HTTPException(422, detail=f"未定义的角色：{unknown}")
        if u.id == p.subject_id and "admin" not in roles:
            raise HTTPException(400, detail="禁止移除当前管理员自身的 admin 角色")
        u.roles = roles
    if "is_active" in body:
        active = bool(body["is_active"])
        if u.id == p.subject_id and not active:
            raise HTTPException(400, detail="禁止停用当前登录的管理员账号")
        u.is_active = active
    await session.flush()
    return _user_out(u)
