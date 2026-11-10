"""Репорты пользователей: список, переписка, ответ, закрытие."""

import asyncio
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from telegram.ext import ContextTypes

from database.manager import db_manager
from google_sheets_handler import log_admin_action_to_sheet
from config import (
    ADMIN_MANAGE_REPORTS,
    ADMIN_VIEWING_REPORTS_LIST,
    ADMIN_CONVERSATION_IN_REPORT,
    AWAITING_ADMIN_REPLY_IN_REPORT,
    ADMIN_CONFIRM_CLOSE_ALL_REPORTS,
)


async def manage_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'") or 0
    closed_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'closed'") or 0
    keyboard = [
        [InlineKeyboardButton(f"🔵 Активные ({open_reports_count})", callback_data="view_reports_open")],
        [InlineKeyboardButton(f"⚪️ Архив ({closed_reports_count})", callback_data="view_reports_closed")],
    ]

    # Добавляем кнопку массового закрытия, если есть активные репорты
    if open_reports_count > 0:
        keyboard.append([InlineKeyboardButton(f"🔴 Закрыть все активные ({open_reports_count})", callback_data="close_all_reports_confirm")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])

    await query.edit_message_text("📬 Управление репортами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MANAGE_REPORTS


async def _display_reports_list(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, status: str, page: int = 0):
    """Отображает список репортов с пагинацией."""
    REPORTS_PER_PAGE = 10  # Максимум репортов на одной странице
    
    reports = await db_manager.fetch(
        "SELECT r.id, r.title, r.last_message_at, u.full_name FROM reports r JOIN users u ON r.user_id = u.user_id WHERE r.status = $1 ORDER BY r.last_message_at DESC;",
        status
    )
    title_text = "🔵 Активные репорты" if status == 'open' else "⚪️ Архив репортов"
    
    if not reports:
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="manage_reports")]]
        await query.edit_message_text(f"{title_text}\n\nЗдесь пока пусто.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    # Вычисляем пагинацию
    total_reports = len(reports)
    total_pages = (total_reports + REPORTS_PER_PAGE - 1) // REPORTS_PER_PAGE
    page = max(0, min(page, total_pages - 1))  # Ограничиваем страницу
    
    start_idx = page * REPORTS_PER_PAGE
    end_idx = min(start_idx + REPORTS_PER_PAGE, total_reports)
    page_reports = reports[start_idx:end_idx]
    
    keyboard = []
    for report in page_reports:
        button_text = f"#{report['id']} от {report['full_name']} - {report['title'][:30]}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"admin_open_report_{report['id']}")])
    
    # Добавляем навигацию по страницам
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"reports_page_{status}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("След. ➡️", callback_data=f"reports_page_{status}_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_reports")])
    
    # Добавляем информацию о странице
    page_info = f"\n\nСтраница {page + 1}/{total_pages} (всего: {total_reports})"
    await query.edit_message_text(f"{title_text}:{page_info}", reply_markup=InlineKeyboardMarkup(keyboard))


async def view_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    status = query.data.split('_')[-1]
    context.user_data['report_list_status'] = status
    await _display_reports_list(query, context, status, page=0)
    return ADMIN_VIEWING_REPORTS_LIST


async def navigate_reports_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик навигации по страницам списка репортов."""
    query = update.callback_query
    await query.answer()
    
    # Парсим callback_data: reports_page_{status}_{page}
    parts = query.data.split('_')
    status = parts[2]  # 'open' или 'closed'
    page = int(parts[3])
    
    await _display_reports_list(query, context, status, page=page)
    return ADMIN_VIEWING_REPORTS_LIST


async def _build_report_view_for_admin(report_id: int, context: ContextTypes.DEFAULT_TYPE):
    report_info = await db_manager.fetchrow(
        "SELECT r.id, r.status, r.user_id, u.full_name, u.username FROM reports r JOIN users u ON r.user_id = u.user_id WHERE r.id = $1",
        report_id
    )
    if not report_info: return None, None
    context.user_data.update({'report_user_id': report_info['user_id'], 'current_report_id': report_id})
    messages = await db_manager.fetch(
        "SELECT sender_role, content, sender_id FROM report_messages WHERE report_id = $1 ORDER BY timestamp ASC;",
        report_id
    )
    import html as _html
    full_name_safe = _html.escape(str(report_info['full_name'] or ''))
    username_safe = _html.escape(str(report_info['username'] or 'N/A'))
    header = (f"Диалог по репорту #{report_id}\nСтатус: {report_info['status'].upper()}\n"
              f"Пользователь: {full_name_safe} (@{username_safe}, <code>{report_info['user_id']}</code>)\n" + "=" * 20)

    # --- ИСПРАВЛЕННАЯ ЛОГИКА ---
    conversation_parts = []
    for msg in messages:
        if msg['sender_role'] == 'user':
            sender = "Пользователь"
        else:
            sender = f"Админ ({msg['sender_id']})"
        content_safe = _html.escape(str(msg['content'] or ''))
        conversation_parts.append(f"<b>{sender}:</b>\n{content_safe}")
    conversation_text = "\n\n".join(conversation_parts)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    full_text = f"{header}\n\n{conversation_text}"
    keyboard = []
    if report_info['status'] == 'open':
        keyboard.append([InlineKeyboardButton("✍️ Ответить", callback_data=f"admin_reply_report_{report_id}"),
                         InlineKeyboardButton("✅ Закрыть", callback_data=f"admin_close_report_{report_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад к списку",
                                          callback_data=f"view_reports_{context.user_data.get('report_list_status', 'open')}")])
    return full_text, InlineKeyboardMarkup(keyboard)


async def view_report_conversation_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    report_id = int(query.data.split('_')[-1])
    text, reply_markup = await _build_report_view_for_admin(report_id, context)
    if not text:
        await query.edit_message_text("Ошибка: репорт не найден.")
        return ADMIN_VIEWING_REPORTS_LIST
    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    return ADMIN_CONVERSATION_IN_REPORT


async def prompt_admin_for_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("◀️ Отмена",
                                      callback_data=f"admin_open_report_{context.user_data['current_report_id']}")]]
    await query.edit_message_text("Введите ваш ответ пользователю.", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_ADMIN_REPLY_IN_REPORT


async def handle_admin_reply_in_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    message_text = update.message.text
    report_id = context.user_data['current_report_id']
    user_id = context.user_data['report_user_id']
    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) VALUES ($1, $2, 'admin', $3);",
        report_id, admin_id, message_text)
    await db_manager.execute("UPDATE reports SET last_message_at = NOW() WHERE id = $1;", report_id)
    logging.info(f"Администратор {admin_id} ответил в репорт #{report_id}.")
    try:
        await context.bot.send_message(chat_id=user_id,
                                       text=f"‼️ **Получен ответ от администрации по вашему репорту #{report_id}**",
                                       reply_markup=InlineKeyboardMarkup(
                                           [[InlineKeyboardButton("📂 Просмотреть", callback_data="start_report_dialog")]]),
                                       parse_mode='Markdown')
    except Exception as e:
        await context.bot.send_message(chat_id=admin_id, text=f"⚠️ Не удалось уведомить пользователя: {e}")
    await update.message.delete()
    text, reply_markup = await _build_report_view_for_admin(report_id, context)
    if text:
        await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup, parse_mode='HTML')
    return ADMIN_CONVERSATION_IN_REPORT


async def close_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Репорт закрыт.", show_alert=True)
    admin_id = query.from_user.id
    report_id = context.user_data['current_report_id']
    await db_manager.execute("UPDATE reports SET status = 'closed' WHERE id = $1;", report_id)
    logging.info(f"Администратор {admin_id} закрыл репорт #{report_id}.")
    await log_admin_action_to_sheet(admin_id, "REPORT_CLOSE", f"Report ID: {report_id}")
    try:
        await context.bot.send_message(chat_id=context.user_data['report_user_id'],
                                       text=f"✅ Ваш репорт #{report_id} был закрыт администрацией.")
    except Exception as e:
        logging.warning(f"Не удалось уведомить пользователя о закрытии репорта: {e}")
    await _display_reports_list(query, context, context.user_data.get('report_list_status', 'open'))
    return ADMIN_VIEWING_REPORTS_LIST


async def confirm_close_all_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает подтверждение на массовое закрытие всех активных репортов."""
    query = update.callback_query
    await query.answer()

    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'") or 0

    if open_reports_count == 0:
        await query.edit_message_text("Нет активных репортов для закрытия.")
        return await manage_reports_menu(update, context)

    keyboard = [
        [InlineKeyboardButton("✅ Да, закрыть все", callback_data="close_all_reports_execute")],
        [InlineKeyboardButton("❌ Отмена", callback_data="manage_reports")]
    ]

    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите закрыть все {open_reports_count} активных репортов?\n\n"
        f"Пользователи НЕ получат уведомления о закрытии.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_CONFIRM_CLOSE_ALL_REPORTS


async def execute_close_all_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет массовое закрытие всех активных репортов без уведомления пользователей."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    # Получаем количество репортов для закрытия
    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'") or 0

    if open_reports_count == 0:
        await query.edit_message_text("Нет активных репортов для закрытия.")
        return await manage_reports_menu(update, context)

    # Закрываем все активные репорты
    await db_manager.execute("UPDATE reports SET status = 'closed' WHERE status = 'open';")

    logging.info(f"Администратор {admin_id} закрыл все активные репорты (количество: {open_reports_count}).")
    await log_admin_action_to_sheet(admin_id, "REPORTS_CLOSE_ALL", f"Closed: {open_reports_count} reports")

    await query.edit_message_text(
        f"✅ Успешно закрыто репортов: {open_reports_count}\n\n"
        f"Пользователи не были уведомлены."
    )

    # Возвращаемся в меню управления репортами
    await asyncio.sleep(2)
    return await manage_reports_menu(update, context)
