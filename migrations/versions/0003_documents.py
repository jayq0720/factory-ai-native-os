"""文档与逐页解析结果

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("blob_uri", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "sha256", name="uq_documents_tenant_sha"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_table(
        "document_pages",
        sa.Column(
            "document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("page", sa.Integer(), primary_key=True),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("title", sa.String(128), nullable=True),
        sa.Column("markdown", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("document_pages")
    op.drop_table("documents")
