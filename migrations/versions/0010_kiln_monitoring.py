"""窑炉监控：测点读数、网关上行批次、监控告警

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "point_readings",
        sa.Column(
            "id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
        ),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("point", sa.String(64), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence", sa.String(512), nullable=True),
        sa.Column("edge_id", sa.String(64), nullable=True),
        sa.Column("recorded_by", sa.String(36), nullable=True),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("received_at", TS, nullable=False),
    )
    op.create_index("ix_point_readings_point_ts", "point_readings", ["tenant_id", "point", "ts"])
    op.create_table(
        "edge_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("edge_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("backfill", sa.Boolean(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.JSON(), nullable=False),
        sa.Column("received_by", sa.String(36), nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.UniqueConstraint("tenant_id", "edge_id", "seq", name="uq_edge_batches_seq"),
    )
    op.create_index("ix_edge_batches_received", "edge_batches", ["tenant_id", "edge_id", "received_at"])
    op.create_table(
        "kiln_alarms",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("point", sa.String(64), nullable=True),
        sa.Column("kiln", sa.String(64), nullable=True),
        sa.Column("kiln_run", sa.String(64), nullable=True),
        sa.Column("edge_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("notified_at", TS, nullable=True),
        sa.Column("acked_by", sa.String(36), nullable=True),
        sa.Column("acked_by_name", sa.String(64), nullable=True),
        sa.Column("acked_at", TS, nullable=True),
    )
    op.create_index("ix_kiln_alarms_open", "kiln_alarms", ["tenant_id", "status", "key"])


def downgrade() -> None:
    op.drop_index("ix_kiln_alarms_open", table_name="kiln_alarms")
    op.drop_table("kiln_alarms")
    op.drop_index("ix_edge_batches_received", table_name="edge_batches")
    op.drop_table("edge_batches")
    op.drop_index("ix_point_readings_point_ts", table_name="point_readings")
    op.drop_table("point_readings")
