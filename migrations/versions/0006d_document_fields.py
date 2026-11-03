"""文档字段抽取：document_fields 表，documents 增加进度与 Schema

Revision ID: 0006d
Revises: 0005
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006d"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("progress", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("documents", sa.Column("schema_id", sa.String(32), nullable=True))
    op.create_table(
        "document_fields",
        sa.Column(
            "document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("key", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("group_name", sa.String(32), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("conflict", sa.Boolean(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_document_fields_tenant_id", "document_fields", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_document_fields_tenant_id", table_name="document_fields")
    op.drop_table("document_fields")
    op.drop_column("documents", "schema_id")
    op.drop_column("documents", "progress")
