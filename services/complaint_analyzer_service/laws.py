"""Подбор релевантных законов для анализа жалоб (RAG и полный текст), включая CMS."""

import asyncio
import logging
from typing import List, Tuple
from google.genai import errors as genai_errors
from google.genai import types
from google.api_core import exceptions as google_exceptions

from database.manager import db_manager
from common.rate_limiter import (
    execute_vertex_request,
    RequestPriority,
    get_current_client,
    mark_current_region_success,
    mark_current_region_error,
)

logger = logging.getLogger('services.complaint_analyzer_service')


# === AI-АНАЛИЗ ===

async def _get_relevant_laws_with_rag(context_text: str, server_id: str) -> List[types.Part]:
    """
    RAG-фильтр законов: AI-роутер выбирает релевантные законы по контексту.

    Адаптация из qa_service._get_relevant_laws() — вместо вопроса
    используется контекст документа (тип + описание ситуации + текст жалобы).

    Args:
        context_text: Контекст для выбора законов (описание ситуации, текст жалобы)
        server_id: ID сервера

    Returns:
        Список types.Part с gemini_file_uri релевантных законов
    """
    from config import ROUTER_MODEL_NAME
    from prompts import get_document_law_router_prompt

    start_time = asyncio.get_running_loop().time()
    logger.info(f"[COMPLAINT_ANALYZER_RAG] Запуск RAG-фильтра законов для сервера {server_id}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        if not law_records:
            logger.warning(f"[COMPLAINT_ANALYZER_RAG] В БД отсутствуют документы для сервера {server_id}.")
            return []

        # Создаём карту законов по индексу
        law_map_by_index = {
            i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']}
            for i, r in enumerate(law_records)
        }
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        # Формируем промпт для роутера
        router_prompt = get_document_law_router_prompt(
            context_text=context_text[:3000],  # Ограничиваем размер контекста
            law_titles_str=law_titles_str
        )

        # Функция запроса к AI-роутеру
        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        # Выполняем запрос через rate limiter
        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        # Парсим ответ (номера законов)
        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(
            f"[COMPLAINT_ANALYZER_RAG] RAG-фильтр выбрал {len(relevant_laws)} законов за {duration:.2f} мс. "
            f"Законы: {[law['title'] for law in relevant_laws]}"
        )

        # Возвращаем Part объекты с URI
        return [types.Part.from_uri(file_uri=law['uri'], mime_type="text/plain") for law in relevant_laws]

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.warning(f"[COMPLAINT_ANALYZER_RAG] Ошибка RAG-фильтра: {e}. Возврат пустого списка.")
        return []
    except Exception as e:
        logger.error(f"[COMPLAINT_ANALYZER_RAG] Критическая ошибка RAG-фильтра.", exc_info=e)
        return []


async def _get_relevant_laws_with_rag_extended(context_text: str, server_id: str) -> Tuple[List[types.Part], List[str]]:
    """
    RAG-фильтр законов: возвращает и Part объекты, и названия законов.

    Расширенная версия _get_relevant_laws_with_rag() для CMS API,
    которая также возвращает названия выбранных законов для ответа клиенту.

    Args:
        context_text: Контекст для выбора законов (описание ситуации, текст жалобы)
        server_id: ID сервера

    Returns:
        Tuple[List[types.Part], List[str]]: (law_parts для контекста AI, law_titles для ответа)
    """
    from config import ROUTER_MODEL_NAME
    from prompts import get_document_law_router_prompt

    start_time = asyncio.get_running_loop().time()
    logger.info(f"[RAG_EXTENDED] Запуск RAG-фильтра законов для сервера {server_id}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        if not law_records:
            logger.warning(f"[RAG_EXTENDED] В БД отсутствуют документы для сервера {server_id}.")
            return [], []

        # Создаём карту законов по индексу
        law_map_by_index = {
            i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']}
            for i, r in enumerate(law_records)
        }
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        # Формируем промпт для роутера
        router_prompt = get_document_law_router_prompt(
            context_text=context_text[:3000],
            law_titles_str=law_titles_str
        )

        # Функция запроса к AI-роутеру
        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        # Выполняем запрос через rate limiter
        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        # Парсим ответ (номера законов)
        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(
            f"[RAG_EXTENDED] RAG-фильтр выбрал {len(relevant_laws)} законов за {duration:.2f} мс. "
            f"Законы: {[law['title'] for law in relevant_laws]}"
        )

        # Возвращаем и Parts, и названия
        law_parts = [types.Part.from_uri(file_uri=law['uri'], mime_type="text/plain") for law in relevant_laws]
        law_titles = [law['title'] for law in relevant_laws]

        return law_parts, law_titles

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.warning(f"[RAG_EXTENDED] Ошибка RAG-фильтра: {e}. Возврат пустых списков.")
        return [], []
    except Exception as e:
        logger.error(f"[RAG_EXTENDED] Критическая ошибка RAG-фильтра.", exc_info=e)
        return [], []


async def _get_relevant_laws(server_id: str) -> str:
    """
    Получает все законы для сервера (упрощённая версия без RAG-фильтра).

    Используется в analyze_complaint и generate_complaint_document_text
    где нужен полный контекст законов.

    Args:
        server_id: ID сервера

    Returns:
        Строка с текстами законов (первые N документов)
    """
    try:
        law_records = await db_manager.fetch(
            """SELECT title, content 
               FROM law_documents 
               WHERE server_id = $1 AND content IS NOT NULL
               ORDER BY title
               LIMIT 10""",
            server_id
        )

        if not law_records:
            logger.warning(f"[COMPLAINT_ANALYZER] Нет законов для сервера {server_id}")
            return ""

        laws_text = "\n\n---\n\n".join([
            f"# {r['title']}\n{r['content'][:5000]}"
            for r in law_records
        ])

        logger.info(f"[COMPLAINT_ANALYZER] Загружено {len(law_records)} законов для {server_id}")
        return laws_text[:30000]  # Ограничиваем общий размер

    except Exception as e:
        logger.error(f"[COMPLAINT_ANALYZER] Ошибка загрузки законов: {e}")
        return ""


# === CMS MIGRATION: New AI Functions ===

# === RAG-фильтр законов для CMS ===

async def _get_relevant_laws_for_cms(complaint_text: str, server_id: str) -> str:
    """
    RAG-фильтр законов для CMS API: выбирает релевантные законы по тексту жалобы.

    Алгоритм:
    1. Загружает все законы для server_id из БД (law_documents)
    2. Использует AI-роутер для выбора релевантных законов (3-8 шт)
    3. Возвращает текст выбранных законов

    Args:
        complaint_text: Текст жалобы (заголовок + контент)
        server_id: ID сервера

    Returns:
        Строка с текстами релевантных законов или пустая строка
    """
    from config import ROUTER_MODEL_NAME
    from api.prompts.law_router import get_cms_law_router_prompt

    start_time = asyncio.get_running_loop().time()
    logger.info(f"[CMS_RAG] Запуск RAG-фильтра законов для сервера {server_id}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            """SELECT id, title, content 
               FROM law_documents 
               WHERE server_id = $1 AND content IS NOT NULL 
               ORDER BY title""",
            server_id
        )

        if not law_records:
            logger.warning(f"[CMS_RAG] Нет законов в БД для сервера {server_id}")
            return ""

        # Создаём карту законов по индексу
        law_map_by_index = {
            i + 1: {
                'id': r['id'],
                'title': r['title'],
                'content': r['content']
            }
            for i, r in enumerate(law_records)
        }

        # Формируем список названий для роутера
        law_titles_str = "\n".join([
            f"{i + 1}. {r['title']}"
            for i, r in enumerate(law_records)
        ])

        logger.info(f"[CMS_RAG] Загружено {len(law_records)} законов для сервера {server_id}")

        # Формируем промпт для роутера
        router_prompt = get_cms_law_router_prompt(
            complaint_text=complaint_text[:3000],
            law_titles_str=law_titles_str
        )

        # Функция запроса к AI-роутеру
        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        # Выполняем запрос через rate limiter
        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        # Парсим ответ (номера законов)
        selected_indices = set()
        for line in response.text.split('\n'):
            line = line.strip()
            if line.isdigit():
                selected_indices.add(int(line))

        relevant_laws = [
            law_map_by_index[idx]
            for idx in sorted(selected_indices)
            if idx in law_map_by_index
        ]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(
            f"[CMS_RAG] RAG-фильтр выбрал {len(relevant_laws)} законов за {duration:.2f} мс. "
            f"Законы: {[law['title'] for law in relevant_laws]}"
        )

        if not relevant_laws:
            # Fallback: если роутер не выбрал ничего, берём первые 5 законов
            relevant_laws = [law_map_by_index[i] for i in range(1, min(6, len(law_records) + 1))]
            logger.info(f"[CMS_RAG] Fallback: используем первые {len(relevant_laws)} законов")

        # Формируем текст законов
        laws_text = "\n\n---\n\n".join([
            f"### {law['title']}\n{law['content'][:4000]}"
            for law in relevant_laws
        ])

        return laws_text[:25000]  # Ограничиваем общий размер

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.warning(f"[CMS_RAG] Ошибка RAG-фильтра: {e}. Возврат без законов.")
        return ""
    except Exception as e:
        logger.error(f"[CMS_RAG] Критическая ошибка RAG-фильтра: {e}", exc_info=True)
        return ""
