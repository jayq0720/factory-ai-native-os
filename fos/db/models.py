"""数据库表。W1 只有身份相关的表；企业本体与事件账本在 W2–W3 加入。

约定：
- 每张业务表都有 tenant_id，所有查询必须按 tenant_id 过滤（由 repository 层保证）。
- 主键用字符串 UUID，跨库迁移和日志里都好认。
- 时间一律 UTC，带时区。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "login", name="uq_users_tenant_login"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    login: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    # IM 身份映射，例如 {"dingtalk": "manager1234"}；用于把 IM 消息落到人
    im_ids: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # 明文 key 里的 key id 部分
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    secret_sha256: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
