"""数据库表：身份（W1）、事件账本与投影（W2–W3）、文档解析（W4）、信道与 Trigger（W5–W6）、Agent 运行（W7）、
审批与待办（W8）、工程方案（W10–W12）、招标拆解（W18）、窑炉监控（W19）。

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
    Boolean,
    DateTime,
    Float,
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
    status: Mapped[str] = mapped_column(String(16))  # queued / parsing / extracting / parsed / failed
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    data_mode: Mapped[str] = mapped_column(String(8))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 解析中：已完成页数
    schema_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 用哪份抽取 Schema
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


class DocumentField(Base):
    """从文档里抽取的字段（感知层结果，可重新抽取）。人工修改与确认记在 history 里留痕；
    字段进入工程项目、报价等业务对象时再经 Ledger.append() 记账。"""

    __tablename__ = "document_fields"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    group_name: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # extracted / missing / corrected / confirmed
    method: Mapped[str] = mapped_column(String(16))  # rule / llm / rule+llm / manual / none
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)  # 不同页给出了不一致的值
    sources: Mapped[list] = mapped_column(JSON, default=list)  # [{page, quote, highlight}]
    candidates: Mapped[list] = mapped_column(JSON, default=list)  # 全部候选值（含被选中的）
    data_mode: Mapped[str] = mapped_column(String(8))
    history: Mapped[list] = mapped_column(
        JSON, default=list
    )  # [{at, by, by_name, action, value, unit, note}]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 信道、Trigger、卡片（W5–W6） ----------------


class ChannelIdentity(Base):
    """IM 账号 → 系统用户。同一个人在钉钉 / 企业微信 / 飞书的账号映射到同一个用户。"""

    __tablename__ = "channel_identities"
    __table_args__ = (Index("ix_channel_identities_user", "user_id"),)

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Thread(Base):
    """一条事件流：一个 IM 会话、一张卡片、一个批次或窑次（方案 2.4.2）。"""

    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_threads_tenant_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(200))  # 例如 dingtalk:group:<会话 id>
    kind: Mapped[str] = mapped_column(String(16))  # im / object / alert
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Trigger(Base):
    """进入系统的一条事件（IM 消息、卡片点击、设备告警、定时任务）及其处理结果。

    这是运行记录，不是业务事实；业务事实仍只通过 Ledger.append() 写入。
    """

    __tablename__ = "triggers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "external_id", name="uq_triggers_external"),
        Index("ix_triggers_thread", "tenant_id", "thread_id", "created_at"),
        Index("ix_triggers_route", "tenant_id", "route", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))  # dingtalk / feishu / wecom / mock / device / schedule
    external_id: Mapped[str] = mapped_column(String(160))  # 平台消息 id，用于去重
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"))
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 未绑定的 IM 账号为空
    sender_ref: Mapped[str] = mapped_column(String(128))  # 平台用户 id
    kind: Mapped[str] = mapped_column(String(16))  # text / image / file / card_action …
    route: Mapped[str] = mapped_column(String(24))  # Fast Path 的分流结果
    status: Mapped[str] = mapped_column(String(16))  # done / pending / failed / rejected
    text: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # 附件引用、卡片动作等
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CardRecord(Base):
    """发出去的卡片，以及点击后要执行的动作（L2 单步获准）。"""

    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("tenant_id", "short_code", name="uq_cards_tenant_code"),
        Index("ix_cards_thread", "tenant_id", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    short_code: Mapped[str] = mapped_column(String(8))  # 文字指令确认用，如 K7Q2M
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"))
    channel: Mapped[str] = mapped_column(String(16))
    conversation: Mapped[dict] = mapped_column(JSON)  # ConversationRef
    card: Mapped[dict] = mapped_column(JSON)  # Card DSL
    action: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 确认后执行的动作
    required_permission: Mapped[str | None] = mapped_column(String(96), nullable=True)
    allowed_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 只允许某个人确认
    status: Mapped[str] = mapped_column(
        String(16), default="open"
    )  # open / done / cancelled / expired / failed
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64))
    acted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- Agent 运行（W7） ----------------


class Run(Base):
    """一次 Worker 运行（方案 2.6 Run / Worker Session）：用哪个 Skill、走了哪些步骤、花了多少 Token。"""

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_thread", "tenant_id", "thread_id", "status"),
        Index("ix_runs_trigger", "tenant_id", "trigger_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"))
    trigger_id: Mapped[str] = mapped_column(ForeignKey("triggers.id"))
    user_id: Mapped[str] = mapped_column(String(36))  # 以谁的名义在查（权限取交集）
    skill_id: Mapped[str] = mapped_column(String(48))
    skill_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # running / waiting（等人回答）/ done / failed
    task: Mapped[str] = mapped_column(Text)
    messages: Mapped[list] = mapped_column(JSON, default=list)  # 对话上下文，用于追问后继续
    steps: Mapped[list] = mapped_column(JSON, default=list)  # 每次工具调用（参数、结果摘要、耗时）
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    data_mode: Mapped[str] = mapped_column(String(8))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 审批与待办（W8） ----------------


class Approval(Base):
    """一张审批单：一个被 Policy 拦下、等人审批后才写入账本的事件。

    审批单本身是治理记录，不是业务事实；审批通过后以申请人身份 Ledger.append()，
    审批单作为事件的证据（evidence kind=approval）。
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_status", "tenant_id", "status", "created_at"),
        Index("ix_approvals_requester", "tenant_id", "requester_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(80))
    request: Mapped[dict] = mapped_column(JSON)  # 审批通过后要追加的 AppendRequest
    rules: Mapped[list] = mapped_column(JSON)  # 命中的规则 [{id, name}]
    steps: Mapped[list] = mapped_column(JSON)  # 审批步骤快照 [{name, role, permission, login, rule}]
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    allow_self: Mapped[bool] = mapped_column(default=False)
    decisions: Mapped[list] = mapped_column(
        JSON, default=list
    )  # [{step, user_id, name, decision, comment, at, via}]
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending / approved / rejected / cancelled / expired / failed
    requester_id: Mapped[str] = mapped_column(String(36))
    requester_name: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))  # api / card:<id>
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    data_mode: Mapped[str] = mapped_column(String(8))
    notified_cards: Mapped[list] = mapped_column(JSON, default=list)  # 发给审批人的卡片 id
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # 记账结果或失败原因
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Todo(Base):
    """待办：提醒、整改、复查之类“需要某人去做”的事。指派给某个人或某个角色。"""

    __tablename__ = "todos"
    __table_args__ = (
        Index("ix_todos_user", "tenant_id", "assignee_user_id", "status"),
        Index("ix_todos_role", "tenant_id", "assignee_role", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text, default="")
    assignee_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assignee_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_type: Mapped[str | None] = mapped_column(String(48), nullable=True)  # 关联对象
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / done / dismissed
    source: Mapped[str] = mapped_column(String(64))  # api / agent:<skill> / system
    data_mode: Mapped[str] = mapped_column(String(8), default="real")
    created_by: Mapped[str] = mapped_column(String(64))
    closed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 工程方案草稿（W10–W12） ----------------


class EngDesign(Base):
    """工程方案草稿的一个版本：衬里 + 传热计算 + BOM + 报价 + 审核清单（方案 4.2 第 5–8 步）。

    草稿是 L1（只建议），不是业务事实，所以不走账本；每次修改生成新版本（旧版本标 superseded），
    所有版本保留可查。发布报价时才以 eng.quote_issued 事件写入账本，引用这里的版本 id。
    """

    __tablename__ = "eng_designs"
    __table_args__ = (
        Index("ix_eng_designs_project", "tenant_id", "project_id", "created_at"),
        UniqueConstraint("tenant_id", "project_id", "label", "version", name="uq_eng_designs_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(32))  # 方案 A / 方案 B / 客户方案
    title: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 上一版本
    status: Mapped[str] = mapped_column(String(16))  # draft / submitted / published / superseded
    data_mode: Mapped[str] = mapped_column(String(8))
    inputs: Mapped[dict] = mapped_column(JSON)  # {layers, options, template_id}：重算只需要这些
    data: Mapped[dict] = mapped_column(JSON)  # 计算结果：传热、校核、BOM、报价、选材理由
    review: Mapped[list] = mapped_column(JSON, default=list)  # 审核条目（含处理结果）
    project_version: Mapped[int] = mapped_column(Integer)  # 计算时工程项目的版本
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str] = mapped_column(String(36))
    created_by_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 招标拆解草稿（W18） ----------------


class TenderAnalysis(Base):
    """招标文件拆解的一个版本：章节结构、需求矩阵、偏离表、评分点、资料清单（方案 4.3 第 1–6 步）。

    和工程方案一样是 L1 草稿，不是业务事实：重新拆解生成新版本（旧版本标 superseded），
    人工处理结果（响应、负责人、状态）按条款 id 带到新版本。提交投标时才以 sales.tender_submitted
    事件写入账本，引用这里的版本 id。
    """

    __tablename__ = "tender_analyses"
    __table_args__ = (
        Index("ix_tender_analyses_tender", "tenant_id", "tender_id", "created_at"),
        UniqueConstraint("tenant_id", "tender_id", "version", name="uq_tender_analyses_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    tender_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # draft / submitted / superseded
    data_mode: Mapped[str] = mapped_column(String(8))
    data: Mapped[dict] = mapped_column(JSON)  # 章节、评分点、资料清单、偏离表、风险
    items: Mapped[list] = mapped_column(JSON, default=list)  # 需求矩阵（含人工处理结果）
    tender_version: Mapped[int] = mapped_column(Integer)  # 拆解时招标项目的版本
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str] = mapped_column(String(36))
    created_by_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 窑炉监控（W19） ----------------


class PointReading(Base):
    """测点读数（时序）。三条接入路径的数据进同一张表，只是 source / quality 不同（方案 3.2）。

    读数不是业务事实，不走账本；只追加，不改。结束窑次时按读数区间算出的最高温度、偏差、能耗才入账。
    """

    __tablename__ = "point_readings"
    __table_args__ = (Index("ix_point_readings_point_ts", "tenant_id", "point", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    point: Mapped[str] = mapped_column(String(64))  # 测点编号（monitor_point.code）
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))  # direct / retrofit / manual
    quality: Mapped[str] = mapped_column(String(16))  # good / estimated / bad
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str | None] = mapped_column(String(512), nullable=True)
    edge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 人工抄表的人
    data_mode: Mapped[str] = mapped_column(String(8), default="real")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EdgeBatch(Base):
    """fos-edge 上行的一批读数。(edge_id, seq) 唯一：断网补传、重发都不会重复入库；也当心跳用。"""

    __tablename__ = "edge_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "edge_id", "seq", name="uq_edge_batches_seq"),
        Index("ix_edge_batches_received", "tenant_id", "edge_id", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    edge_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(BigInteger)
    backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[dict] = mapped_column(JSON, default=dict)  # 点位 → 原因
    # 厂端状态（EdgeStatus：版本、积压、被拒批次、各点位健康）；只有云端要了状态的那一批才有（W21）
    status: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    received_by: Mapped[str] = mapped_column(String(36))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KilnAlarm(Base):
    """监控告警：超上下限、曲线超差、网关断网、设备故障。条件消失自动关闭；人可以确认（已知晓）。

    告警是从读数派生的运行状态，不是业务事实，不走账本；同一 key 同时只有一条未关闭的告警。
    """

    __tablename__ = "kiln_alarms"
    __table_args__ = (Index("ix_kiln_alarms_open", "tenant_id", "status", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(160))  # kind:点位 或 kind:网关
    kind: Mapped[str] = mapped_column(String(16))  # high / low / deviation / edge_offline / point_fault
    point: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kiln: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kiln_run: Mapped[str | None] = mapped_column(String(64), nullable=True)
    edge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # open / closed
    message: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    data_mode: Mapped[str] = mapped_column(String(8), default="real")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    acked_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EdgeCommandRecord(Base):
    """云端发给 fos-edge 的只读命令（W21，方案 7.2）：排队 → 随上行回执签名下发 → 厂端回报结果。

    命令不是业务事实，不走账本；谁要的、什么时候、结果如何都记在这里备查。
    签名在下发时做（有效期很短）；没收到结果就用同一个 id 重签，厂端按 id 去重、只回报上次结果。
    """

    __tablename__ = "edge_commands"
    __table_args__ = (Index("ix_edge_commands_open", "tenant_id", "edge_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)  # = command_id
    tenant_id: Mapped[str] = mapped_column(String(32))
    edge_id: Mapped[str] = mapped_column(String(64))
    command: Mapped[dict] = mapped_column(JSON)  # {"type": "read_now", "points": [...]} 等
    status: Mapped[str] = mapped_column(String(16))  # pending / sent / done / failed / expired / cancelled
    deliveries: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str] = mapped_column(String(500), default="")
    auto: Mapped[bool] = mapped_column(Boolean, default=False)  # Watchdog 自动要的（定时要状态）
    created_by: Mapped[str] = mapped_column(String(36))
    created_by_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 过了还没结果就作废
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
