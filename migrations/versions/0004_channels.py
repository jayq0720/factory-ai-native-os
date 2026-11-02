"""信道账号绑定、Thread、Trigger、卡片

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "channel_identities",
        sa.Column("tenant_id", sa.String(32), primary_key=True),
        sa.Column("channel", sa.String(16), primary_key=True),
        sa.Column("external_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_channel_identities_user", "channel_identities", ["user_id"])
    op.create_table(
        "threads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        _ts("created_at"),
        _ts("last_trigger_at"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_threads_tenant_key"),
    )
    op.create_table(
        "triggers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(160), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("sender_ref", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("route", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("tenant_id", "source", "external_id", name="uq_triggers_external"),
    )
    op.create_index("ix_triggers_thread", "triggers", ["tenant_id", "thread_id", "created_at"])
    op.create_index("ix_triggers_route", "triggers", ["tenant_id", "route", "status"])
    op.create_table(
        "cards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("short_code", sa.String(8), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("conversation", sa.JSON(), nullable=False),
        sa.Column("card", sa.JSON(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=True),
        sa.Column("required_permission", sa.String(96), nullable=True),
        sa.Column("allowed_user_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("acted_by", sa.String(36), nullable=True),
        _ts("acted_at", nullable=True),
        _ts("expires_at"),
        _ts("created_at"),
        sa.UniqueConstraint("tenant_id", "short_code", name="uq_cards_tenant_code"),
    )
    op.create_index("ix_cards_thread", "cards", ["tenant_id", "thread_id"])


def downgrade() -> None:
    op.drop_table("cards")
    op.drop_table("triggers")
    op.drop_table("threads")
    op.drop_table("channel_identities")
