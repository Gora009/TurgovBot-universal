"""CMS: ответ на произвольный запрос."""

import logging
from typing import Optional, Dict
from google.genai import errors as genai_errors
from google.genai import types
from google.api_core import exceptions as google_exceptions

from config import COMPLAINT_ANALYZER_MODEL_NAME
from common.rate_limiter import (
    execute_vertex_request,
    RequestPriority,
    get_current_client,
    mark_current_region_success,
    mark_current_region_error,
)
from common.utils import reinitialize_vertex_ai

logger = logging.getLogger('services.complaint_analyzer_service')


async def generate_free_form_response_cms(
    server_id: str,
    complaint_number: str,
    complainant: str,
    complaint_summary: str,
    prosecutor_instructions: str,
    use_laws_rag: bool,
    attorney: str,
    rank: str,
    regenerate_feedback: Optional[str] = None
) -> Dict:
    """
    Генерация ответа в свободной форме (option_5) через AI.

    AI создаёт официальный ответ на русском языке по инструкциям прокурора.

    Args:
        server_id: ID сервера для RAG законов
        complaint_number: Номер обращения
        complainant: ФИО заявителя
        complaint_summary: Краткое содержание жалобы
        prosecutor_instructions: Инструкции прокурора (что ответить)
        use_laws_rag: Использовать RAG законов для генерации
        attorney: ФИО прокурора
        rank: Должность прокурора
        regenerate_feedback: Что исправить при перегенерации (опционально)

    Returns:
        Dict: {main_text: str}

    Raises:
        RuntimeError: При ошибке AI
    """
    logger.info(f"[CMS_FREE_FORM] Генерация свободной формы: complaint={complaint_number}, use_rag={use_laws_rag}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # RAG законов пока не реализован в свободной форме - укажем это в промпте
    law_context = ""
    if use_laws_rag:
        law_context = "\n\n[AI: Прокурор запросил использование законодательной базы. Постарайся ссылаться на релевантные статьи УК, АК, ЭК штата Сан-Андреас.]"
        logger.info(f"[CMS_FREE_FORM] RAG законов запрошен (будет использован контекст AI)")

    # Формируем промпт
    prompt = _build_free_form_prompt(
        complaint_number=complaint_number,
        complainant=complainant,
        complaint_summary=complaint_summary,
        prosecutor_instructions=prosecutor_instructions,
        attorney=attorney,
        rank=rank,
        law_context=law_context,
        regenerate_feedback=regenerate_feedback
    )

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=2000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        main_text = response.text.strip()

        # Убираем markdown если есть
        if main_text.startswith("```"):
            lines = main_text.split('\n')
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            main_text = '\n'.join(lines).strip()

        logger.info(f"[CMS_FREE_FORM] Успешно сгенерировано: {len(main_text)} символов")

        return {'main_text': main_text}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FREE_FORM] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FREE_FORM] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


def _build_free_form_prompt(
    complaint_number: str,
    complainant: str,
    complaint_summary: str,
    prosecutor_instructions: str,
    attorney: str,
    rank: str,
    law_context: str = "",
    regenerate_feedback: Optional[str] = None
) -> str:
    """
    Формирует промпт для генерации свободной формы ответа.
    """
    rank_mapping = {
        'Прокурор': 'Прокурором',
        'Заместитель Генерального Прокурора': 'Заместителем Генерального Прокурора',
        'Генеральный Прокурор': 'Генеральным Прокурором'
    }
    rank_inst = rank_mapping.get(rank, rank)

    # Get first name safely
    first_name = 'гражданин'
    if complainant and complainant.strip():
        parts = complainant.strip().split()
        if parts:
            first_name = parts[0]

    base_prompt = f"""Ты — опытный прокурор Офиса Генерального Прокурора штата Сан-Андреас.

Твоя задача: написать официальный ответ на обращение гражданина в свободной форме.

=== ДАННЫЕ ОБРАЩЕНИЯ ===
Номер обращения: №{complaint_number}
Заявитель: {complainant}
Суть обращения: {complaint_summary}

=== ДАННЫЕ ПРОКУРОРА ===
ФИО: {attorney}
Должность: {rank}
{law_context}

=== ИНСТРУКЦИИ ПРОКУРОРА ===
{prosecutor_instructions}

=== ТРЕБОВАНИЯ К ОТВЕТУ ===
1. Ответ должен быть на русском языке
2. Официально-деловой стиль
3. Структура:
   - Обращение к заявителю ("Уважаемый/ая {first_name}")
   - Указание что обращение №{complaint_number} рассмотрено {rank_inst} {attorney}
   - Суть ответа согласно инструкциям прокурора
   - Информация о праве на обжалование (если уместно)
   - Подпись: "С уважением, {rank} {attorney}"

4. НЕ добавляй шапку документа (дату, адрес) — это добавится автоматически
5. Ответ должен быть конкретным, без воды
6. Используй законы и статьи если они предоставлены в контексте
7. Ответ НЕ ДОЛЖЕН превышать 600 символов. Если содержание требует больше — сократи, сохранив ключевую информацию. Это ограничение размера документа.

Напиши только текст ответа, без дополнительных комментариев."""

    if regenerate_feedback:
        base_prompt += f"""

=== ИСПРАВЛЕНИЯ (ПЕРЕГЕНЕРАЦИЯ) ===
Предыдущая версия ответа не удовлетворила прокурора. 
Учти следующие замечания при генерации нового ответа:
{regenerate_feedback}"""

    return base_prompt
