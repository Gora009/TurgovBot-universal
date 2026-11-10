"""CMS: черновики материалов дела (с RAG и обратной связью), план и его адаптация."""

import logging
from typing import Dict
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

from .analysis import _parse_json_response
from .laws import _get_relevant_laws_with_rag_extended

logger = logging.getLogger('services.complaint_analyzer_service')


async def generate_case_file_draft_cms(
    complaint_title: str,
    complaint_content: str,
    facts_with_verdict: list,
    law_sections: list,
    requirements: list
) -> Dict:
    """
    Генерация черновика кейс-файла (аналог generateCaseFileDraft из caseFileDraftService.js).

    Args:
        complaint_title: Заголовок жалобы
        complaint_content: Текст жалобы
        facts_with_verdict: Список фактов с пометкой прокурора
        law_sections: Тексты релевантных статей законов
        requirements: Требования заявителя

    Returns:
        Dict: {draft: {...}, sections_count: int}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.case_file_draft import get_case_file_draft_prompt, REQUIRED_KEYS

    logger.info(f"[CMS_CASE_FILE_DRAFT] Генерация черновика")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем текст законов
    law_sections_text = "\n\n---\n\n".join(law_sections) if law_sections else ""
    requirements_text = "; ".join(requirements) if requirements else ""

    prompt = get_case_file_draft_prompt(
        complaint_title=complaint_title,
        complaint_content=complaint_content,
        facts_with_verdict=facts_with_verdict,
        law_sections_text=law_sections_text,
        requirements_text=requirements_text
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
                max_output_tokens=4000
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

        result = _parse_json_response(response.text)

        # Валидируем обязательные поля
        draft = {}
        for key in REQUIRED_KEYS:
            value = result.get(key, '')
            if not isinstance(value, str):
                value = str(value) if value else ''
            draft[key] = value.strip()

        # Проверяем что все поля заполнены
        empty_fields = [k for k in REQUIRED_KEYS if not draft[k]]
        if empty_fields:
            logger.warning(f"[CMS_CASE_FILE_DRAFT] Пустые поля: {empty_fields}")

        logger.info(f"[CMS_CASE_FILE_DRAFT] Успешно сгенерирован черновик")

        return {
            'draft': draft,
            'sections_count': len(law_sections)
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


async def generate_case_file_draft_with_rag_cms(
    server_id: str,
    complaint_title: str,
    complaint_content: str,
    facts_with_verdict: list,
    requirements: list,
    pipeline_context: str = None
) -> Dict:
    """
    Генерация черновика кейс-файла с умным RAG-фильтром.

    Законы выбираются AI-роутером и передаются как GCS Files
    в контекст генерации (AI видит их полные тексты).

    Args:
        server_id: ID сервера для загрузки законов
        complaint_title: Заголовок жалобы
        complaint_content: Текст жалобы
        facts_with_verdict: Список фактов с пометкой прокурора
        requirements: Требования заявителя
        pipeline_context: Контекст из пайплайна (комментарии, допросы, решения прокурора)

    Returns:
        Dict: {draft: {...}, sections_count: int, selected_laws: [...]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.case_file_draft import get_case_file_draft_prompt_for_rag, REQUIRED_KEYS

    logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] Генерация с RAG для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # 1. Формируем контекст для RAG-фильтра
    facts_summary = ', '.join([f.get('text', '')[:100] for f in facts_with_verdict[:5]])
    context_for_rag = f"""Тип документа: Кейс-файл (дело о привлечении к ответственности)
Заголовок жалобы: {complaint_title}
Содержание: {complaint_content[:3000]}
Факты: {facts_summary}"""

    # 2. AI выбирает релевантные законы (возвращает List[types.Part] + названия)
    law_parts, selected_law_titles = await _get_relevant_laws_with_rag_extended(context_for_rag, server_id)

    logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] RAG выбрал {len(law_parts)} законов: {selected_law_titles}")

    # 3. Формируем промпт (без текстов законов — они будут как Part)
    requirements_text = "; ".join(requirements) if requirements else ""
    prompt = get_case_file_draft_prompt_for_rag(
        complaint_title=complaint_title,
        complaint_content=complaint_content,
        facts_with_verdict=facts_with_verdict,
        requirements_text=requirements_text,
        pipeline_context=pipeline_context
    )

    # 4. Собираем контент: промпт + законы как файлы
    contents = [prompt]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] Отправка запроса в AI, law_parts={len(law_parts)}")

    # 5. Генерируем черновик (AI видит и промпт, и тексты законов)
    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4000
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

        result = _parse_json_response(response.text)

        # Валидируем обязательные поля
        draft = {}
        for key in REQUIRED_KEYS:
            value = result.get(key, '')
            if not isinstance(value, str):
                value = str(value) if value else ''
            draft[key] = value.strip()

        # Проверяем что все поля заполнены
        empty_fields = [k for k in REQUIRED_KEYS if not draft[k]]
        if empty_fields:
            logger.warning(f"[CMS_CASE_FILE_DRAFT_RAG] Пустые поля: {empty_fields}")

        logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] Успешно сгенерирован черновик с {len(law_parts)} законами")

        return {
            'draft': draft,
            'sections_count': len(law_parts),
            'selected_laws': selected_law_titles
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_RAG] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_RAG] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


async def generate_case_file_draft_with_feedback_cms(
    server_id: str,
    complaint_title: str,
    complaint_content: str,
    facts_with_verdict: list,
    requirements: list,
    previous_draft: dict,
    prosecutor_feedback: str,
    pipeline_context: str = None
) -> Dict:
    """
    Перегенерация черновика кейс-файла с учётом замечаний прокурора.

    Использует RAG для выбора законов + промпт с предыдущим черновиком и замечаниями.

    Args:
        server_id: ID сервера для загрузки законов
        complaint_title: Заголовок жалобы
        complaint_content: Текст жалобы
        facts_with_verdict: Список фактов с пометкой прокурора
        requirements: Требования заявителя
        previous_draft: Предыдущий черновик (dict с 7 полями)
        prosecutor_feedback: Текст замечаний прокурора
        pipeline_context: Контекст из пайплайна (комментарии, допросы, решения)

    Returns:
        Dict: {draft: {...}, sections_count: int, selected_laws: [...]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.case_file_draft import get_case_file_draft_feedback_prompt, REQUIRED_KEYS

    logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Перегенерация с замечаниями для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # 1. Формируем контекст для RAG-фильтра
    facts_summary = ', '.join([f.get('text', '')[:100] for f in facts_with_verdict[:5]])
    context_for_rag = f"""Тип документа: Кейс-файл (дело о привлечении к ответственности)
Заголовок жалобы: {complaint_title}
Содержание: {complaint_content[:3000]}
Факты: {facts_summary}"""

    # 2. AI выбирает релевантные законы (возвращает List[types.Part] + названия)
    law_parts, selected_law_titles = await _get_relevant_laws_with_rag_extended(context_for_rag, server_id)

    logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] RAG выбрал {len(law_parts)} законов: {selected_law_titles}")

    # 3. Формируем промпт с предыдущим черновиком и замечаниями
    requirements_text = "; ".join(requirements) if requirements else ""
    prompt = get_case_file_draft_feedback_prompt(
        complaint_title=complaint_title,
        complaint_content=complaint_content,
        facts_with_verdict=facts_with_verdict,
        requirements_text=requirements_text,
        previous_draft=previous_draft,
        prosecutor_feedback=prosecutor_feedback,
        pipeline_context=pipeline_context
    )

    # 4. Собираем контент: промпт + законы как файлы
    contents = [prompt]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Отправка запроса в AI, law_parts={len(law_parts)}")

    # 5. Генерируем черновик
    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4000
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

        result = _parse_json_response(response.text)

        # Валидируем обязательные поля
        draft = {}
        for key in REQUIRED_KEYS:
            value = result.get(key, '')
            if not isinstance(value, str):
                value = str(value) if value else ''
            draft[key] = value.strip()

        # Проверяем что все поля заполнены
        empty_fields = [k for k in REQUIRED_KEYS if not draft[k]]
        if empty_fields:
            logger.warning(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Пустые поля: {empty_fields}")

        logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Успешно перегенерирован черновик с {len(law_parts)} законами")

        return {
            'draft': draft,
            'sections_count': len(law_parts),
            'selected_laws': selected_law_titles
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


async def generate_plan_ai_cms(context: dict) -> Dict:
    """
    Генерация плана работы (аналог generatePlanAI из adaptivePlanService.js).

    Args:
        context: Контекст жалобы (complaint, analysis, defendantHistory)

    Returns:
        Dict с планом работы

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.adaptive_plan import get_generate_plan_prompt

    logger.info(f"[CMS_GENERATE_PLAN] Генерация плана работы")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_generate_plan_prompt(context)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
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

        result = _parse_json_response(response.text)

        # Валидируем и нормализуем результат
        plan_type = result.get('planType', 'STANDARD')
        if plan_type not in ('STANDARD', 'WITH_DOCS_REQUEST', 'REJECTION', 'INTERROGATION', 'COMPLEX'):
            plan_type = 'STANDARD'

        complexity = result.get('complexity', 'MEDIUM')
        if complexity not in ('LOW', 'MEDIUM', 'HIGH'):
            complexity = 'MEDIUM'

        logger.info(f"[CMS_GENERATE_PLAN] План создан: type={plan_type}, complexity={complexity}")

        return {
            'planType': plan_type,
            'estimatedDuration': result.get('estimatedDuration', '3-5 дней'),
            'complexity': complexity,
            'phases': result.get('phases', []),
            'decisionPoints': result.get('decisionPoints', []),
            'riskFactors': result.get('riskFactors', []),
            'recommendations': result.get('recommendations', [])
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_GENERATE_PLAN] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_GENERATE_PLAN] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации плана: {str(e)}")


async def adapt_plan_ai_cms(
    current_plan: dict,
    new_information: dict,
    completed_steps: list
) -> Dict:
    """
    Адаптация плана работы (аналог adaptPlanAI из adaptivePlanService.js).

    Args:
        current_plan: Текущий план
        new_information: Новая информация
        completed_steps: Список номеров выполненных шагов

    Returns:
        Dict с результатом адаптации

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.adaptive_plan import get_adapt_plan_prompt

    logger.info(f"[CMS_ADAPT_PLAN] Адаптация плана, выполнено шагов: {len(completed_steps)}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_adapt_plan_prompt(current_plan, new_information, completed_steps)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=1500
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

        result = _parse_json_response(response.text)

        needs_adaptation = bool(result.get('needsAdaptation', False))

        logger.info(f"[CMS_ADAPT_PLAN] Результат: needsAdaptation={needs_adaptation}")

        return {
            'needsAdaptation': needs_adaptation,
            'reason': result.get('reason', ''),
            'changes': result.get('changes', []),
            'newEstimatedDuration': result.get('newEstimatedDuration'),
            'warnings': result.get('warnings', [])
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ADAPT_PLAN] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ADAPT_PLAN] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка адаптации плана: {str(e)}")
