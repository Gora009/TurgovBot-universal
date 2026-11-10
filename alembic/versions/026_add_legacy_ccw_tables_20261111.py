"""Bring legacy criminal-case tables and system_cache under Alembic.

Таблицы criminal_case_sessions, criminal_case_parts и system_cache используются кодом
(уголовные дела, кэш шаблонов и др.), но создавались только ручным скриптом
database/create_ccw_tables.sql, который не входит в цепочку миграций. На свежей
установке через `alembic upgrade head` их не было.

Миграция переносит тот скрипт как есть. Всё через IF NOT EXISTS: на базах, где
скрипт уже запускали руками, ничего не меняется.

Revision ID: 026_legacy_ccw_tables
Revises: 025_assistant_case_digest
Create Date: 2026-11-11
"""

from typing import Sequence, Union

from alembic import op


revision: str = "026_legacy_ccw_tables"
down_revision: Union[str, None] = "025_assistant_case_digest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS criminal_case_sessions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            case_number VARCHAR(50),
            server_id TEXT REFERENCES servers(server_id) ON DELETE SET NULL,
            current_part_index INTEGER DEFAULT 0,
            collected_data JSONB DEFAULT '{}',
            status VARCHAR(20) DEFAULT 'active',
            google_doc_id VARCHAR(100),
            google_doc_url TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_criminal_case_sessions_user_status "
        "ON criminal_case_sessions(user_id, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS criminal_case_parts (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES criminal_case_sessions(id) ON DELETE CASCADE,
            part_type VARCHAR(50) NOT NULL,
            version INTEGER DEFAULT 1,
            content TEXT,
            ai_review_notes TEXT,
            is_final BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_criminal_case_parts_session_type "
        "ON criminal_case_parts(session_id, part_type)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS system_cache (
            key VARCHAR(255) PRIMARY KEY,
            value JSONB,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    # Намеренно пусто: на существующих базах эти таблицы старше Alembic и содержат
    # рабочие данные. Откат ревизии не должен их удалять.
    pass
