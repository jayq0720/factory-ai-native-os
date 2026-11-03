"""审批单与待办

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("allow_self", sa.Boolean(), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("requester_name", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=True),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("notified_cards", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approvals_status", "approvals", ["tenant_id", "status", "created_at"])
    op.create_index("ix_approvals_requester", "approvals", ["tenant_id", "requester_id", "created_at"])
    op.create_table(
        "todos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("assignee_user_id", sa.String(36), nullable=True),
        sa.Column("assignee_role", sa.String(32), nullable=True),
        sa.Column("object_type", sa.String(48), nullable=True),
        sa.Column("object_id", sa.String(64), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("data_mode", sa.String(8), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("closed_by", sa.String(36), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_todos_user", "todos", ["tenant_id", "assignee_user_id", "status"])
    op.create_index("ix_todos_role", "todos", ["tenant_id", "assignee_role", "status"])


def downgrade() -> None:
    op.drop_table("todos")
    op.drop_table("approvals")
