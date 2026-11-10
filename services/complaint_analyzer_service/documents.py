"""Генерация текстов документов: ответ на жалобу, документ АГО, произвольное постановление."""

import json
import logging
from typing import Optional, Dict
from google.genai import errors as genai_errors
from google.genai import types
from google.api_core import exceptions as google_exceptions

from config import COMPLAINT_ANALYZER_MODEL_NAME
from prompts import get_ago_generation_prompt, get_custom_resolution_prompt
from common.rate_limiter import (
    execute_vertex_request,
    RequestPriority,
    get_current_client,
    mark_current_region_success,
    mark_current_region_error,
)
from common.utils import reinitialize_vertex_ai

from .analysis import _parse_json_response
from .laws import _get_relevant_laws, _get_relevant_laws_with_rag

logger = logging.getLogger('services.complaint_analyzer_service')


# === ГЕНЕРАЦИЯ ТЕКСТА ДОКУМЕНТОВ ===

async def generate_complaint_document_text(
    server_id: str,
    document_type: str,
    case_number: str,
    complainant: str,
    attorney: str,
    rank: str,
    complaint_text: str = None,
    complaint_summary: str = None,
    additional_data: dict = None
) -> Dict:
    """
    Генерирует текст документа ответа на жалобу через AI.

    Args:
        server_id: ID сервера
        document_type: 'accept', 'reject', 'close'
        case_number: Номер обращения
        complainant: ФИО заявителя
        attorney: ФИО прокурора
        rank: Должность прокурора
        complaint_text: Текст жалобы
        complaint_summary: AI-резюме жалобы
        additional_data: Доп. данные (rejection_issues, defendant, charges и т.д.)

    Returns:
        Dict с text_blocks (header_0, main_0, main_1, bottom_0) и raw_text
    """
    from prompts import get_complaint_response_generation_prompt

    logger.info(f"[COMPLAINT_ANALYZER] Генерация документа '{document_type}' для обращения {case_number}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем контекст жалобы
    complaint_context = complaint_summary or complaint_text or "Контекст не предоставлен"

    # Получаем релевантные законы
    relevant_laws = await _get_relevant_laws(server_id)

    # Формируем дополнительные данные как строку
    additional_data_str = json.dumps(additional_data or {}, ensure_ascii=False, indent=2)

    prompt = get_complaint_response_generation_prompt(
        document_type=document_type,
        case_number=case_number,
        complainant=complainant,
        attorney=attorney,
        rank=rank,
        complaint_context=complaint_context,
        relevant_laws=relevant_laws,
        additional_data=additional_data_str
    )

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2048
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON из ответа
        result = _parse_json_response(response.text)

        # Валидируем наличие всех полей
        required_fields = ['header_0', 'main_0', 'main_1', 'bottom_0']
        for field in required_fields:
            if field not in result:
                result[field] = ''

        # Формируем raw_text
        raw_text = f"{result.get('main_0', '')}\n\n{result.get('main_1', '')}\n\n{result.get('bottom_0', '')}"

        logger.info(f"[COMPLAINT_ANALYZER] Документ '{document_type}' успешно сгенерирован")

        return {
            'text_blocks': {
                'header_0': result.get('header_0', ''),
                'main_0': result.get('main_0', ''),
                'main_1': result.get('main_1', ''),
                'bottom_0': result.get('bottom_0', '')
            },
            'raw_text': raw_text
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] Ошибка генерации документа: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации документа: {str(e)}")


# === AGO DOCUMENT AI GENERATION ===

async def generate_ago_document_text(
    server_id: str,
    ago_type: str,
    form_data: Dict,
    situation_description: str = None,
    complaint_id: Optional[int] = None
) -> Dict:
    """
    Генерирует текст AGO-документа (постановления) через AI с RAG-фильтрацией законов.

    Args:
        server_id: ID сервера для загрузки законов
        ago_type: Тип постановления (REQUEST_INFO, INITIATE_CASE, CLOSE_CASE,
                  FINE_ORDER, DISMISSAL_ORDER, INTERROGATION_ORDER, CANCEL_ORDER)
        form_data: Данные формы (attorney, rank, defendant, complainant, organization и т.д.)
        situation_description: Описание ситуации / контекст (опционально)
        complaint_id: ID привязанного обращения (опционально, для будущего расширения)

    Returns:
        Dict: {title: str, main_text: str}

    Raises:
        RuntimeError: При ошибке AI или аутентификации
    """
    logger.info(f"[AGO_GENERATOR] Генерация AGO '{ago_type}' для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем контекст для RAG-фильтра
    context_for_rag = f"Тип документа: {ago_type}\n"
    if situation_description:
        context_for_rag += f"Описание ситуации: {situation_description}\n"
    if form_data.get('charges'):
        context_for_rag += f"Статья: {form_data.get('charges')}\n"
    if form_data.get('charges_type'):
        context_for_rag += f"Тип обвинения: {form_data.get('charges_type')}\n"

    # Получаем релевантные законы через RAG
    law_parts = await _get_relevant_laws_with_rag(context_for_rag, server_id)

    logger.info(f"[AGO_GENERATOR] RAG вернул {len(law_parts)} законов")

    # Формируем данные формы как JSON строку
    document_data_str = json.dumps(form_data, ensure_ascii=False, indent=2)

    # Формируем промпт
    prompt_text = get_ago_generation_prompt(
        ago_type=ago_type,
        document_data=document_data_str,
        situation_description=situation_description or ""
    )

    # Собираем контент: промпт + законы как файлы
    contents = [prompt_text]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[AGO_GENERATOR] Отправка запроса в AI, law_parts={len(law_parts)}")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=4096
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Логируем сырой ответ для отладки
        raw_response = response.text if hasattr(response, 'text') else str(response)
        logger.info(f"[AGO_GENERATOR] Сырой ответ AI (первые 500 символов): {raw_response[:500]}")

        # Парсим JSON из ответа
        result = _parse_json_response(raw_response)

        logger.info(f"[AGO_GENERATOR] Распарсенный результат: {list(result.keys()) if result else 'пусто'}")

        title = result.get('title', 'Постановление')

        # Проверяем новый формат с блоками
        if 'preamble' in result:
            # Новый формат с text_blocks
            text_blocks = {
                'title': title,
                'preamble': result.get('preamble', ''),
                'section_1': result.get('section_1', ''),
                'section_2': result.get('section_2', ''),
                'section_3': result.get('section_3', ''),
                'section_4': result.get('section_4', ''),
            }

            # Убираем пустые секции
            text_blocks = {k: v for k, v in text_blocks.items() if v}

            # Собираем main_text для обратной совместимости
            main_text_parts = [text_blocks.get('preamble', '')]
            for i in range(1, 5):
                section = text_blocks.get(f'section_{i}')
                if section:
                    main_text_parts.append(section)
            main_text = '\n\n'.join(main_text_parts)

            logger.info(f"[AGO_GENERATOR] Успешно сгенерирован AGO '{ago_type}': {title}, блоков: {len(text_blocks)}")

            return {
                'title': title,
                'main_text': main_text,
                'text_blocks': text_blocks
            }
        else:
            # Старый формат с main_text
            main_text = result.get('main_text', '')

            # Если main_text пустой, пробуем альтернативные ключи
            if not main_text:
                main_text = result.get('mainText', '') or result.get('text', '') or result.get('content', '')

            if not main_text:
                # Логируем весь результат для отладки
                logger.error(f"[AGO_GENERATOR] AI вернул пустой main_text. Полный результат: {result}")
                logger.error(f"[AGO_GENERATOR] Сырой ответ: {raw_response[:1000]}")
                raise RuntimeError("AI не сгенерировал текст документа")

            logger.info(f"[AGO_GENERATOR] Успешно сгенерирован AGO '{ago_type}': {title}, длина текста: {len(main_text)}")

            return {
                'title': title,
                'main_text': main_text
            }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[AGO_GENERATOR] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[AGO_GENERATOR] Ошибка генерации AGO: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации постановления: {str(e)}")


async def generate_custom_resolution_text(
    server_id: str,
    user_description: str,
    context: str = None,
    complaint_id: Optional[int] = None
) -> Dict:
    """
    Генерирует текст кастомного постановления через AI с RAG-фильтрацией законов.

    Args:
        server_id: ID сервера для загрузки законов
        user_description: Описание от пользователя какое постановление нужно
        context: Дополнительный контекст (текст жалобы, данные дела)
        complaint_id: ID привязанного обращения (опционально)

    Returns:
        Dict: {title: str, main_text: str}

    Raises:
        RuntimeError: При ошибке AI или аутентификации
    """
    logger.info(f"[CUSTOM_RESOLUTION] Генерация кастомного постановления для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем контекст для RAG-фильтра
    context_for_rag = f"Кастомное постановление\nОписание: {user_description[:1500]}"
    if context:
        context_for_rag += f"\nКонтекст: {context[:1000]}"

    # Получаем релевантные законы через RAG
    law_parts = await _get_relevant_laws_with_rag(context_for_rag, server_id)

    logger.info(f"[CUSTOM_RESOLUTION] RAG вернул {len(law_parts)} законов")

    # Формируем промпт
    prompt_text = get_custom_resolution_prompt(
        user_description=user_description,
        context=context or ""
    )

    # Собираем контент: промпт + законы как файлы
    contents = [prompt_text]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[CUSTOM_RESOLUTION] Отправка запроса в AI, law_parts={len(law_parts)}")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.5,  # Чуть выше для креативности
                max_output_tokens=4096
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Логируем сырой ответ для отладки
        raw_response = response.text if hasattr(response, 'text') else str(response)
        logger.info(f"[CUSTOM_RESOLUTION] Сырой ответ AI (первые 500 символов): {raw_response[:500]}")

        # Парсим JSON из ответа
        result = _parse_json_response(raw_response)

        title = result.get('title', 'Постановление')
        main_text = result.get('main_text', '')

        # Если main_text пустой, пробуем альтернативные ключи
        if not main_text:
            main_text = result.get('mainText', '') or result.get('text', '') or result.get('content', '')

        if not main_text:
            logger.error(f"[CUSTOM_RESOLUTION] AI вернул пустой main_text. Полный результат: {result}")
            logger.error(f"[CUSTOM_RESOLUTION] Сырой ответ: {raw_response[:1000]}")
            raise RuntimeError("AI не сгенерировал текст документа")

        logger.info(f"[CUSTOM_RESOLUTION] Успешно сгенерировано кастомное постановление: {title}, длина текста: {len(main_text)}")

        return {
            'title': title,
            'main_text': main_text
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CUSTOM_RESOLUTION] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CUSTOM_RESOLUTION] Ошибка генерации: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации постановления: {str(e)}")
