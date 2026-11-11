"""厂端命令：云端签发的只读命令、上行批次附带的厂端状态

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("edge_batches", sa.Column("status", sa.JSON(), nullable=True))
    op.create_table(
        "edge_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("edge_id", sa.String(64), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("deliveries", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(500), nullable=False),
        sa.Column("auto", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_by_name", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("sent_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
    )
    op.create_index("ix_edge_commands_open", "edge_commands", ["tenant_id", "edge_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_edge_commands_open", table_name="edge_commands")
    op.drop_table("edge_commands")
    op.drop_column("edge_batches", "status")
