"""Web 工作台统一登录与会话路由（POST /v1/auth/login、/logout、GET /v1/auth/session）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from fos.api.deps import PrincipalDep, RegistryDep, SessionDep
from fos.governance import apikeys
from fos.governance.principal import principal_for_user
from fos.governance.session import COOKIE_NAME, resolve_secret, sign_session

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)


@router.post("/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: SessionDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    """校验 API Key 并签发 HttpOnly 会话 Cookie，免去在 16 个工作台重复粘贴密钥。"""
    found = await apikeys.verify(session, body.api_key.strip())
    if not found:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="API Key 无效或已吊销")
    key, user = found
    tenant = registry.get(user.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="租户配置不可用")
    settings = request.app.state.settings
    secret = resolve_secret(settings.session_secret)
    ttl_h = max(1, int(settings.session_ttl_hours))
    cookie_val = sign_session(
        user_id=user.id,
        tenant_id=user.tenant_id,
        key_id=key.id,
        secret=secret,
        ttl_hours=ttl_h,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie_val,
        max_age=ttl_h * 3600,
        httponly=True,
        samesite="lax",
        secure=(settings.env == "prod"),
        path="/",
    )
    pr = principal_for_user(user, tenant, edge_id=key.edge_id)
    return {
        "authenticated": True,
        "tenant_id": pr.tenant_id,
        "tenant_name": tenant.name,
        "subject_id": pr.subject_id,
        "display_name": pr.display_name,
        "roles": list(pr.roles),
        "ttl_hours": ttl_h,
    }


@router.post("/logout")
async def logout(response: Response) -> dict[str, Any]:
    """清除浏览器 HttpOnly 会话 Cookie。"""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"authenticated": False}


@router.get("/session")
async def session_info(principal: PrincipalDep, registry: RegistryDep) -> dict[str, Any]:
    """查询当前会话身份（同时支持 Bearer API Key 与 HttpOnly 会话 Cookie）。"""
    tenant = registry.get(principal.tenant_id)
    assert tenant is not None
    return {
        "authenticated": True,
        "tenant_id": principal.tenant_id,
        "tenant_name": tenant.name,
        "subject_id": principal.subject_id,
        "display_name": principal.display_name,
        "roles": list(principal.roles),
    }
