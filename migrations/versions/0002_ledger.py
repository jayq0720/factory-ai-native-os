"""事件账本与投影

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from fos.db.models import APPEND_ONLY_PG

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_counters",
        sa.Column("tenant_id", sa.String(32), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(48), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_name", sa.String(64), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("causation_id", sa.String(36), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("ontology_layers", sa.JSON(), nullable=False),
        sa.UniqueConstraint("tenant_id", "seq", name="uq_events_tenant_seq"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_events_tenant_idem"),
    )
    op.create_index("ix_events_object", "events", ["tenant_id", "object_type", "object_id", "seq"])
    op.create_index("ix_events_type", "events", ["tenant_id", "type", "seq"])
    for stmt in APPEND_ONLY_PG:
        op.execute(stmt)

    op.create_table(
        "objects",
        sa.Column("tenant_id", sa.String(32), primary_key=True),
        sa.Column("object_type", sa.String(48), primary_key=True),
        sa.Column("object_id", sa.String(64), primary_key=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_seq", sa.BigInteger(), nullable=False),
        sa.Column("updated_seq", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_objects_type", "objects", ["tenant_id", "object_type", "updated_seq"])
    op.create_table(
        "object_links",
        sa.Column("tenant_id", sa.String(32), primary_key=True),
        sa.Column("from_type", sa.String(48), primary_key=True),
        sa.Column("from_id", sa.String(64), primary_key=True),
        sa.Column("rel", sa.String(48), primary_key=True),
        sa.Column("to_type", sa.String(48), primary_key=True),
        sa.Column("to_id", sa.String(64), primary_key=True),
    )
    op.create_index("ix_links_to", "object_links", ["tenant_id", "to_type", "to_id"])


def downgrade() -> None:
    op.drop_table("object_links")
    op.drop_table("objects")
    op.execute("DROP TRIGGER IF EXISTS events_append_only ON events")
    op.execute("DROP FUNCTION IF EXISTS fos_events_append_only()")
    op.drop_table("events")
    op.drop_table("tenant_counters")
