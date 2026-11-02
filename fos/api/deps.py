from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance import apikeys
from fos.governance.principal import Principal, principal_for_user
from fos.tenancy import TenantRegistry


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


def get_registry(request: Request) -> TenantRegistry:
    return request.app.state.registry


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[TenantRegistry, Depends(get_registry)]

_UNAUTH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="缺少或无效的 API Key",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_principal(request: Request, session: SessionDep, registry: RegistryDep) -> Principal:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTH
    found = await apikeys.verify(session, token.strip())
    if not found:
        raise _UNAUTH
    _, user = found
    tenant = registry.get(user.tenant_id)
    if tenant is None:  # 租户配置被移除：一律拒绝
        raise _UNAUTH
    return principal_for_user(user, tenant)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(permission: str) -> Callable[[Principal], Principal]:
    """路由依赖：要求主体拥有某个权限。用法：Depends(require("eng.project:write"))"""

    def _dep(principal: PrincipalDep) -> Principal:
        if not principal.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"需要权限 {permission}")
        return principal

    return _dep
