"""AI-конвейер: подбор законов, парсинг жалобы, фазы анализа, генерация документов, перевод в УД."""

import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from google.genai import types
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

import config
from config import PCH_MODEL_NAME, PCH_STAGES_ORDER, PCH_DOCUMENT_TYPES, PCH_STAGE_AVAILABLE_DOCS
from database.manager import db_manager
from prompts import (
    get_pch_intake_prompt,
    get_pch_planning_prompt,
    get_pch_clarification_prompt,
    get_pch_finalization_prompt,
    get_pch_document_prompt,
)
from common.rate_limiter import execute_vertex_request, RequestPriority, get_current_client
from knowledge.remote_parser import remote_parse_topic, check_remote_parser_available

from .session import ProsecutionComplaintManager, get_form_template


# === ФУНКЦИИ ПОЛУЧЕНИЯ ЗАКОНОВ (RAG) ===

async def get_relevant_laws_for_complaint(
    server_id: str,
    complaint_text: str,
    stage_name: str = 'intake'
) -> str:
    """
    Получает релевантные законы для жалобы используя RAG-систему.

    Алгоритм:
    1. Загружает все законы сервера из БД
    2. Использует AI-роутер для выбора релевантных законов
    3. Возвращает текст выбранных законов

    Args:
        server_id: ID сервера
        complaint_text: Текст жалобы для анализа
        stage_name: Название этапа (intake, planning, clarification, finalization)

    Returns:
        Форматированный текст релевантных законов
    """
    from prompts import get_pch_law_router_prompt

    try:
        # Шаг 1: Загружаем все законы сервера
        law_records = await db_manager.fetch(
            """SELECT id, title, content FROM law_documents 
               WHERE server_id = $1 AND content IS NOT NULL 
               ORDER BY title""",
            server_id
        )

        if not law_records:
            logging.warning(f"[PCH RAG] Законы для сервера {server_id} не найдены в БД")
            return "⚠️ Законы не найдены в базе данных для данного сервера."

        # Создаём маппинг номер -> закон
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

        logging.info(f"[PCH RAG] Загружено {len(law_records)} законов для сервера {server_id}")

        # Шаг 2: Используем AI-роутер для выбора релевантных законов
        router_prompt = get_pch_law_router_prompt(
            complaint_text=complaint_text,
            stage_name=stage_name,
            law_titles_str=law_titles_str
        )

        try:
            async def make_router_request():
                client = get_current_client()
                if not client:
                    raise RuntimeError("genai_client не инициализирован")
                return await client.aio.models.generate_content(
                    model=config.ROUTER_MODEL_NAME,  # Используем быструю модель для роутера
                    contents=router_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,  # Низкая температура для точности
                        max_output_tokens=200,
                    )
                )

            response = await execute_vertex_request(
                func=make_router_request,
                model_name=config.ROUTER_MODEL_NAME,
                priority=RequestPriority.NORMAL,
                max_retries=2
            )

            # Парсим ответ роутера
            selected_indices = set()
            for line in response.text.strip().split('\n'):
                line = line.strip()
                if line.isdigit():
                    idx = int(line)
                    if idx in law_map_by_index:
                        selected_indices.add(idx)

            logging.info(f"[PCH RAG] Роутер выбрал {len(selected_indices)} законов: {sorted(selected_indices)}")

            # Если роутер не выбрал ничего, используем fallback
            if not selected_indices:
                logging.warning("[PCH RAG] Роутер не выбрал законы, используем приоритетные")
                selected_indices = await _get_priority_law_indices(law_map_by_index)

        except Exception as e:
            logging.warning(f"[PCH RAG] Ошибка роутера: {e}, используем приоритетные законы")
            selected_indices = await _get_priority_law_indices(law_map_by_index)

        # Шаг 3: Формируем текст выбранных законов
        if not selected_indices:
            # Если совсем ничего не выбрано, берём первые 5 законов
            selected_indices = set(list(law_map_by_index.keys())[:5])

        laws_text = []
        total_length = 0
        max_total_length = 20000  # Лимит на общий размер законов

        for idx in sorted(selected_indices):
            law = law_map_by_index[idx]
            title = law['title']
            content = law['content']

            # Ограничиваем размер каждого закона
            if len(content) > 6000:
                content = content[:6000] + "\n\n[... текст сокращён ...]"

            law_block = f"\n\n{'='*50}\n📜 {title}\n{'='*50}\n\n{content}"

            # Проверяем общий лимит
            if total_length + len(law_block) > max_total_length:
                laws_text.append("\n\n⚠️ [Остальные законы опущены из-за ограничения размера]")
                break

            laws_text.append(law_block)
            total_length += len(law_block)

        result = ''.join(laws_text)
        logging.info(f"[PCH RAG] Сформирован текст законов: {len(result)} символов, {len(selected_indices)} документов")

        return result

    except Exception as e:
        logging.error(f"[PCH RAG] Критическая ошибка: {e}", exc_info=True)
        return "❌ Ошибка загрузки законов из базы данных."


async def _get_priority_law_indices(law_map: dict) -> set:
    """
    Возвращает индексы приоритетных законов (УК, УПК, процессуальные).
    Используется как fallback когда роутер не работает.
    """
    priority_keywords = [
        'уголов', 'процессуальн', 'УК', 'УПК', 'прокурор',
        'надзор', 'полиц', 'должност', 'административ'
    ]

    selected = set()
    for idx, law in law_map.items():
        title_lower = law['title'].lower()
        for keyword in priority_keywords:
            if keyword.lower() in title_lower:
                selected.add(idx)
                break
        if len(selected) >= 7:  # Максимум 7 приоритетных
            break

    return selected


# === ЧАСТЬ 2: ПАРСИНГ И ГЕНЕРАЦИЯ ===

# === ФУНКЦИИ ПАРСИНГА ЖАЛОБ ===

async def parse_complaint_from_url(url: str) -> Tuple[bool, Optional[Dict], str]:
    """
    Парсит жалобу с форума по URL.

    Returns:
        Tuple[success, parsed_data, error_message]
    """
    # Проверяем доступность парсера
    parser_available = await check_remote_parser_available()
    if not parser_available:
        return False, None, "Сервис парсинга временно недоступен. Попробуйте позже."

    try:
        # Парсим тему форума (все посты, не только автора).
        # content_cleanup=False: цитаты в жалобах — содержимое, не мусор.
        result = await remote_parse_topic(url, parse_only_author=False, content_cleanup=False)

        if not result:
            return False, None, "Не удалось загрузить тему с форума. Проверьте ссылку."

        parsed_data = {
            'title': result.get('title', 'Без названия'),
            'content': result.get('content', ''),
            'thread_id': result.get('thread_id'),
            'url': url,
            'parsed_at': datetime.now().isoformat()
        }

        # Извлекаем информацию о заявителе из первого поста
        content = parsed_data['content']
        if content:
            # Пробуем найти ник заявителя в тексте
            applicant_match = re.search(
                r'(?:Заявитель|От кого|ФИО|Ник)[:\s]+([A-Za-z_]+\s*[A-Za-z_]*)',
                content, re.IGNORECASE
            )
            if applicant_match:
                parsed_data['applicant'] = applicant_match.group(1).strip()

        logging.info(f"[PCH] Успешно распарсена жалоба: {parsed_data['title']}")
        return True, parsed_data, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка парсинга жалобы {url}: {e}")
        return False, None, f"Ошибка при парсинге: {str(e)[:100]}"


# === ФУНКЦИИ ГЕНЕРАЦИИ AI ===

async def run_intake_analysis(
    session_id: int,
    parsed_complaint: Dict,
    form_template: Optional[str],
    relevant_laws: str
) -> Tuple[bool, str, str]:
    """
    Фаза A: Приём и первичный анализ жалобы.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)

    # Формируем текст жалобы для анализа
    complaint_text = f"Заголовок: {parsed_complaint.get('title', 'N/A')}\n\n"
    complaint_text += parsed_complaint.get('content', 'Текст жалобы не найден')

    prompt = get_pch_intake_prompt(
        complaint_text=complaint_text,
        form_template=form_template or "Форма подачи не предоставлена",
        relevant_laws=relevant_laws
    )

    try:
        async def make_intake_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4000,
                )
            )

        response = await execute_vertex_request(
            func=make_intake_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем результаты в сессию
        await manager.update_session(
            summary=content[:2000],  # Сохраняем краткое резюме
            parsed_complaint=parsed_complaint
        )

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='intake',
            content=content,
            ai_review_notes="Первичный анализ завершён"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка intake analysis: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def run_planning_phase(session_id: int) -> Tuple[bool, str, str]:
    """
    Фаза B: Составление плана действий.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    # Получаем данные для анализа
    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()
    server_id = session.get('server_id')

    # Получаем релевантные законы через RAG-систему
    parsed_complaint = await manager.get_parsed_complaint()
    complaint_text = parsed_complaint.get('content', '') if parsed_complaint else summary
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='planning')

    prompt = get_pch_planning_prompt(
        summary=summary,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        relevant_laws=relevant_laws
    )

    try:
        async def make_planning_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4000,
                )
            )

        response = await execute_vertex_request(
            func=make_planning_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='planning',
            content=content,
            ai_review_notes="План действий сформирован"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка planning phase: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def run_clarification_phase(session_id: int) -> Tuple[bool, str, str]:
    """
    Фаза C: Генерация уточняющих вопросов.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()

    # Получаем результаты предыдущих этапов
    planning_result = await manager.get_latest_stage_content('planning') or ''

    # Определяем недостающую информацию на основе плана
    missing_info = "На основании плана действий определить недостающие данные"

    prompt = get_pch_clarification_prompt(
        summary=summary,
        planning_result=planning_result,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        missing_info=missing_info
    )

    try:
        async def make_clarification_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=2000,
                )
            )

        response = await execute_vertex_request(
            func=make_clarification_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='clarification',
            content=content,
            ai_review_notes="Уточняющие вопросы сформулированы"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка clarification phase: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def run_finalization_phase(session_id: int) -> Tuple[bool, str, str]:
    """
    Фаза D: Завершение и принятие решения.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()
    server_id = session.get('server_id')

    # Получаем все предыдущие этапы
    planning_result = await manager.get_latest_stage_content('planning') or ''
    clarifications = await manager.get_latest_stage_content('clarification') or ''

    # Получаем законы через RAG-систему
    parsed_complaint = await manager.get_parsed_complaint()
    complaint_text = parsed_complaint.get('content', '') if parsed_complaint else summary
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='finalization')

    prompt = get_pch_finalization_prompt(
        summary=summary,
        planning_result=planning_result,
        clarifications=clarifications,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        relevant_laws=relevant_laws
    )

    try:
        async def make_finalization_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4000,
                )
            )

        response = await execute_vertex_request(
            func=make_finalization_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='finalization',
            content=content,
            ai_review_notes="Финальное решение подготовлено"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка finalization phase: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def regenerate_stage(session_id: int, stage_type: str, user_feedback: str = None) -> Tuple[bool, str, str]:
    """
    Перегенерирует этап с учётом обратной связи пользователя.

    Returns:
        Tuple[success, content, ai_notes]
    """
    # Определяем какую функцию вызвать
    stage_runners = {
        'intake': run_intake_analysis,
        'planning': run_planning_phase,
        'clarification': run_clarification_phase,
        'finalization': run_finalization_phase
    }

    if stage_type not in stage_runners:
        return False, "", f"Неизвестный тип этапа: {stage_type}"

    # Если есть фидбек, добавляем его в collected_data
    if user_feedback:
        manager = ProsecutionComplaintManager(session_id)
        await manager.update_collected_data({
            f'{stage_type}_feedback': user_feedback,
            f'{stage_type}_feedback_at': datetime.now().isoformat()
        })

    # Для intake нужны дополнительные параметры
    if stage_type == 'intake':
        manager = ProsecutionComplaintManager(session_id)
        session = await manager.load_session_data()
        parsed_complaint = await manager.get_parsed_complaint()
        server_id = session.get('server_id')
        user_id = session.get('user_id')

        form_template = await get_form_template(user_id, server_id)
        complaint_text = parsed_complaint.get('content', '') if parsed_complaint else ''
        relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text)

        return await run_intake_analysis(session_id, parsed_complaint, form_template, relevant_laws)

    # Для остальных этапов
    return await stage_runners[stage_type](session_id)


# === ФУНКЦИИ ГЕНЕРАЦИИ ДОКУМЕНТОВ ===

async def generate_document(
    session_id: int,
    doc_type: str,
    additional_instructions: str = ""
) -> Tuple[bool, str, str]:
    """
    Генерирует документ указанного типа.

    Returns:
        Tuple[success, document_content, error_message]
    """
    # Разрешаем как стандартные типы, так и 'custom' для кастомных запросов
    if doc_type != 'custom' and doc_type not in PCH_DOCUMENT_TYPES:
        return False, "", f"Неизвестный тип документа: {doc_type}"

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()
    server_id = session.get('server_id')

    # Получаем релевантные законы через RAG-систему
    parsed_complaint = await manager.get_parsed_complaint()
    complaint_text = parsed_complaint.get('content', '') if parsed_complaint else summary
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='document')

    prompt = get_pch_document_prompt(
        document_type=doc_type,
        summary=summary,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        relevant_laws=relevant_laws,
        additional_instructions=additional_instructions
    )

    try:
        async def make_document_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,  # Низкая температура для документов
                    max_output_tokens=3000,
                )
            )

        response = await execute_vertex_request(
            func=make_document_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем документ в сессию
        await manager.add_generated_document(
            doc_type=doc_type,
            content=content,
            metadata={'additional_instructions': additional_instructions}
        )

        logging.info(f"[PCH] Сгенерирован документ '{doc_type}' для сессии #{session_id}")
        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка генерации документа: {e}")
        return False, "", f"Ошибка генерации: {str(e)[:100]}"


def get_available_documents_for_stage(stage_index: int) -> List[str]:
    """Возвращает список доступных типов документов для текущего этапа."""
    if stage_index >= len(PCH_STAGES_ORDER):
        return []

    stage_type = PCH_STAGES_ORDER[stage_index]
    return PCH_STAGE_AVAILABLE_DOCS.get(stage_type, [])


def build_document_selection_keyboard(
    session_id: int,
    available_docs: List[str]
) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для выбора типа документа."""
    keyboard = []

    for doc_type in available_docs:
        doc_name = PCH_DOCUMENT_TYPES.get(doc_type, doc_type)
        keyboard.append([
            InlineKeyboardButton(
                f"📄 {doc_name}",
                callback_data=f"pch_doc_{doc_type}_{session_id}"
            )
        ])

    # Добавляем кнопку для кастомного документа
    keyboard.append([
        InlineKeyboardButton(
            "✏️ Свой запрос (написать вручную)",
            callback_data=f"pch_custom_doc_{session_id}"
        )
    ])

    keyboard.append([
        InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")
    ])

    return InlineKeyboardMarkup(keyboard)


# === ИНТЕГРАЦИЯ С CRIMINAL CASE WRITER ===

async def convert_to_criminal_case(session_id: int, context) -> Tuple[bool, int, str]:
    """
    Конвертирует жалобу в уголовное дело через CCW.

    Returns:
        Tuple[success, criminal_case_id, error_message]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    user_id = session.get('user_id')
    server_id = session.get('server_id')
    complaint_number = session.get('complaint_number', '')

    # Собираем все материалы жалобы
    collected_data = await manager.get_collected_data()
    parsed_complaint = await manager.get_parsed_complaint()
    all_stages = await manager.get_all_stages()
    generated_docs = await manager.get_generated_documents()

    # Формируем данные для уголовного дела
    case_facts = f"""
МАТЕРИАЛЫ ИЗ ЖАЛОБЫ {complaint_number}

=== ИСХОДНАЯ ЖАЛОБА ===
{parsed_complaint.get('content', 'Не найдено') if parsed_complaint else 'Не найдено'}

=== РЕЗЮМЕ (Phase A) ===
{session.get('summary', 'Не сформировано')}

=== ПЛАН ДЕЙСТВИЙ (Phase B) ===
{all_stages.get('planning', {}).get('content', 'Не сформирован')}

=== УТОЧНЕНИЯ (Phase C) ===
{all_stages.get('clarification', {}).get('content', 'Не проводились')}

=== ФИНАЛЬНОЕ РЕШЕНИЕ (Phase D) ===
{all_stages.get('finalization', {}).get('content', 'Не принято')}

=== ДОПОЛНИТЕЛЬНЫЕ МАТЕРИАЛЫ ===
{json.dumps(collected_data, ensure_ascii=False, indent=2)}
"""

    try:
        # Импортируем CCW менеджер
        from .criminal_case_flow import CriminalCaseManager, schedule_ccw_session_closure

        # Создаём сессию CCW
        ccw_manager = await CriminalCaseManager.create_session(user_id, server_id)
        ccw_session_id = ccw_manager.session_id

        # Генерируем номер дела на основе номера жалобы
        # PCH-2026-0042 -> DJR-042
        case_number = complaint_number.replace('PCH-', 'DJR-').split('-')[-1]
        case_number = f"DJR-{case_number}"

        await ccw_manager.update_session(case_number=case_number)
        await ccw_manager.update_collected_data({
            'initial_facts': case_facts,
            'source': 'prosecution_complaint',
            'source_session_id': session_id,
            'source_complaint_number': complaint_number,
            'input_timestamp': datetime.now().isoformat()
        })

        # Связываем жалобу с уголовным делом
        await manager.link_criminal_case(ccw_session_id)

        # Планируем автозакрытие CCW сессии
        schedule_ccw_session_closure(context, ccw_session_id, user_id)

        logging.info(
            f"[PCH] Жалоба #{session_id} конвертирована в уголовное дело #{ccw_session_id}"
        )
        return True, ccw_session_id, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка конвертации в уголовное дело: {e}")
        return False, 0, f"Ошибка: {str(e)[:100]}"
