"""Сервис Complaint Analyzer: бизнес-логика анализа жалоб для API и CMS.

Раньше всё было в одном complaint_analyzer_service.py (~3000 строк). Разбито так:

    forms          парсинг жалобы по ссылке, шаблон формы, типы нарушений
    laws           подбор релевантных законов (RAG и полный текст), включая CMS
    analysis       проверка формы, анализ, извлечение фактов, разбор JSON модели
    documents      тексты документов: ответ на жалобу, АГО, постановление
    cms_analysis   CMS: полный и комбинированный анализ, теги, метки доказательств
    cms_drafts     CMS: черновики материалов дела, план и его адаптация
    cms_passport   CMS: паспортные данные
    cms_free_form  CMS: ответ на произвольный запрос

Логгер во всех подмодулях — 'services.complaint_analyzer_service', как и до разбиения.
"""

from .analysis import analyze_complaint, check_complaint_form, extract_complaint_facts
from .cms_analysis import (
    analyze_complaint_combined_cms,
    analyze_complaint_full_cms,
    get_evidence_type_labels_cms,
    get_issue_spotting_tags_cms,
    validate_form_ai_cms,
)
from .cms_drafts import (
    adapt_plan_ai_cms,
    generate_case_file_draft_cms,
    generate_case_file_draft_with_feedback_cms,
    generate_case_file_draft_with_rag_cms,
    generate_plan_ai_cms,
)
from .cms_free_form import generate_free_form_response_cms
from .cms_passport import extract_passport_data_cms, resolve_passport_url_cms
from .documents import (
    generate_ago_document_text,
    generate_complaint_document_text,
    generate_custom_resolution_text,
)
from .forms import (
    add_issue_type,
    delete_issue_type,
    get_form_template,
    get_issue_types,
    parse_complaint_from_url,
    set_form_template,
    update_issue_type,
)

__all__ = [
    'adapt_plan_ai_cms',
    'add_issue_type',
    'analyze_complaint',
    'analyze_complaint_combined_cms',
    'analyze_complaint_full_cms',
    'check_complaint_form',
    'delete_issue_type',
    'extract_complaint_facts',
    'extract_passport_data_cms',
    'generate_ago_document_text',
    'generate_case_file_draft_cms',
    'generate_case_file_draft_with_feedback_cms',
    'generate_case_file_draft_with_rag_cms',
    'generate_complaint_document_text',
    'generate_custom_resolution_text',
    'generate_free_form_response_cms',
    'generate_plan_ai_cms',
    'get_evidence_type_labels_cms',
    'get_form_template',
    'get_issue_spotting_tags_cms',
    'get_issue_types',
    'parse_complaint_from_url',
    'resolve_passport_url_cms',
    'set_form_template',
    'update_issue_type',
    'validate_form_ai_cms',
]
