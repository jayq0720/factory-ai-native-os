"""工程方案草稿

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eng_designs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("review", sa.JSON(), nullable=False),
        sa.Column("project_version", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_by_name", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "project_id", "label", "version", name="uq_eng_designs_version"),
    )
    op.create_index("ix_eng_designs_project", "eng_designs", ["tenant_id", "project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_eng_designs_project", table_name="eng_designs")
    op.drop_table("eng_designs")
