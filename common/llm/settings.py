"""Настройки LLM-поставщиков из переменных окружения.

Бот работает с любым OpenAI-совместимым API: DeepSeek, OpenAI, OpenRouter, Qwen
(DashScope), Groq, Together, Mistral, локальные vLLM / Ollama / LM Studio и т.д.

Модели разбиты по РОЛЯМ — у каждой роли может быть свой поставщик и ключ:

    fast    быстрые служебные вызовы: роутеры законов, оптимизатор вопроса, сводки
    smart   основные ответы: консультант, помощник, документы, анализ жалоб
    vision  запросы с картинками (OCR паспорта, скриншоты из дел); DeepSeek картинки
            не принимает, поэтому сюда обычно ставят другую модель/поставщика
    embed   векторы для RAG (/embeddings)
    stt     распознавание речи (/audio/transcriptions, напр. whisper)

Для роли X читаются переменные LLM_X_BASE_URL / LLM_X_API_KEY / LLM_X_MODEL.
Пустые значения берутся из общих LLM_BASE_URL / LLM_API_KEY. Минимальная настройка —
три переменные:

    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_API_KEY=sk-...
    LLM_SMART_MODEL=deepseek-chat
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

ROLES = ("fast", "smart", "vision", "embed", "stt")

# Значение по умолчанию для модели роли, если LLM_<ROLE>_MODEL не задана.
_MODEL_FALLBACK = {
    "fast": "smart",      # нет отдельной быстрой модели — берём основную
    "vision": "smart",    # основная модель должна уметь картинки, иначе задайте LLM_VISION_*
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Endpoint:
    role: str
    base_url: str
    api_key: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


@lru_cache(maxsize=None)
def endpoint(role: str) -> Endpoint:
    """Поставщик и модель для роли. Кэшируется: переменные читаются один раз."""
    if role not in ROLES:
        raise ValueError(f"Неизвестная роль модели: {role!r}")
    up = role.upper()
    base_url = _env(f"LLM_{up}_BASE_URL") or _env("LLM_BASE_URL")
    api_key = _env(f"LLM_{up}_API_KEY") or _env("LLM_API_KEY")
    model = _env(f"LLM_{up}_MODEL")
    if not model and role in _MODEL_FALLBACK:
        fallback = endpoint(_MODEL_FALLBACK[role])
        model = fallback.model
        # Модель взята у другой роли — значит, и поставщик тот же, если свой не задан.
        if not _env(f"LLM_{up}_BASE_URL"):
            base_url = fallback.base_url
        if not _env(f"LLM_{up}_API_KEY"):
            api_key = fallback.api_key
    return Endpoint(role=role, base_url=base_url.rstrip("/"), api_key=api_key, model=model)


def reset_cache() -> None:
    """Сбросить кэш настроек (тесты, смена ключа без рестарта)."""
    endpoint.cache_clear()


# --- Поведение запросов -------------------------------------------------------

def request_timeout() -> float:
    return float(_env("LLM_TIMEOUT_SECONDS", "180"))


def json_mode() -> str:
    """Как просить JSON у модели, когда код задал response_mime_type=application/json.

    json_object  — response_format={"type": "json_object"} (DeepSeek, OpenAI, Qwen, Groq…)
    json_schema  — response_format с JSON Schema (OpenAI, часть vLLM); схема берётся из
                   response_schema, если код её задал, иначе как json_object
    prompt       — только инструкция в системном промпте (для API без response_format)
    """
    mode = _env("LLM_JSON_MODE", "json_object").lower()
    return mode if mode in ("json_object", "json_schema", "prompt") else "json_object"


def send_reasoning_effort() -> bool:
    """Передавать ли thinking_level как reasoning_effort (OpenAI o-серия, gpt-5, часть
    OpenRouter-моделей). По умолчанию выключено: многие API отвечают 400 на неизвестное поле."""
    return _env("LLM_SEND_REASONING_EFFORT", "false").lower() in ("1", "true", "yes")


def embed_send_dimensions() -> bool:
    """Передавать ли `dimensions` в /embeddings (поддерживают text-embedding-3-*)."""
    return _env("LLM_EMBED_SEND_DIMENSIONS", "false").lower() in ("1", "true", "yes")


def qpm_limit(role: str) -> Optional[int]:
    """Лимит запросов в минуту для роли (общий лимитер бота). None — дефолт лимитера."""
    raw = _env(f"LLM_{role.upper()}_QPM")
    return int(raw) if raw.isdigit() else None


def extra_headers() -> dict:
    """Доп. заголовки ко всем запросам. OpenRouter просит HTTP-Referer и X-Title."""
    headers = {}
    if _env("LLM_HTTP_REFERER"):
        headers["HTTP-Referer"] = _env("LLM_HTTP_REFERER")
    if _env("LLM_APP_TITLE"):
        headers["X-Title"] = _env("LLM_APP_TITLE")
    return headers


def is_configured() -> bool:
    return endpoint("smart").configured
