"""审计加固：ApiKey 绑定 edge_id、窑炉未关闭告警唯一索引及常用查询索引

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("edge_id", sa.String(64), nullable=True))
    op.create_index("ix_api_keys_user", "api_keys", ["user_id"])
    op.create_index("ix_events_occurred", "events", ["tenant_id", "occurred_at"])
    op.create_index("ix_cards_open_exp", "cards", ["tenant_id", "status", "expires_at"])
    op.create_index("ix_approvals_open_exp", "approvals", ["tenant_id", "status", "expires_at"])
    op.create_index(
        "uq_kiln_alarms_open",
        "kiln_alarms",
        ["tenant_id", "key"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_kiln_alarms_open", table_name="kiln_alarms")
    op.drop_index("ix_approvals_open_exp", table_name="approvals")
    op.drop_index("ix_cards_open_exp", table_name="cards")
    op.drop_index("ix_events_occurred", table_name="events")
    op.drop_index("ix_api_keys_user", table_name="api_keys")
    op.drop_column("api_keys", "edge_id")
