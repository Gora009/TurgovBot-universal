"""Удаление закрытых серверов GTA5RP и NOVA RP Mirage.

На форуме GTA5RP осталось 8 серверов (Downtown, Eclipse, Rockford, Redwood, Murrieta,
Chiliad, Mirror, Blaine). Остальные закрыты, их разделы законов недоступны. У NOVA RP
раздел сервера Mirage удалён с форума.

Удаление из servers каскадом удаляет законы и прецеденты сервера (law_documents →
law_articles, law_chunks, синонимы, векторы; precedents). У уголовных дел server_id
обнуляется (ON DELETE SET NULL). Пользователям, у которых закрытый сервер стоял по
умолчанию, сбрасывается выбор: бот попросит выбрать сервер заново. История вопросов,
жалоб и дел пользователей не удаляется.

Список серверов указан явно: синхронизация с конфигом серверы никогда не удаляет,
чтобы ошибка в конфиге не стёрла данные.

Revision ID: 030_remove_closed_servers
Revises: 029_legacy_unmanaged_tables
Create Date: 2026-11-18
"""

from typing import Sequence, Union

from alembic import op


revision: str = "030_remove_closed_servers"
down_revision: Union[str, None] = "029_legacy_unmanaged_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CLOSED_SERVERS = (
    # GTA5RP
    "strawberry", "vinewood", "blackberry", "insquad", "sunrise", "rainbow", "richman",
    "la_mesa", "burton", "alta", "del_perro", "davis", "harmony", "hawick", "grapeseed",
    "vespucci", "milton", "la_puerta", "senora",
    # NOVA RP
    "nova_mirage",
)


def upgrade() -> None:
    ids = ", ".join(f"'{s}'" for s in CLOSED_SERVERS)
    op.execute(
        f"UPDATE users SET default_server_id = NULL, default_project_id = NULL "
        f"WHERE default_server_id IN ({ids})"
    )
    op.execute(f"DELETE FROM servers WHERE server_id IN ({ids})")


def downgrade() -> None:
    # Удалённые законы не восстановить; серверы вернутся в таблицу при следующей
    # синхронизации, если их снова добавить в knowledge/sources.py.
    pass
