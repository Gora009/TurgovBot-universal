"""CMS: полный и комбинированный анализ жалобы, теги, метки доказательств, проверка формы."""

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
from .laws import _get_relevant_laws_for_cms

logger = logging.getLogger('services.complaint_analyzer_service')


async def analyze_complaint_full_cms(
    server_id: str,
    title: str,
    content: str
) -> Dict:
    """
    Полный анализ жалобы (аналог analyzeComplaintFull из complaintAiService.js).

    Возвращает формат, идентичный OpenAI-версии на сайте:
    - extractedData (с respondents массивом)
    - facts, legalCharacterization, requirements
    - formCheck, summary, recommendations
    - evidence, pipelineFlags

    Использует RAG-фильтр для получения релевантных законов сервера.

    Args:
        server_id: ID сервера
        title: Заголовок жалобы
        content: Текст жалобы

    Returns:
        Dict в формате CMS (идентичный OpenAI-версии)

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.complaint_analysis import (
        get_full_complaint_analysis_prompt,
        COMPLAINT_FULL_ANALYSIS_SCHEMA
    )

    logger.info(f"[CMS_FULL_ANALYSIS] Полный анализ жалобы для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем промпт без законов (RAG отключён)
    prompt = get_full_complaint_analysis_prompt(title, content)
    logger.info(f"[CMS_FULL_ANALYSIS] Используем промпт без законов")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
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

        # Парсим JSON
        result = _parse_json_response(response.text)

        if not result:
            logger.warning("[CMS_FULL_ANALYSIS] AI вернул пустой результат")
            return _get_default_full_analysis()

        # Нормализуем respondents
        respondents = []
        raw_respondents = result.get('extractedData', {}).get('respondents', [])
        if isinstance(raw_respondents, list):
            for r in raw_respondents:
                if r and isinstance(r.get('name'), str):
                    respondents.append({
                        'name': str(r['name']),
                        'position': str(r['position']) if r.get('position') else None,
                        'org': str(r['org']) if r.get('org') else None
                    })

        first = respondents[0] if respondents else {}

        extracted_data = {
            'complainantName': result.get('extractedData', {}).get('complainantName'),
            'respondent': first.get('name'),
            'respondentPosition': first.get('position'),
            'respondentOrg': first.get('org'),
            'respondents': respondents,
            'complexity': result.get('extractedData', {}).get('complexity', 'MEDIUM'),
            'complaintNumber': result.get('extractedData', {}).get('complaintNumber')
        }

        # Нормализуем complexity
        if extracted_data['complexity'] not in ('LOW', 'MEDIUM', 'HIGH'):
            extracted_data['complexity'] = 'MEDIUM'

        # Нормализуем facts и legalCharacterization
        facts = [str(f) for f in result.get('facts', []) if f]
        legal_chars = result.get('legalCharacterization', [])
        if not isinstance(legal_chars, list):
            legal_chars = []
        legal_chars = [str(c) if c else '' for c in legal_chars]

        # Выравниваем длину
        while len(legal_chars) < len(facts):
            legal_chars.append('')
        legal_chars = legal_chars[:len(facts)]

        # Нормализуем requirements
        requirements = [str(r) for r in result.get('requirements', []) if r]

        # Нормализуем formCheck
        form_check_raw = result.get('formCheck', {})
        form_check = {
            'hasSignature': bool(form_check_raw.get('hasSignature')),
            'hasEmail': bool(form_check_raw.get('hasEmail')),
            'hasPassportCopy': bool(form_check_raw.get('hasPassportCopy')),
            'isValid': bool(form_check_raw.get('isValid'))
        }

        # Нормализуем recommendations
        recommendations = []
        for rec in result.get('recommendations', []):
            if rec and (rec.get('title') or rec.get('description')):
                recommendations.append({
                    'type': rec.get('type', 'info'),
                    'title': rec.get('title', ''),
                    'description': rec.get('description', '')
                })

        # Нормализуем evidence
        evidence = []
        for ev in result.get('evidence', []):
            if ev and isinstance(ev.get('url'), str) and ev['url'].strip():
                evidence.append({
                    'url': ev['url'].strip(),
                    'typeLabel': str(ev.get('typeLabel', 'Ссылка')).strip() or 'Ссылка'
                })

        # Нормализуем pipelineFlags
        flags_raw = result.get('pipelineFlags', {})
        pipeline_flags = {
            'requiresDocs': bool(flags_raw.get('requiresDocs')),
            'requiresInterrogation': bool(flags_raw.get('requiresInterrogation')),
            'requiresCourt': bool(flags_raw.get('requiresCourt'))
        }

        logger.info(f"[CMS_FULL_ANALYSIS] Успешно: {len(facts)} фактов, {len(respondents)} ответчиков")

        return {
            'extractedData': extracted_data,
            'facts': facts,
            'legalCharacterization': legal_chars,
            'requirements': requirements,
            'formCheck': form_check,
            'summary': result.get('summary', ''),
            'recommendations': recommendations,
            'evidence': evidence,
            'pipelineFlags': pipeline_flags
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FULL_ANALYSIS] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FULL_ANALYSIS] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка анализа: {str(e)}")


def _get_default_full_analysis() -> Dict:
    """Возвращает дефолтный результат полного анализа."""
    return {
        'extractedData': {
            'complainantName': None,
            'respondent': None,
            'respondentPosition': None,
            'respondentOrg': None,
            'respondents': [],
            'complexity': 'MEDIUM',
            'complaintNumber': None
        },
        'facts': [],
        'legalCharacterization': [],
        'requirements': [],
        'formCheck': {
            'hasSignature': False,
            'hasEmail': False,
            'hasPassportCopy': False,
            'isValid': False
        },
        'summary': None,
        'recommendations': [],
        'evidence': [],
        'pipelineFlags': {
            'requiresDocs': False,
            'requiresInterrogation': False,
            'requiresCourt': False
        }
    }


async def analyze_complaint_combined_cms(
    server_id: str,
    title: str,
    content: str
) -> Dict:
    """
    Комбинированный анализ жалобы: fullAnalysis + issueTags за 2 запроса.

    Оптимизация: вместо 4 запросов (2 RAG + 2 анализа) делаем 2:
    1. RAG-роутер — выбирает законы для обеих задач
    2. Комбинированный анализ — возвращает fullAnalysis + issueTags

    Args:
        server_id: ID сервера
        title: Заголовок жалобы
        content: Текст жалобы

    Returns:
        Dict: {
            "fullAnalysis": { ... результат analyzeComplaintFull ... },
            "issueTags": ["тег1", "тег2", ...],
            "rag_laws_count": int
        }

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.complaint_analysis import get_combined_analysis_prompt_with_laws

    logger.info(f"[CMS_COMBINED] Комбинированный анализ для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Комбинированный анализ без законов (RAG отключён)
    prompt = get_combined_analysis_prompt_with_laws(title, content, "")
    rag_laws_count = 0  # RAG отключён
    logger.info(f"[CMS_COMBINED] Запускаем комбинированный анализ без законов")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=5000
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

        if not result:
            logger.warning("[CMS_COMBINED] AI вернул пустой результат")
            return {
                "fullAnalysis": _get_default_full_analysis(),
                "issueTags": [],
                "rag_laws_count": rag_laws_count
            }

        # Проверка типа result
        if not isinstance(result, dict):
            logger.warning(f"[CMS_COMBINED] AI вернул не словарь, а {type(result).__name__}: {str(result)[:200]}")
            return {
                "fullAnalysis": _get_default_full_analysis(),
                "issueTags": [],
                "rag_laws_count": rag_laws_count
            }

        # Извлекаем analysis
        analysis_raw = result.get('analysis', {})

        # Проверка типа analysis_raw
        if not isinstance(analysis_raw, dict):
            logger.warning(f"[CMS_COMBINED] analysis не словарь, а {type(analysis_raw).__name__}")
            analysis_raw = {}

        # Нормализуем respondents
        respondents = []
        raw_respondents = analysis_raw.get('extractedData', {}).get('respondents', [])
        if isinstance(raw_respondents, list):
            for r in raw_respondents:
                if r and isinstance(r.get('name'), str):
                    respondents.append({
                        'name': str(r['name']),
                        'position': str(r['position']) if r.get('position') else None,
                        'org': str(r['org']) if r.get('org') else None
                    })

        first = respondents[0] if respondents else {}

        extracted_data = {
            'complainantName': analysis_raw.get('extractedData', {}).get('complainantName'),
            'respondent': first.get('name'),
            'respondentPosition': first.get('position'),
            'respondentOrg': first.get('org'),
            'respondents': respondents,
            'complexity': analysis_raw.get('extractedData', {}).get('complexity', 'MEDIUM'),
            'complaintNumber': analysis_raw.get('extractedData', {}).get('complaintNumber')
        }

        if extracted_data['complexity'] not in ('LOW', 'MEDIUM', 'HIGH'):
            extracted_data['complexity'] = 'MEDIUM'

        # Нормализуем facts и legalCharacterization
        facts = [str(f) for f in analysis_raw.get('facts', []) if f]
        legal_chars = analysis_raw.get('legalCharacterization', [])
        if not isinstance(legal_chars, list):
            legal_chars = []
        legal_chars = [str(c) if c else '' for c in legal_chars]

        while len(legal_chars) < len(facts):
            legal_chars.append('')
        legal_chars = legal_chars[:len(facts)]

        requirements = [str(r) for r in analysis_raw.get('requirements', []) if r]

        form_check_raw = analysis_raw.get('formCheck', {})
        if not isinstance(form_check_raw, dict):
            form_check_raw = {}
        form_check = {
            'hasSignature': bool(form_check_raw.get('hasSignature')),
            'hasEmail': bool(form_check_raw.get('hasEmail')),
            'hasPassportCopy': bool(form_check_raw.get('hasPassportCopy')),
            'isValid': bool(form_check_raw.get('isValid'))
        }

        recommendations = []
        for rec in analysis_raw.get('recommendations', []):
            # AI может вернуть строку вместо объекта
            if isinstance(rec, str) and rec.strip():
                recommendations.append({
                    'type': 'info',
                    'title': rec.strip(),
                    'description': ''
                })
            elif isinstance(rec, dict) and (rec.get('title') or rec.get('description')):
                recommendations.append({
                    'type': rec.get('type', 'info'),
                    'title': rec.get('title', ''),
                    'description': rec.get('description', '')
                })

        evidence = []
        for ev in analysis_raw.get('evidence', []):
            # AI может вернуть строку URL вместо объекта
            if isinstance(ev, str) and ev.strip():
                evidence.append({
                    'url': ev.strip(),
                    'typeLabel': 'Ссылка'
                })
            elif isinstance(ev, dict) and isinstance(ev.get('url'), str) and ev['url'].strip():
                evidence.append({
                    'url': ev['url'].strip(),
                    'typeLabel': str(ev.get('typeLabel', 'Ссылка')).strip() or 'Ссылка'
                })

        flags_raw = analysis_raw.get('pipelineFlags', {})
        if not isinstance(flags_raw, dict):
            flags_raw = {}
        pipeline_flags = {
            'requiresDocs': bool(flags_raw.get('requiresDocs')),
            'requiresInterrogation': bool(flags_raw.get('requiresInterrogation')),
            'requiresCourt': bool(flags_raw.get('requiresCourt'))
        }

        full_analysis = {
            'extractedData': extracted_data,
            'facts': facts,
            'legalCharacterization': legal_chars,
            'requirements': requirements,
            'formCheck': form_check,
            'summary': analysis_raw.get('summary', ''),
            'recommendations': recommendations,
            'evidence': evidence,
            'pipelineFlags': pipeline_flags
        }

        # Извлекаем issueTags
        issue_tags = []
        raw_tags = result.get('issueTags', [])
        if isinstance(raw_tags, list):
            seen = set()
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    key = t.lower().strip()
                    if key not in seen:
                        seen.add(key)
                        issue_tags.append(t.strip())

        logger.info(f"[CMS_COMBINED] Успешно: {len(facts)} фактов, {len(respondents)} ответчиков, {len(issue_tags)} тегов")

        return {
            "fullAnalysis": full_analysis,
            "issueTags": issue_tags,
            "rag_laws_count": rag_laws_count
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_COMBINED] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_COMBINED] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка комбинированного анализа: {str(e)}")


async def get_issue_spotting_tags_cms(facts: list, server_id: str = None) -> Dict:
    """
    Получение юридических тегов по фактам (аналог getIssueSpottingTags из complaintAiService.js).

    Если передан server_id, использует RAG-фильтр для получения законов
    и формирует теги с учётом терминологии законов.

    Args:
        facts: Список фактов
        server_id: ID сервера (опционально, для RAG)

    Returns:
        Dict: {tags: List[str]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.issue_spotting import (
        get_issue_spotting_prompt,
        get_issue_spotting_prompt_with_laws,
        ISSUE_SPOTTING_SCHEMA
    )

    logger.info(f"[CMS_ISSUE_SPOTTING] Получение тегов для {len(facts)} фактов, server_id={server_id}")

    if not facts:
        return {'tags': []}

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Получаем законы если передан server_id
    laws_text = ""
    if server_id:
        facts_text = "\n".join(facts[:10])  # Берём первые 10 фактов для контекста
        laws_text = await _get_relevant_laws_for_cms(facts_text, server_id)

    # Формируем промпт
    if laws_text:
        prompt = get_issue_spotting_prompt_with_laws(facts, laws_text)
        logger.info(f"[CMS_ISSUE_SPOTTING] Используем промпт с законами")
    else:
        prompt = get_issue_spotting_prompt(facts)
        logger.info(f"[CMS_ISSUE_SPOTTING] Используем промпт без законов")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
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

        tags = []
        raw_tags = result.get('tags', [])
        if isinstance(raw_tags, list):
            seen = set()
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    key = t.lower().strip()
                    if key not in seen:
                        seen.add(key)
                        tags.append(t.strip())

        logger.info(f"[CMS_ISSUE_SPOTTING] Получено {len(tags)} тегов")
        return {'tags': tags}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ISSUE_SPOTTING] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ISSUE_SPOTTING] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка получения тегов: {str(e)}")


async def get_evidence_type_labels_cms(text: str, urls: list) -> Dict:
    """
    Классификация типов ссылок на доказательства (аналог getEvidenceTypeLabels из complaintAiService.js).

    Args:
        text: Текст обращения
        urls: Список URL для классификации

    Returns:
        Dict: {labels: List[{url, typeLabel}]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.evidence_labels import get_evidence_labels_prompt

    logger.info(f"[CMS_EVIDENCE_LABELS] Классификация {len(urls)} ссылок")

    if not urls:
        return {'labels': []}

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_evidence_labels_prompt(text, urls)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
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

        labels = []
        # Поддерживаем разные форматы ответа
        raw_labels = result.get('labels', []) or result.get('evidence', []) or result.get('items', [])
        if isinstance(raw_labels, list):
            for item in raw_labels:
                if item and (item.get('url') or item.get('externalUrl')):
                    labels.append({
                        'url': item.get('url') or item.get('externalUrl'),
                        'typeLabel': item.get('typeLabel') or item.get('title') or 'Ссылка'
                    })

        logger.info(f"[CMS_EVIDENCE_LABELS] Классифицировано {len(labels)} ссылок")
        return {'labels': labels}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_EVIDENCE_LABELS] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_EVIDENCE_LABELS] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка классификации: {str(e)}")


async def validate_form_ai_cms(title: str, content: str) -> Dict:
    """
    AI-валидация формы обращения (аналог validateFormAI из formValidatorService.js).

    Args:
        title: Заголовок обращения
        content: Текст обращения

    Returns:
        Dict в формате CMS (идентичный OpenAI-версии)

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.form_validation import get_form_validation_ai_prompt, FORM_VALIDATION_SCHEMA

    logger.info(f"[CMS_FORM_VALIDATION] AI-валидация формы")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_form_validation_ai_prompt(title, content)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
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

        # Нормализуем ответ
        is_valid = bool(result.get('isValid', False))
        confidence = int(result.get('confidenceScore', 0))
        if confidence < 0:
            confidence = 0
        if confidence > 100:
            confidence = 100

        form_quality = result.get('formQuality', 'INVALID')
        if form_quality not in ('EXCELLENT', 'GOOD', 'ACCEPTABLE', 'POOR', 'INVALID'):
            form_quality = 'INVALID'

        # Нормализуем issues
        issues = []
        for issue in result.get('issues', []):
            if issue:
                issues.append({
                    'severity': issue.get('severity', 'INFO'),
                    'field': issue.get('field', ''),
                    'issue': issue.get('issue', ''),
                    'suggestion': issue.get('suggestion', '')
                })

        # Нормализуем extractedData
        extracted_raw = result.get('extractedData', {})
        extracted_data = {
            'complainant': extracted_raw.get('complainant'),
            'defendant': extracted_raw.get('defendant'),
            'organization': extracted_raw.get('organization'),
            'incidentDate': extracted_raw.get('incidentDate'),
            'incidentTime': extracted_raw.get('incidentTime')
        }

        recommendation = result.get('recommendation', 'REJECT')
        if recommendation not in ('ACCEPT', 'ACCEPT_WITH_NOTICE', 'REQUEST_CORRECTION', 'REJECT'):
            recommendation = 'REJECT'

        logger.info(f"[CMS_FORM_VALIDATION] Результат: valid={is_valid}, quality={form_quality}")

        return {
            'isValid': is_valid,
            'confidenceScore': confidence,
            'formQuality': form_quality,
            'issues': issues,
            'missingInfo': result.get('missingInfo', []),
            'contradictions': result.get('contradictions', []),
            'extractedData': extracted_data,
            'recommendation': recommendation,
            'summary': result.get('summary', '')
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FORM_VALIDATION] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FORM_VALIDATION] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка валидации: {str(e)}")
