"""操作主体：人或 Agent。所有写入事件都要记录主体（方案 5.1 原则 2）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fos.governance.permissions import grants


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject_id: str  # 用户 id 或 agent id
    kind: Literal["user", "agent", "system"]
    display_name: str
    roles: tuple[str, ...]
    permissions: frozenset[str]

    def can(self, permission: str) -> bool:
        return grants(self.permissions, permission)


def principal_for_user(user, tenant) -> Principal:
    """由用户记录和租户配置构造主体（API Key 鉴权与 IM 消息共用）。"""
    return Principal(
        tenant_id=user.tenant_id,
        subject_id=user.id,
        kind="user",
        display_name=user.display_name,
        roles=tuple(user.roles),
        permissions=tenant.permissions_for(list(user.roles)),
    )
