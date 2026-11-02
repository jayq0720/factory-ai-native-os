"""数据库表：身份（W1）、事件账本与投影（W2–W3）、文档解析（W4）。

约定：
- 每张业务表都有 tenant_id，所有查询必须按 tenant_id 过滤（由 repository 层保证）。
- 主键用字符串 UUID，跨库迁移和日志里都好认。
- 时间一律 UTC，带时区。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
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


# ---------------- 事件账本（只追加） ----------------


class TenantCounter(Base):
    """每个租户的事件序号。追加事件时行级加锁递增，保证租户内 seq 连续、无空洞。"""

    __tablename__ = "tenant_counters"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "seq", name="uq_events_tenant_seq"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_events_tenant_idem"),
        Index("ix_events_object", "tenant_id", "object_type", "object_id", "seq"),
        Index("ix_events_type", "tenant_id", "type", "seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(48))
    object_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    actor_kind: Mapped[str] = mapped_column(String(16))  # user / agent / system
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_name: Mapped[str] = mapped_column(String(64))
    data_mode: Mapped[str] = mapped_column(String(8), default="real")  # real / mock
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 业务发生时间
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)  # 记账时间
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 由哪个事件引起
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 同一件事（Thread）
    ontology_layers: Mapped[list] = mapped_column(JSON, default=list)  # 记账时的本体版本


# ---------------- 投影（可从事件重放重建） ----------------


class ObjectState(Base):
    __tablename__ = "objects"
    __table_args__ = (Index("ix_objects_type", "tenant_id", "object_type", "updated_seq"),)

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON)
    data_mode: Mapped[str] = mapped_column(String(8))
    version: Mapped[int] = mapped_column(Integer)  # 作用于该对象的事件数
    created_seq: Mapped[int] = mapped_column(BigInteger)
    updated_seq: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObjectLink(Base):
    """引用图：由对象的 ref 属性派生，用于链式召回（批次 → 原料 → 窑次 → 检验）。"""

    __tablename__ = "object_links"
    __table_args__ = (Index("ix_links_to", "tenant_id", "to_type", "to_id"),)

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    from_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    from_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rel: Mapped[str] = mapped_column(String(48), primary_key=True)
    to_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    to_id: Mapped[str] = mapped_column(String(64), primary_key=True)


# 数据库层强制只追加：任何 UPDATE / DELETE events 都会报错（重建投影不需要改事件）。
APPEND_ONLY_PG = [
    """
    CREATE OR REPLACE FUNCTION fos_events_append_only() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'events 表只允许追加（% 被拒绝）', TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION fos_events_append_only()
    """,
]
APPEND_ONLY_SQLITE = [
    """
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events 表只允许追加'); END
    """,
    """
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events 表只允许追加'); END
    """,
]


@event.listens_for(Event.__table__, "after_create")
def _append_only(target, connection, **kw) -> None:  # create_all 路径（测试）；生产走迁移
    stmts = APPEND_ONLY_PG if connection.dialect.name == "postgresql" else APPEND_ONLY_SQLITE
    for s in stmts:
        connection.exec_driver_sql(s)


# ---------------- 文档解析 ----------------


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", name="uq_documents_tenant_sha"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(8))  # pdf / image / docx / xlsx
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    blob_uri: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(
        String(16), default="other"
    )  # tech / tender / drawing / boq / receipt / other
    status: Mapped[str] = mapped_column(String(16))  # parsed / failed
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    data_mode: Mapped[str] = mapped_column(String(8))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentPage(Base):
    __tablename__ = "document_pages"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    method: Mapped[str] = mapped_column(String(16))  # text_layer / ocr / ocr_mock / docx / xlsx
    data_mode: Mapped[str] = mapped_column(String(8))
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    markdown: Mapped[str] = mapped_column(Text)
