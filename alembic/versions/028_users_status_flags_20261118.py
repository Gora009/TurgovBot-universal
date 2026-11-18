"""Колонки users.is_subscriber, users.is_tester, users.is_blocked.

Код читает и пишет эти флаги, но ни одна миграция их не создавала: на старой базе они
появились вручную. На новой базе первый же вопрос пользователя падал с
UndefinedColumnError. IF NOT EXISTS — на базах, где колонки уже есть, миграция ничего
не меняет.

Revision ID: 028_users_status_flags
Revises: 027_admin_action_log
Create Date: 2026-11-18
"""

from typing import Sequence, Union

from alembic import op


revision: str = "028_users_status_flags"
down_revision: Union[str, None] = "027_admin_action_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("is_subscriber", "is_tester", "is_blocked")


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} BOOLEAN NOT NULL DEFAULT FALSE"
        )


def downgrade() -> None:
    # Колонки могли существовать до этой миграции (старые базы), поэтому не удаляем их.
    pass
