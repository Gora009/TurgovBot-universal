"""Журнал действий администраторов (таблица admin_action_log, миграция 027).

Раньше записи уходили в Google Sheets (лист Admin_Logs). Имя функции оставлено
прежним — её вызывают разделы админ-панели. Ошибка записи не должна ломать действие
админа, поэтому она только логируется.
"""
import logging

from database.manager import db_manager

logger = logging.getLogger(__name__)


async def log_admin_action_to_sheet(admin_id: int, action: str, details: str = "N/A") -> None:
    try:
        await db_manager.execute(
            "INSERT INTO admin_action_log (admin_id, action, details) VALUES ($1, $2, $3)",
            int(admin_id), str(action), None if details is None else str(details),
        )
    except Exception:
        logger.exception("[ADMIN_LOG] не удалось записать действие %s админа %s", action, admin_id)


log_admin_action = log_admin_action_to_sheet
