"""租户配置：核心 + 行业包 + 租户配置 中的第三层（方案第 8 章）。

租户配置是 git 里的 YAML 文件（tenants/<id>/tenant.yaml），不含任何密钥，
改动走 PR 评审、可回滚。运行时只读。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from fos.governance.permissions import is_valid_permission

IMKind = Literal["dingtalk", "wecom", "feishu"]
_TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class RoleDef(BaseModel):
    name: str  # 显示名，如“仓管员”
    permissions: list[str] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _check_perms(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not is_valid_permission(p)]
        if bad:
            raise ValueError(f"非法权限串: {bad}")
        return v


class TenantConfig(BaseModel):
    id: str
    name: str
    industry_pack: str  # 例如 "refractory"
    timezone: str = "Asia/Shanghai"
    im: list[IMKind] = Field(default_factory=list)
    roles: dict[str, RoleDef]
    # 自治级别上限：某类动作 Agent 最多能做到 L 几（方案 2.7）
    autonomy: dict[str, Literal["L0", "L1", "L2", "L3"]] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _TENANT_ID.match(v):
            raise ValueError("租户 id 只能是小写字母、数字和连字符，2–31 位，字母开头")
        return v

    @model_validator(mode="after")
    def _admin_role_exists(self) -> TenantConfig:
        if "admin" not in self.roles:
            raise ValueError("租户必须定义 admin 角色")
        return self

    def permissions_for(self, role_keys: list[str]) -> frozenset[str]:
        perms: set[str] = set()
        for key in role_keys:
            role = self.roles.get(key)
            if role:
                perms.update(role.permissions)
        return frozenset(perms)


class TenantRegistry:
    """从目录加载全部租户配置。目录结构：<dir>/<tenant_id>/tenant.yaml"""

    def __init__(self, tenants: dict[str, TenantConfig]):
        self._tenants = tenants

    @classmethod
    def load(cls, directory: Path) -> TenantRegistry:
        tenants: dict[str, TenantConfig] = {}
        for path in sorted(directory.glob("*/tenant.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            cfg = TenantConfig.model_validate(data)
            if cfg.id != path.parent.name:
                raise ValueError(f"{path}: id={cfg.id!r} 与目录名 {path.parent.name!r} 不一致")
            tenants[cfg.id] = cfg
        return cls(tenants)

    def get(self, tenant_id: str) -> TenantConfig | None:
        return self._tenants.get(tenant_id)

    def ids(self) -> list[str]:
        return sorted(self._tenants)
