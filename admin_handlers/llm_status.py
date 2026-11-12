"""Админ-панель: какие поставщики и модели стоят на ролях (fast/smart/vision/embed/stt)."""

import html
from urllib.parse import urlsplit

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import SUPER_ADMIN_ID, CHOOSING_ACTION
from common.llm import settings as llm_settings

_ROLE_TITLES = {
    "fast": "Быстрая (роутеры, сводки)",
    "smart": "Основная (ответы, документы)",
    "vision": "Картинки (OCR)",
    "embed": "Векторы (RAG)",
    "stt": "Распознавание речи",
}


def _mask(key: str) -> str:
    if not key:
        return "—"
    return key[:3] + "…" + key[-4:] if len(key) > 10 else "***"


def render_llm_status() -> str:
    lines = ["🔑 <b>Модели и ключи</b>", ""]
    for role in llm_settings.ROLES:
        ep = llm_settings.endpoint(role)
        title = _ROLE_TITLES.get(role, role)
        if not ep.configured:
            lines.append(f"⚪ <b>{title}</b>: не настроена")
            continue
        host = urlsplit(ep.base_url).netloc or ep.base_url
        lines.append(
            f"🟢 <b>{title}</b>\n"
            f"    <code>{html.escape(ep.model)}</code> @ {html.escape(host)}, ключ {html.escape(_mask(ep.api_key))}"
        )
    lines += ["", "<i>Меняется в .env (LLM_*) и перезапуском бота.</i>"]
    return "\n".join(lines)


async def llm_status_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для суперадмина", show_alert=True)
        return CHOOSING_ACTION
    await query.edit_message_text(
        render_llm_status(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]]),
    )
    return CHOOSING_ACTION
