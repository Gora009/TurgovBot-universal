"""Парсинг жалобы по ссылке, шаблон формы, справочник типов нарушений."""

import logging
import re
from typing import Optional, Dict, List

from database.manager import db_manager
from config import DEFAULT_COMPLAINT_ISSUE_TYPES
from knowledge.remote_parser import remote_parse_topic

logger = logging.getLogger('services.complaint_analyzer_service')


# === ПАРСИНГ ЖАЛОБЫ ===

async def parse_complaint_from_url(url: str) -> Dict:
    """
    Парсит жалобу по URL.

    Args:
        url: URL темы с жалобой на форуме

    Returns:
        Dict: {title, content, thread_id}

    Raises:
        ValueError: Если URL невалидный или парсинг не удался
    """
    logger.info(f"[COMPLAINT_ANALYZER] Парсинг жалобы: {url}")

    # content_cleanup=False: в жалобах blockquote-цитаты — содержимое (цитируемые
    # доказательства/реплики), чистка разметки rmrp-законки здесь недопустима.
    result = await remote_parse_topic(url, parse_only_author=False, content_cleanup=False)

    if not result:
        raise ValueError(f"Не удалось распарсить жалобу по URL: {url}")

    title = result.get('title', '')
    content = result.get('content', '')
    thread_id = result.get('thread_id')

    if not content:
        raise ValueError("Жалоба не содержит текста")

    logger.info(f"[COMPLAINT_ANALYZER] Успешно распарсено: {len(content)} символов")

    return {
        'title': title,
        'content': content,
        'thread_id': thread_id
    }


# === УПРАВЛЕНИЕ ШАБЛОНАМИ ФОРМ ===

async def get_form_template(server_id: str) -> Optional[Dict]:
    """
    Получает шаблон формы для сервера.

    Returns:
        Dict с form_url, form_content, updated_at или None
    """
    query = """
        SELECT server_id, form_url, form_content, updated_at
        FROM complaint_form_templates
        WHERE server_id = $1
    """
    row = await db_manager.fetchrow(query, server_id)

    if row:
        return dict(row)
    return None


async def set_form_template(server_id: str, form_url: str) -> Dict:
    """
    Устанавливает шаблон формы для сервера.
    Парсит URL и сохраняет контент.

    Args:
        server_id: ID сервера
        form_url: URL темы с образцом формы

    Returns:
        Dict с информацией о шаблоне

    Raises:
        ValueError: Если парсинг не удался
    """
    logger.info(f"[COMPLAINT_ANALYZER] Установка шаблона формы для {server_id}: {form_url}")

    # Парсим URL (content_cleanup=False: образец формы может быть завёрнут в цитату)
    result = await remote_parse_topic(form_url, parse_only_author=True, content_cleanup=False)

    if not result:
        raise ValueError(f"Не удалось распарсить шаблон по URL: {form_url}")

    form_content = result.get('content', '')

    if not form_content:
        raise ValueError("Шаблон не содержит текста")

    # Сохраняем в БД (upsert)
    query = """
        INSERT INTO complaint_form_templates (server_id, form_url, form_content, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (server_id)
        DO UPDATE SET form_url = $2, form_content = $3, updated_at = NOW()
        RETURNING server_id, form_url, form_content, updated_at
    """
    row = await db_manager.fetchrow(query, server_id, form_url, form_content)

    logger.info(f"[COMPLAINT_ANALYZER] Шаблон формы сохранён для {server_id}")

    return dict(row)


# === УПРАВЛЕНИЕ ТИПАМИ ПРОБЛЕМ ===

async def get_issue_types(server_id: str) -> List[Dict]:
    """
    Получает список типов проблем для сервера.
    Если таблица пуста для сервера - автоматически вставляет дефолтные типы.

    Returns:
        Список словарей {code, name, description, is_required}
    """
    query = """
        SELECT code, name, description, is_required
        FROM complaint_issue_types
        WHERE server_id = $1
        ORDER BY created_at
    """
    rows = await db_manager.fetch(query, server_id)

    if not rows:
        # Автоинициализация дефолтных типов
        logger.info(f"[COMPLAINT_ANALYZER] Инициализация дефолтных issue_types для {server_id}")
        await _initialize_default_issue_types(server_id)
        rows = await db_manager.fetch(query, server_id)

    return [dict(row) for row in rows]


async def _initialize_default_issue_types(server_id: str) -> None:
    """Вставляет дефолтные типы проблем для сервера."""
    for issue_type in DEFAULT_COMPLAINT_ISSUE_TYPES:
        query = """
            INSERT INTO complaint_issue_types (server_id, code, name, description, is_required)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (server_id, code) DO NOTHING
        """
        await db_manager.execute(
            query,
            server_id,
            issue_type['code'],
            issue_type['name'],
            issue_type.get('description'),
            issue_type.get('is_required', False)
        )


async def add_issue_type(
    server_id: str,
    code: str,
    name: str,
    description: Optional[str] = None,
    is_required: bool = False
) -> Dict:
    """
    Добавляет новый тип проблемы.

    Args:
        server_id: ID сервера
        code: Код (snake_case)
        name: Название
        description: Описание
        is_required: Обязательное поле

    Returns:
        Dict с данными созданного типа

    Raises:
        ValueError: Если тип с таким кодом уже существует
    """
    # Нормализуем код
    code = code.lower().strip()

    # Проверяем формат
    if not re.match(r'^[a-z][a-z0-9_]*$', code):
        raise ValueError("Код должен быть в формате snake_case (латиница, цифры, _)")

    query = """
        INSERT INTO complaint_issue_types (server_id, code, name, description, is_required)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING code, name, description, is_required
    """
    try:
        row = await db_manager.fetchrow(query, server_id, code, name, description, is_required)
        logger.info(f"[COMPLAINT_ANALYZER] Добавлен issue_type '{code}' для {server_id}")
        return dict(row)
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            raise ValueError(f"Тип проблемы с кодом '{code}' уже существует для сервера {server_id}")
        raise


async def update_issue_type(
    server_id: str,
    code: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_required: Optional[bool] = None
) -> Dict:
    """
    Обновляет тип проблемы.

    Returns:
        Dict с обновлёнными данными

    Raises:
        ValueError: Если тип не найден
    """
    # Формируем динамический UPDATE
    updates = []
    params = [server_id, code]
    param_idx = 3

    if name is not None:
        updates.append(f"name = ${param_idx}")
        params.append(name)
        param_idx += 1

    if description is not None:
        updates.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if is_required is not None:
        updates.append(f"is_required = ${param_idx}")
        params.append(is_required)
        param_idx += 1

    if not updates:
        # Ничего не обновляем, просто возвращаем текущие данные
        query = """
            SELECT code, name, description, is_required
            FROM complaint_issue_types
            WHERE server_id = $1 AND code = $2
        """
        row = await db_manager.fetchrow(query, server_id, code)
        if not row:
            raise ValueError(f"Тип проблемы '{code}' не найден для сервера {server_id}")
        return dict(row)

    query = f"""
        UPDATE complaint_issue_types
        SET {', '.join(updates)}
        WHERE server_id = $1 AND code = $2
        RETURNING code, name, description, is_required
    """
    row = await db_manager.fetchrow(query, *params)

    if not row:
        raise ValueError(f"Тип проблемы '{code}' не найден для сервера {server_id}")

    logger.info(f"[COMPLAINT_ANALYZER] Обновлён issue_type '{code}' для {server_id}")
    return dict(row)


async def delete_issue_type(server_id: str, code: str) -> bool:
    """
    Удаляет тип проблемы.

    Returns:
        True если удалён, False если не найден
    """
    query = """
        DELETE FROM complaint_issue_types
        WHERE server_id = $1 AND code = $2
        RETURNING id
    """
    result = await db_manager.fetchval(query, server_id, code)

    if result:
        logger.info(f"[COMPLAINT_ANALYZER] Удалён issue_type '{code}' для {server_id}")
        return True
    return False
