"""Текст документов по их URI (законы и прецеденты) для запросов к модели.

Gemini читал законы сам по ссылке gs://…; OpenAI-совместимые API так не умеют, поэтому
адаптер подставляет в запрос сам текст. Источники по порядку:

  1. локальное хранилище (common.storage): local://… и перенесённые gs://<bucket>/…;
  2. БД: law_documents.content / precedents.full_content по gemini_file_uri — так база,
     перенесённая со старой версии бота, работает без повторной загрузки файлов.

Тексты кэшируются в памяти (законы большие и одни и те же для многих вопросов).
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Dict, Iterable, Optional

from common import storage

logger = logging.getLogger(__name__)

_CACHE_MAX = 512
_cache: "OrderedDict[str, tuple]" = OrderedDict()  # uri -> (mtime | None, text)


def _cache_get(uri: str, mtime: Optional[float]) -> Optional[str]:
    hit = _cache.get(uri)
    if hit and hit[0] == mtime:
        _cache.move_to_end(uri)
        return hit[1]
    return None


def _cache_put(uri: str, mtime: Optional[float], text: str) -> None:
    _cache[uri] = (mtime, text)
    _cache.move_to_end(uri)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


def _read_local(uri: str) -> Optional[tuple]:
    name = storage.blob_name_from_uri(uri)
    if not name:
        return None
    try:
        blob = storage.get_bucket().blob(name)
    except ValueError:
        return None
    if not blob.exists():
        return None
    mtime = blob.path.stat().st_mtime
    cached = _cache_get(uri, mtime)
    if cached is not None:
        return mtime, cached
    return mtime, blob.download_as_text()


async def _read_db(uri: str) -> Optional[str]:
    from database.manager import db_manager

    row = await db_manager.fetchrow(
        "SELECT title, content FROM law_documents WHERE gemini_file_uri = $1 LIMIT 1", uri)
    if row and row["content"]:
        return f"НАЗВАНИЕ ДОКУМЕНТА: {row['title']}\n\n{row['content']}"
    row = await db_manager.fetchrow(
        "SELECT title, summary, full_content, precedent_type FROM precedents "
        "WHERE gemini_file_uri = $1 LIMIT 1", uri)
    if row and row["full_content"]:
        text = f"ТИП ДОКУМЕНТА: {(row['precedent_type'] or 'precedent').upper()}\nНАЗВАНИЕ: {row['title']}\n"
        if row["summary"]:
            text += f"КРАТКОЕ ОПИСАНИЕ: {row['summary']}\n"
        return text + f"\n{row['full_content']}"
    return None


async def resolve_document(uri: str) -> Optional[str]:
    local = await asyncio.to_thread(_read_local, uri)
    if local is not None:
        mtime, text = local
        _cache_put(uri, mtime, text)
        return text
    cached = _cache_get(uri, None)
    if cached is not None:
        return cached
    try:
        text = await _read_db(uri)
    except Exception:
        logger.exception("[LLM] не удалось прочитать документ %s из БД", uri)
        return None
    if text is not None:
        _cache_put(uri, None, text)
    return text


async def resolve_documents(uris: Iterable[str]) -> Dict[str, str]:
    uris = [u for u in dict.fromkeys(uris) if u]
    if not uris:
        return {}
    texts = await asyncio.gather(*(resolve_document(u) for u in uris))
    return {u: t for u, t in zip(uris, texts) if t is not None}
