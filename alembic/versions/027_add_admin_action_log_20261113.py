"""Журнал действий администраторов в БД (вместо листа Admin_Logs в Google Sheets).

Revision ID: 027_admin_action_log
Revises: 026_legacy_ccw_tables
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "027_admin_action_log"
down_revision: Union[str, None] = "026_legacy_ccw_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_action_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
    )
    op.create_index("ix_admin_action_log_created_at", "admin_action_log", ["created_at"])
    op.create_index("ix_admin_action_log_admin_id", "admin_action_log", ["admin_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_action_log_admin_id", table_name="admin_action_log")
    op.drop_index("ix_admin_action_log_created_at", table_name="admin_action_log")
    op.drop_table("admin_action_log")
