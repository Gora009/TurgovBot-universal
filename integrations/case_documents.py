"""Документы уголовных дел в формате .docx (вместо Google Docs).

Раньше бот копировал шаблон в Google Docs и дописывал в конец сгенерированные части
дела. Теперь то же самое делается локально через python-docx:

  * основа — шаблон CCW_TEMPLATE_DOCX (путь к .docx; можно выгрузить старый шаблон из
    Google Docs: Файл → Скачать → .docx). Без шаблона создаётся пустой документ;
  * файл кладётся в хранилище (common/storage) как case_documents/<id>.docx;
  * ссылка — подписанная, отдаётся API по /files/... (нужны PUBLIC_BASE_URL и
    FILES_SIGNING_SECRET). Срок жизни ссылки — CCW_LINK_DAYS (по умолчанию 365 дней).

Интерфейс прежний: create_criminal_case_document(case_number) → (doc_id, doc_url) и
fill_criminal_case_document(doc_id, ...) → True, поэтому сценарий уголовного дела не менялся.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import uuid
from datetime import timedelta
from typing import Dict, Tuple

from docx import Document
from docx.shared import Pt

from common import storage

logger = logging.getLogger(__name__)

PARTS_ORDER = [
    ('inventory', 'ОПИСЬ ДОКУМЕНТОВ'),
    ('accused_card', 'КАРТОЧКА ОБВИНЯЕМОГО'),
    ('case_materials', 'МАТЕРИАЛЫ ДЕЛА'),
    ('indictment', 'ОБВИНИТЕЛЬНОЕ ЗАКЛЮЧЕНИЕ'),
    ('summons_list', 'СПИСОК ВЫЗЫВАЕМЫХ В СУД'),
    ('prosecution_demands', 'ТРЕБОВАНИЯ ПРОКУРАТУРЫ'),
]

_TAG_RE = re.compile(r"<[^>]+>")


def _blob_name(doc_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id or ""):
        raise ValueError(f"Некорректный id документа: {doc_id!r}")
    return f"case_documents/{doc_id}.docx"


def _link_days() -> int:
    try:
        return int(os.getenv("CCW_LINK_DAYS", "365"))
    except ValueError:
        return 365


def _new_document(case_number: str) -> Document:
    template = os.getenv("CCW_TEMPLATE_DOCX", "").strip()
    if template and os.path.isfile(template):
        return Document(template)
    if template:
        logger.warning("[CaseDocs] шаблон %s не найден — создаю пустой документ", template)
    doc = Document()
    doc.add_heading(f"Уголовное дело №{case_number}", level=0)
    return doc


def _save(doc: Document, doc_id: str) -> None:
    buf = io.BytesIO()
    doc.save(buf)
    storage.get_bucket().blob(_blob_name(doc_id)).upload_from_string(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def get_document_url(doc_id: str) -> str:
    return storage.signed_url(_blob_name(doc_id), timedelta(days=_link_days()))


async def create_criminal_case_document(case_number: str) -> Tuple[str, str]:
    """Создаёт документ дела из шаблона. Возвращает (doc_id, ссылка на скачивание)."""
    doc_id = uuid.uuid4().hex
    doc = _new_document(case_number)
    await asyncio.to_thread(_save, doc, doc_id)
    url = get_document_url(doc_id)
    logger.info("[CaseDocs] создан документ %s для дела %s", doc_id, case_number)
    return doc_id, url


def _plain(text: str) -> str:
    """Части дела приходят с HTML-разметкой Telegram — в документ кладём чистый текст."""
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&amp;", "&"))


def fill_criminal_case_document(doc_id: str, case_number: str, collected_data: Dict,
                                final_parts: Dict[str, str], template_structure: Dict = None) -> bool:
    """Дописывает в конец документа данные дела и все сгенерированные части."""
    blob = storage.get_bucket().blob(_blob_name(doc_id))
    doc = Document(io.BytesIO(blob.download_as_bytes()))

    doc.add_heading(f"УГОЛОВНОЕ ДЕЛО №{case_number}", level=1)
    if collected_data:
        if collected_data.get('accused_name'):
            doc.add_paragraph(f"Обвиняемый: {collected_data['accused_name']}")
        if collected_data.get('charges'):
            doc.add_paragraph(f"Статьи обвинения: {collected_data['charges']}")

    added = 0
    for part_type, title in PARTS_ORDER:
        content = (final_parts or {}).get(part_type)
        if not content:
            continue
        doc.add_heading(title, level=2)
        for line in _plain(content).split("\n"):
            p = doc.add_paragraph(line)
            for run in p.runs:
                run.font.size = Pt(11)
        added += 1
    if not added:
        doc.add_paragraph("[Части дела не сгенерированы]")

    _save(doc, doc_id)
    logger.info("[CaseDocs] документ %s заполнен: %d частей", doc_id, added)
    return True


def _cleanup_service_account_storage() -> int:
    """Раньше чистил Google Drive сервис-аккаунта. Локальному хранилищу это не нужно."""
    return 0
