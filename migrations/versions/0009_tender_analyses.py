"""招标拆解草稿

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tender_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("tender_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("tender_version", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_by_name", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "tender_id", "version", name="uq_tender_analyses_version"),
    )
    op.create_index("ix_tender_analyses_tender", "tender_analyses", ["tenant_id", "tender_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_tender_analyses_tender", table_name="tender_analyses")
    op.drop_table("tender_analyses")
