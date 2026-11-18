"""Время последнего сообщения темы и время последней проверки для законов.

Синк скачивает тему заново, только если в списке тем раздела изменилось время
последнего сообщения или с прошлой полной проверки прошло около недели (правки первого
поста время последнего сообщения не меняют). Так плановое обновление делает 1–2 запроса
на сервер вместо 30–40, и форумы не блокируют IP.

Revision ID: 031_law_source_activity
Revises: 030_remove_closed_servers
Create Date: 2026-11-18
"""

from typing import Sequence, Union

from alembic import op


revision: str = "031_law_source_activity"
down_revision: Union[str, None] = "030_remove_closed_servers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE law_documents ADD COLUMN IF NOT EXISTS source_last_post_at BIGINT")
    op.execute("ALTER TABLE law_documents ADD COLUMN IF NOT EXISTS source_checked_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE law_documents DROP COLUMN IF EXISTS source_checked_at")
    op.execute("ALTER TABLE law_documents DROP COLUMN IF EXISTS source_last_post_at")
