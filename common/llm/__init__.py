"""Работа с языковыми моделями через любой OpenAI-совместимый API.

    from common.llm import CompatClient     # клиент с интерфейсом google-genai
    from common.llm import settings         # поставщики и модели по ролям (env LLM_*)
"""
from . import settings
from .compat import CompatClient, role_for_model

__all__ = ["CompatClient", "role_for_model", "settings"]
