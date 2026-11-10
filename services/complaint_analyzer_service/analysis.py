"""Проверка формы жалобы, анализ, извлечение фактов, разбор JSON-ответов модели."""

import json
import logging
import re
from typing import Dict
from google.genai import errors as genai_errors
from google.genai import types
from google.api_core import exceptions as google_exceptions

from config import (
    COMPLAINT_ANALYZER_MODEL_NAME,
    COMPLAINT_COMPLEXITY_LEVELS,
    COMPLAINT_REVIEWER_LEVELS,
    COMPLAINT_TARGET_TYPES,
)
from prompts import (
    get_complaint_form_check_prompt,
    get_complaint_analysis_prompt,
    get_complaint_facts_extraction_prompt,
)
from common.rate_limiter import (
    execute_vertex_request,
    RequestPriority,
    get_current_client,
    mark_current_region_success,
    mark_current_region_error,
)
from common.utils import reinitialize_vertex_ai

from .forms import get_form_template, get_issue_types
from .laws import _get_relevant_laws

logger = logging.getLogger('services.complaint_analyzer_service')


async def check_complaint_form(server_id: str, complaint_text: str) -> Dict:
    """
    Проверяет форму жалобы на соответствие шаблону.

    Args:
        server_id: ID сервера
        complaint_text: Текст жалобы

    Returns:
        Dict: {is_valid, explanation, issues}

    Raises:
        RuntimeError: При ошибке AI
    """
    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Получаем шаблон формы
    template = await get_form_template(server_id)
    form_template = template.get('form_content', '') if template else ''

    # Получаем типы проблем
    issue_types = await get_issue_types(server_id)
    issue_types_json = json.dumps(issue_types, ensure_ascii=False, indent=2)

    # Формируем промпт
    prompt = get_complaint_form_check_prompt(complaint_text, form_template, issue_types_json)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
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

        # Валидируем ответ
        is_valid = result.get('is_valid', False)
        explanation = result.get('explanation', '')
        issues = result.get('issues', [])

        # Фильтруем только валидные коды
        valid_codes = {it['code'] for it in issue_types}
        issues = [code for code in issues if code in valid_codes]

        logger.info(f"[COMPLAINT_ANALYZER] Проверка формы: valid={is_valid}, issues={len(issues)}")

        return {
            'is_valid': is_valid,
            'explanation': explanation,
            'issues': issues
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


async def analyze_complaint(server_id: str, complaint_text: str) -> Dict:
    """
    Анализирует жалобу: сложность, субъект, рецензент.

    Args:
        server_id: ID сервера
        complaint_text: Текст жалобы

    Returns:
        Dict: {complexity, needs_additional_materials, subject, reviewer_level}

    Raises:
        RuntimeError: При ошибке AI
    """
    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Получаем законы
    relevant_laws = await _get_relevant_laws(server_id)

    # Формируем промпт
    prompt = get_complaint_analysis_prompt(complaint_text, relevant_laws)

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

        # Парсим JSON
        result = _parse_json_response(response.text)

        # Валидируем и нормализуем
        complexity = result.get('complexity', 'medium')
        if complexity not in COMPLAINT_COMPLEXITY_LEVELS:
            complexity = 'medium'

        reviewer_level = result.get('reviewer_level', 'any')
        if reviewer_level not in COMPLAINT_REVIEWER_LEVELS:
            reviewer_level = 'any'

        subject = result.get('subject', {})
        target_type = subject.get('target_type')
        if target_type and target_type not in COMPLAINT_TARGET_TYPES:
            target_type = None

        logger.info(f"[COMPLAINT_ANALYZER] Анализ: complexity={complexity}, reviewer={reviewer_level}")

        return {
            'complexity': complexity,
            'needs_additional_materials': bool(result.get('needs_additional_materials', False)),
            'subject': {
                'target_type': target_type,
                'nickname': subject.get('nickname'),
                'organization': subject.get('organization'),
                'position': subject.get('position')
            },
            'reviewer_level': reviewer_level
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


async def extract_complaint_facts(server_id: str, complaint_text: str) -> Dict:
    """
    Извлекает факты и требования из жалобы.

    Args:
        server_id: ID сервера (для будущего расширения)
        complaint_text: Текст жалобы

    Returns:
        Dict: {facts, requirements}

    Raises:
        RuntimeError: При ошибке AI
    """
    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем промпт
    prompt = get_complaint_facts_extraction_prompt(complaint_text)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
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

        # Парсим JSON
        result = _parse_json_response(response.text)

        facts = result.get('facts', [])
        requirements = result.get('requirements', [])

        # Убедимся что это списки строк
        if not isinstance(facts, list):
            facts = []
        if not isinstance(requirements, list):
            requirements = []

        facts = [str(f) for f in facts if f]
        requirements = [str(r) for r in requirements if r]

        logger.info(f"[COMPLAINT_ANALYZER] Извлечено: {len(facts)} фактов, {len(requirements)} требований")

        return {
            'facts': facts,
            'requirements': requirements
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


def _parse_json_response(text: str) -> Dict:
    """
    Извлекает JSON из ответа AI.
    Обрабатывает случаи когда JSON обёрнут в markdown блок.
    Всегда возвращает словарь (пустой при неудаче).
    """
    if not text:
        logger.warning("[COMPLAINT_ANALYZER] Пустой текст для парсинга")
        return {}

    text = text.strip()

    # Логируем входные данные для отладки
    logger.debug(f"[COMPLAINT_ANALYZER] Парсинг JSON, длина текста: {len(text)}")

    # Убираем markdown блоки ```json или ```
    if text.startswith('```'):
        lines = text.split('\n')
        # Убираем первую строку с ``` или ```json
        if lines[0].startswith('```'):
            lines = lines[1:]
        # Убираем последнюю строку с ```
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()

    # Пытаемся парсить JSON напрямую
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            logger.debug(f"[COMPLAINT_ANALYZER] JSON успешно распарсен напрямую")
            return result
        else:
            logger.warning(f"[COMPLAINT_ANALYZER] JSON распарсен, но это не словарь: {type(result).__name__}")
            return {}
    except json.JSONDecodeError as e:
        logger.debug(f"[COMPLAINT_ANALYZER] Прямой парсинг не удался: {e}")

    # Пробуем найти JSON объект в тексте (от { до последней })
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            json_text = json_match.group()
            result = json.loads(json_text)
            if isinstance(result, dict):
                logger.debug(f"[COMPLAINT_ANALYZER] JSON найден через regex")
                return result
            else:
                logger.warning(f"[COMPLAINT_ANALYZER] Regex JSON не словарь: {type(result).__name__}")
        except json.JSONDecodeError as e:
            logger.debug(f"[COMPLAINT_ANALYZER] Regex парсинг не удался: {e}")
            # Пробуем исправить экранирование
            try:
                # Заменяем неэкранированные переносы строк
                fixed_json = json_text.replace('\n', '\\n').replace('\r', '\\r')
                result = json.loads(fixed_json)
                if isinstance(result, dict):
                    logger.debug(f"[COMPLAINT_ANALYZER] JSON распарсен после исправления переносов")
                    return result
            except json.JSONDecodeError:
                pass

    logger.warning(f"[COMPLAINT_ANALYZER] Не удалось распарсить JSON: {text[:500]}")
    return {}
