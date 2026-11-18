"""Таблицы, которые код использует, но ни одна миграция не создавала.

На старом сервере они были созданы вручную (api_keys — отдельным SQL-скриптом, удалённым
при рефакторинге). На новой базе их отсутствие роняло /admin, лимиты ассистента, разбор
жалоб в прокуратуру и анализатор жалоб. Схемы восстановлены по запросам в коде.

Все CREATE — IF NOT EXISTS: на базах, где таблицы уже есть, миграция ничего не меняет.

Revision ID: 029_legacy_unmanaged_tables
Revises: 028_users_status_flags
Create Date: 2026-11-18
"""

from typing import Sequence, Union

from alembic import op


revision: str = "029_legacy_unmanaged_tables"
down_revision: Union[str, None] = "028_users_status_flags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STATEMENTS = (
    # services/promo_service.py: настройки бота ключ → JSON-строка
    """
    CREATE TABLE IF NOT EXISTS bot_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # user_handlers/core.py: скользящее окно лимитов ассистента
    """
    CREATE TABLE IF NOT EXISTS assistant_usage_log (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        usage_type TEXT NOT NULL,
        timestamp  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_assistant_usage_log_user_ts ON assistant_usage_log (user_id, timestamp)",
    # api/: ключи REST API (схема из database/migrate_add_api_keys.sql)
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        id                  SERIAL PRIMARY KEY,
        key_hash            VARCHAR(64) UNIQUE NOT NULL,
        key_prefix          VARCHAR(12) NOT NULL,
        name                VARCHAR(100) NOT NULL,
        owner_user_id       BIGINT NOT NULL,
        key_type            VARCHAR(10) NOT NULL,
        allowed_origins     TEXT[] DEFAULT NULL,
        allowed_ips         TEXT[] DEFAULT NULL,
        is_active           BOOLEAN DEFAULT TRUE,
        requests_per_minute INTEGER DEFAULT 10,
        created_at          TIMESTAMPTZ DEFAULT now(),
        last_used_at        TIMESTAMPTZ DEFAULT NULL,
        total_requests      INTEGER DEFAULT 0,
        last_ip             VARCHAR(45) DEFAULT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_api_keys_owner ON api_keys (owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_keys_active ON api_keys (is_active)",
    # services/complaint_analyzer_service/forms.py: форма жалобы сервера
    """
    CREATE TABLE IF NOT EXISTS complaint_form_templates (
        server_id    TEXT PRIMARY KEY,
        form_url     TEXT,
        form_content TEXT,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # services/complaint_analyzer_service/forms.py: типы нарушений в жалобах
    """
    CREATE TABLE IF NOT EXISTS complaint_issue_types (
        id          SERIAL PRIMARY KEY,
        server_id   TEXT NOT NULL,
        code        TEXT NOT NULL,
        name        TEXT NOT NULL,
        description TEXT,
        is_required BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (server_id, code)
    )
    """,
    # user_handlers/prosecution_complaint_flow/session.py: сессии разбора жалоб в прокуратуру
    """
    CREATE TABLE IF NOT EXISTS prosecution_complaint_sessions (
        id                      SERIAL PRIMARY KEY,
        user_id                 BIGINT NOT NULL,
        server_id               TEXT NOT NULL,
        complaint_url           TEXT,
        complaint_number        TEXT,
        status                  TEXT NOT NULL DEFAULT 'active',
        current_stage_index     INTEGER NOT NULL DEFAULT 0,
        parsed_complaint        JSONB,
        collected_data          JSONB NOT NULL DEFAULT '{}'::jsonb,
        generated_documents     JSONB,
        ai_recommendations      JSONB,
        form_compliance_status  JSONB,
        linked_criminal_case_id INTEGER,
        expiry_warning_sent     BOOLEAN NOT NULL DEFAULT FALSE,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_pch_sessions_user_status ON prosecution_complaint_sessions (user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS prosecution_complaint_stages (
        id              SERIAL PRIMARY KEY,
        session_id      INTEGER NOT NULL REFERENCES prosecution_complaint_sessions(id) ON DELETE CASCADE,
        stage_type      TEXT NOT NULL,
        version         INTEGER NOT NULL DEFAULT 1,
        content         TEXT,
        ai_review_notes TEXT,
        is_final        BOOLEAN NOT NULL DEFAULT FALSE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_pch_stages_session_type ON prosecution_complaint_stages (session_id, stage_type, version)",
    # Форма подачи жалобы, сохранённая пользователем для сервера
    """
    CREATE TABLE IF NOT EXISTS prosecution_form_templates (
        user_id      BIGINT NOT NULL,
        server_id    TEXT NOT NULL,
        form_content TEXT NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, server_id)
    )
    """,
    # user_handlers/report_flow.py, admin_handlers/reports.py: описание и время последнего сообщения
    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS title TEXT",
    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Таблицы могли существовать до этой миграции (старые базы) и содержать данные.
    pass
