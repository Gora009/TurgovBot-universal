"""Сессия разбора жалобы: менеджер в БД, шаблоны форм, номер жалобы, отладочные утилиты."""

import json
import logging
import io
from datetime import datetime
from typing import Dict, List, Optional
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import PCH_DOCUMENT_TYPES
from database.manager import db_manager


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

async def send_debug_document(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    content: str,
    filename: str,
    caption: str = None
) -> None:
    """
    Отправляет текстовый документ для дебага.

    Args:
        context: Контекст бота
        chat_id: ID чата
        content: Содержимое документа
        filename: Имя файла (например, 'form_parsed.txt')
        caption: Подпись к документу
    """
    try:
        # Создаём файл в памяти
        file_bytes = io.BytesIO(content.encode('utf-8'))
        file_bytes.name = filename

        await context.bot.send_document(
            chat_id=chat_id,
            document=file_bytes,
            filename=filename,
            caption=caption or f"🔍 Debug: {filename}"
        )
        logging.info(f"[PCH] Отправлен дебаг-документ: {filename}")
    except Exception as e:
        logging.warning(f"[PCH] Не удалось отправить дебаг-документ: {e}")


async def safe_edit_message_text(query, text: str, **kwargs):
    """
    Безопасно редактирует сообщение, игнорируя ошибку 'Message is not modified'.
    """
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logging.debug(f"[PCH] Message not modified (ignored): {e}")
            return None
        raise


def generate_complaint_number() -> str:
    """Генерирует уникальный номер жалобы в формате PCH-YYYY-XXXX."""
    year = datetime.now().year
    # Получаем следующий номер из последовательности
    # Формат: PCH-2026-0001, PCH-2026-0002, ...
    return f"PCH-{year}-{{seq:04d}}"


# === КОНСТАНТЫ ===

STAGES_PER_PAGE = 4  # Все этапы помещаются на одной странице


# === КЛАСС МЕНЕДЖЕРА СЕССИИ ===

class ProsecutionComplaintManager:
    """Управляет сессией обработки жалобы в прокуратуру."""

    def __init__(self, session_id: int):
        self.session_id = session_id
        self._session_data = None

    @classmethod
    async def create_session(
        cls,
        user_id: int,
        server_id: str,
        complaint_url: str
    ) -> 'ProsecutionComplaintManager':
        """Создаёт новую сессию обработки жалобы."""
        # Генерируем номер жалобы
        year = datetime.now().year

        # Получаем следующий номер для этого года
        count_query = """
            SELECT COUNT(*) + 1 FROM prosecution_complaint_sessions
            WHERE EXTRACT(YEAR FROM created_at) = $1
        """
        seq_num = await db_manager.fetchval(count_query, year) or 1
        complaint_number = f"PCH-{year}-{seq_num:04d}"

        query = """
            INSERT INTO prosecution_complaint_sessions 
            (user_id, server_id, complaint_url, complaint_number, collected_data, status)
            VALUES ($1, $2, $3, $4, '{}', 'active')
            RETURNING id
        """
        session_id = await db_manager.fetchval(
            query, user_id, server_id, complaint_url, complaint_number
        )
        logging.info(
            f"[PCH] Создана сессия #{session_id} ({complaint_number}) "
            f"для пользователя {user_id}, сервер {server_id}"
        )
        return cls(session_id)

    @classmethod
    async def get_active_session(cls, user_id: int) -> Optional['ProsecutionComplaintManager']:
        """Получает активную сессию пользователя."""
        query = """
            SELECT id FROM prosecution_complaint_sessions
            WHERE user_id = $1 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """
        session_id = await db_manager.fetchval(query, user_id)
        if session_id:
            return cls(session_id)
        return None

    @classmethod
    async def get_user_sessions(
        cls,
        user_id: int,
        limit: int = 10,
        include_statuses: List[str] = None
    ) -> List[Dict]:
        """Получает список сессий пользователя."""
        if include_statuses is None:
            include_statuses = ['active', 'completed', 'converted_to_case', 'expired']

        placeholders = ', '.join(f"${i+2}" for i in range(len(include_statuses)))
        query = f"""
            SELECT id, complaint_number, complaint_url, server_id, 
                   current_stage_index, status, linked_criminal_case_id,
                   created_at, updated_at
            FROM prosecution_complaint_sessions
            WHERE user_id = $1 AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ${len(include_statuses) + 2}
        """
        rows = await db_manager.fetch(query, user_id, *include_statuses, limit)
        return [dict(row) for row in rows]

    async def load_session_data(self) -> Dict:
        """Загружает данные сессии из БД."""
        if self._session_data is None:
            query = """
                SELECT * FROM prosecution_complaint_sessions WHERE id = $1
            """
            row = await db_manager.fetchrow(query, self.session_id)
            self._session_data = dict(row) if row else {}
        return self._session_data

    async def update_session(self, **kwargs) -> None:
        """Обновляет поля сессии."""
        set_clauses = []
        values = []
        idx = 1

        for key, value in kwargs.items():
            set_clauses.append(f"{key} = ${idx}")
            # Сериализуем dict/list в JSON
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
            idx += 1

        values.append(self.session_id)
        query = f"""
            UPDATE prosecution_complaint_sessions
            SET {', '.join(set_clauses)}, updated_at = NOW()
            WHERE id = ${idx}
        """
        await db_manager.execute(query, *values)
        self._session_data = None  # Сбрасываем кэш

    async def get_collected_data(self) -> Dict:
        """Получает собранные материалы дела."""
        session = await self.load_session_data()
        data = session.get('collected_data', {})
        if isinstance(data, str):
            return json.loads(data)
        return data or {}

    async def update_collected_data(self, new_data: Dict) -> None:
        """Обновляет собранные материалы."""
        current = await self.get_collected_data()
        current.update(new_data)
        await self.update_session(collected_data=current)

    async def get_parsed_complaint(self) -> Optional[Dict]:
        """Получает распарсенную жалобу."""
        session = await self.load_session_data()
        data = session.get('parsed_complaint')
        if isinstance(data, str):
            return json.loads(data)
        return data

    async def get_ai_recommendations(self) -> Optional[Dict]:
        """Получает рекомендации AI."""
        session = await self.load_session_data()
        data = session.get('ai_recommendations')
        if isinstance(data, str):
            return json.loads(data)
        return data

    async def get_form_compliance_status(self) -> Optional[Dict]:
        """Получает результаты скрининга формы подачи."""
        session = await self.load_session_data()
        data = session.get('form_compliance_status')
        if isinstance(data, str):
            return json.loads(data)
        return data

    async def save_stage_content(
        self,
        stage_type: str,
        content: str,
        ai_review_notes: str = None,
        is_final: bool = False
    ) -> int:
        """Сохраняет версию этапа обработки."""
        # Получаем текущую максимальную версию
        query = """
            SELECT COALESCE(MAX(version), 0) FROM prosecution_complaint_stages
            WHERE session_id = $1 AND stage_type = $2
        """
        max_version = await db_manager.fetchval(query, self.session_id, stage_type)
        new_version = max_version + 1

        # Вставляем новую версию
        insert_query = """
            INSERT INTO prosecution_complaint_stages 
            (session_id, stage_type, version, content, ai_review_notes, is_final)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """
        stage_id = await db_manager.fetchval(
            insert_query,
            self.session_id, stage_type, new_version, content, ai_review_notes, is_final
        )

        logging.info(
            f"[PCH] Сохранён этап {stage_type} v{new_version} для сессии #{self.session_id}"
        )
        return stage_id

    async def get_latest_stage_content(self, stage_type: str) -> Optional[str]:
        """Получает последнюю версию этапа."""
        query = """
            SELECT content FROM prosecution_complaint_stages
            WHERE session_id = $1 AND stage_type = $2
            ORDER BY version DESC LIMIT 1
        """
        return await db_manager.fetchval(query, self.session_id, stage_type)

    async def get_stage_with_notes(self, stage_type: str) -> Optional[Dict]:
        """Получает последнюю версию этапа с заметками AI."""
        query = """
            SELECT content, ai_review_notes, version, is_final, created_at
            FROM prosecution_complaint_stages
            WHERE session_id = $1 AND stage_type = $2
            ORDER BY version DESC LIMIT 1
        """
        row = await db_manager.fetchrow(query, self.session_id, stage_type)
        return dict(row) if row else None

    async def get_all_stages(self) -> Dict[str, Dict]:
        """Получает все финальные этапы."""
        query = """
            SELECT DISTINCT ON (stage_type) 
                stage_type, content, ai_review_notes, version, is_final
            FROM prosecution_complaint_stages
            WHERE session_id = $1
            ORDER BY stage_type, version DESC
        """
        rows = await db_manager.fetch(query, self.session_id)
        return {row['stage_type']: dict(row) for row in rows}

    async def mark_stage_final(self, stage_type: str) -> None:
        """Помечает последнюю версию этапа как финальную."""
        # Снимаем флаг is_final со всех версий этого этапа
        await db_manager.execute(
            "UPDATE prosecution_complaint_stages SET is_final = FALSE "
            "WHERE session_id = $1 AND stage_type = $2",
            self.session_id, stage_type
        )
        # Ставим флаг на последнюю версию
        await db_manager.execute("""
            UPDATE prosecution_complaint_stages SET is_final = TRUE
            WHERE id = (
                SELECT id FROM prosecution_complaint_stages
                WHERE session_id = $1 AND stage_type = $2
                ORDER BY version DESC LIMIT 1
            )
        """, self.session_id, stage_type)

    async def add_generated_document(
        self,
        doc_type: str,
        content: str,
        metadata: Dict = None
    ) -> None:
        """Добавляет сгенерированный документ в список."""
        session = await self.load_session_data()
        docs = session.get('generated_documents', [])
        if isinstance(docs, str):
            docs = json.loads(docs)

        doc_entry = {
            'type': doc_type,
            'type_display': PCH_DOCUMENT_TYPES.get(doc_type, doc_type),
            'content': content,
            'created_at': datetime.now().isoformat(),
            'metadata': metadata or {}
        }
        docs.append(doc_entry)

        await self.update_session(generated_documents=docs)
        logging.info(f"[PCH] Добавлен документ '{doc_type}' для сессии #{self.session_id}")

    async def get_generated_documents(self) -> List[Dict]:
        """Получает список всех сгенерированных документов."""
        session = await self.load_session_data()
        docs = session.get('generated_documents', [])
        if isinstance(docs, str):
            return json.loads(docs)
        return docs or []

    async def close_session(self, status: str = 'completed') -> None:
        """Закрывает сессию."""
        await self.update_session(status=status)
        logging.info(f"[PCH] Сессия #{self.session_id} закрыта со статусом '{status}'")

    async def link_criminal_case(self, criminal_case_id: int) -> None:
        """Связывает сессию с уголовным делом."""
        await self.update_session(
            linked_criminal_case_id=criminal_case_id,
            status='converted_to_case'
        )
        logging.info(
            f"[PCH] Сессия #{self.session_id} связана с уголовным делом #{criminal_case_id}"
        )


# === ФУНКЦИИ ДЛЯ РАБОТЫ С ФОРМАМИ ПОДАЧИ ===

async def get_form_template(user_id: int, server_id: str) -> Optional[str]:
    """Получает кэшированную форму подачи жалобы для пользователя и сервера."""
    query = """
        SELECT form_content FROM prosecution_form_templates
        WHERE user_id = $1 AND server_id = $2
    """
    return await db_manager.fetchval(query, user_id, server_id)


async def save_form_template(user_id: int, server_id: str, content: str) -> None:
    """Сохраняет форму подачи жалобы (upsert)."""
    query = """
        INSERT INTO prosecution_form_templates (user_id, server_id, form_content)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, server_id) 
        DO UPDATE SET form_content = $3, updated_at = NOW()
    """
    await db_manager.execute(query, user_id, server_id, content)
    logging.info(f"[PCH] Сохранена форма подачи для user={user_id}, server={server_id}")


async def delete_form_template(user_id: int, server_id: str) -> None:
    """Удаляет кэшированную форму подачи."""
    query = """
        DELETE FROM prosecution_form_templates
        WHERE user_id = $1 AND server_id = $2
    """
    await db_manager.execute(query, user_id, server_id)
