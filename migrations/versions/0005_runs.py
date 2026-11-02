"""Agent 运行记录

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("trigger_id", sa.String(36), sa.ForeignKey("triggers.id"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("skill_id", sa.String(48), nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runs_thread", "runs", ["tenant_id", "thread_id", "status"])
    op.create_index("ix_runs_trigger", "runs", ["tenant_id", "trigger_id"])


def downgrade() -> None:
    op.drop_table("runs")
