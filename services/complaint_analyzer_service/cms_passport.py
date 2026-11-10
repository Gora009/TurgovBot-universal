"""CMS: распознавание паспортных данных и разрешение ссылки на паспорт."""

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

logger = logging.getLogger('services.complaint_analyzer_service')


async def extract_passport_data_cms(image_url: str) -> Dict:
    """
    OCR паспорта (аналог extractPassportData из citizenService.js).

    Args:
        image_url: URL изображения паспорта

    Returns:
        Dict с данными паспорта

    Raises:
        RuntimeError: При ошибке AI или недоступности Vision
    """
    import httpx
    import tempfile
    import os
    from pathlib import Path
    from api.prompts.passport_ocr import get_passport_ocr_prompt

    logger.info(f"[CMS_PASSPORT_OCR] Распознавание паспорта: {image_url[:100]}...")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt_text = get_passport_ocr_prompt()

    # Скачиваем изображение
    async def download_image(url: str) -> tuple:
        """Скачивает изображение и возвращает (bytes, mime_type)"""
        # Определяем Referer на основе URL
        referer_url = 'https://forum.gta5rp.com/'
        if 'majestic-rp.ru' in url:
            referer_url = 'https://forum.majestic-rp.ru/'
        elif 'rmrp.ru' in url:
            referer_url = 'https://forum.rmrp.ru/'
        elif 'russia.online' in url:
            referer_url = 'https://forum.russia.online/'

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': referer_url,
        }

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers=headers
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

                # Определяем mime-type из Content-Type или URL
                content_type = response.headers.get('content-type', '').lower()
                if 'jpeg' in content_type or 'jpg' in content_type:
                    mime_type = 'image/jpeg'
                elif 'png' in content_type:
                    mime_type = 'image/png'
                elif 'gif' in content_type:
                    mime_type = 'image/gif'
                elif 'webp' in content_type:
                    mime_type = 'image/webp'
                else:
                    # Определяем по расширению URL
                    url_lower = url.lower().split('?')[0]
                    if url_lower.endswith('.png'):
                        mime_type = 'image/png'
                    elif url_lower.endswith('.gif'):
                        mime_type = 'image/gif'
                    elif url_lower.endswith('.webp'):
                        mime_type = 'image/webp'
                    else:
                        mime_type = 'image/jpeg'  # По умолчанию

                logger.info(f"[CMS_PASSPORT_OCR] Изображение скачано: {len(response.content)} байт, mime: {mime_type}")
                return response.content, mime_type

        except httpx.HTTPStatusError as e:
            logger.error(f"[CMS_PASSPORT_OCR] HTTP ошибка {e.response.status_code} при скачивании {url}")
            raise RuntimeError(f"Не удалось скачать изображение: HTTP {e.response.status_code}")
        except httpx.TimeoutException:
            logger.error(f"[CMS_PASSPORT_OCR] Таймаут при скачивании {url}")
            raise RuntimeError("Таймаут при скачивании изображения")
        except Exception as e:
            logger.error(f"[CMS_PASSPORT_OCR] Ошибка при скачивании {url}: {e}")
            raise RuntimeError(f"Ошибка при скачивании изображения: {str(e)}")

    # Скачиваем изображение
    logger.info(f"[CMS_PASSPORT_OCR] Начинаем скачивание: {image_url}")
    try:
        image_data, mime_type = await download_image(image_url)
        logger.info(f"[CMS_PASSPORT_OCR] Скачано успешно: {len(image_data)} байт, mime: {mime_type}")
    except RuntimeError as e:
        logger.error(f"[CMS_PASSPORT_OCR] Ошибка скачивания: {e}")
        raise e

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")

        # Формируем контент с изображением как inline_data (не from_uri!)
        image_part = types.Part(inline_data=types.Blob(
            data=image_data,
            mime_type=mime_type
        ))

        contents = [
            types.Part.from_text(text=prompt_text),
            image_part
        ]

        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=500
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

        # Нормализуем dateOfBirth
        dob = result.get('dateOfBirth')
        if dob and isinstance(dob, str):
            import re
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', dob):
                try:
                    from datetime import datetime
                    parsed = datetime.fromisoformat(dob.replace('/', '-'))
                    dob = parsed.strftime('%Y-%m-%d')
                except Exception:
                    pass
        result['dateOfBirth'] = dob

        confidence = int(result.get('confidence', 0))
        if confidence < 0:
            confidence = 0
        if confidence > 100:
            confidence = 100
        result['confidence'] = confidence

        result['isValid'] = bool(result.get('isValid', False))
        result['readabilityIssues'] = result.get('readabilityIssues', [])

        logger.info(f"[CMS_PASSPORT_OCR] Успешно: {result.get('fullName')}, confidence={confidence}")

        return result

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_OCR] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_OCR] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка распознавания: {str(e)}")


async def resolve_passport_url_cms(content: str, urls: list) -> Dict:
    """
    Определение ссылки на паспорт среди нескольких URL (аналог resolvePassportUrlFromCandidates).

    Args:
        content: Текст обращения
        urls: Список URL-ссылок (минимум 2)

    Returns:
        Dict: {passportUrl: str | None}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.passport_resolver import get_passport_resolver_prompt

    logger.info(f"[CMS_PASSPORT_RESOLVER] Определение паспорта среди {len(urls)} ссылок")

    if not urls or len(urls) < 2:
        return {'passportUrl': None}

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_passport_resolver_prompt(content, urls)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=500
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

        passport_url = result.get('passportUrl')

        # Проверяем что URL из списка
        if passport_url and isinstance(passport_url, str):
            passport_url = passport_url.strip()
            if passport_url not in urls:
                # Попробуем найти похожий
                for u in urls:
                    if passport_url in u or u in passport_url:
                        passport_url = u
                        break
                else:
                    passport_url = None

        logger.info(f"[CMS_PASSPORT_RESOLVER] Результат: {passport_url}")

        return {'passportUrl': passport_url}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_RESOLVER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_RESOLVER] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка определения: {str(e)}")
