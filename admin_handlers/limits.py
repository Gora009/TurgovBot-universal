"""Лимиты пользователей: сброс одному и всем."""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from database.manager import db_manager
from integrations.admin_log import log_admin_action_to_sheet
from config import MANAGE_LIMITS, GET_ID_FOR_LIMIT_RESET, CONFIRM_RESET_ALL_LIMITS

from .core import admin_panel, request_user_id


# ========== УПРАВЛЕНИЕ ЛИМИТАМИ ==========

async def manage_limits_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("💥 Сбросить всем пользователям", callback_data="reset_limits_all_confirm")],
        [InlineKeyboardButton("👤 Сбросить конкретному пользователю", callback_data="reset_limits_user")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text(
        text="⚙️ Управление лимитами использования.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MANAGE_LIMITS


async def pre_reset_user_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(
        update.callback_query, context, "сброса лимитов", GET_ID_FOR_LIMIT_RESET
    )


async def reset_user_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        target_id = int(update.message.text)
    except (ValueError, TypeError):
        await update.message.reply_text("ID должен быть числом.")
        return GET_ID_FOR_LIMIT_RESET
    target_user_exists = await db_manager.fetchval("SELECT 1 FROM users WHERE user_id = $1", target_id)
    if not target_user_exists:
        await update.message.reply_text(f"Пользователь с ID {target_id} не найден.")
        return await admin_panel(update, context)
    # Сбрасываем дневные лимиты
    query = "DELETE FROM user_usage WHERE user_id = $1 AND usage_date = CURRENT_DATE;"
    await db_manager.execute(query, target_id)
    # Сбрасываем лимиты ролевого помощника
    query_assistant = "DELETE FROM assistant_usage_log WHERE user_id = $1;"
    await db_manager.execute(query_assistant, target_id)
    await update.message.reply_text(f"✅ Все лимиты для пользователя {target_id} сброшены (включая ролевого помощника).")
    admin_id = update.effective_user.id
    logging.info(f"Администратор {admin_id} сбросил все лимиты для {target_id}.")
    await log_admin_action_to_sheet(admin_id, "LIMITS_RESET_SINGLE", f"Target ID: {target_id}")
    return await admin_panel(update, context)


async def confirm_reset_all_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("ДА, сбросить всем", callback_data="reset_limits_all_execute")],
        [InlineKeyboardButton("НЕТ, отмена", callback_data="manage_limits")]
    ]
    await query.answer()
    await query.edit_message_text(
        text="⚠️ **ВЫ УВЕРЕНЫ?**\n\nЭто действие сбросит дневные лимиты для **ВСЕХ** пользователей.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CONFIRM_RESET_ALL_LIMITS


async def execute_reset_all_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Выполняю сброс...")
    # Сбрасываем дневные лимиты
    db_query = "DELETE FROM user_usage WHERE usage_date = CURRENT_DATE;"
    result = await db_manager.execute(db_query)
    users_affected = int(result.split()[-1]) if result and result.split() else 0
    # Сбрасываем лимиты ролевого помощника для ВСЕХ пользователей
    db_query_assistant = "DELETE FROM assistant_usage_log;"
    result_assistant = await db_manager.execute(db_query_assistant)
    assistant_affected = int(result_assistant.split()[-1]) if result_assistant and result_assistant.split() else 0
    await query.edit_message_text(
        f"✅ Глобальный сброс лимитов завершен.\n"
        f"• Дневные лимиты: {users_affected} пользователей\n"
        f"• Ролевой помощник: {assistant_affected} записей"
    )
    admin_id = update.effective_user.id
    logging.info(f"Администратор {admin_id} сбросил все лимиты для ВСЕХ (daily: {users_affected}, assistant: {assistant_affected}).")
    await log_admin_action_to_sheet(admin_id, "LIMITS_RESET_ALL", f"Daily: {users_affected}, Assistant: {assistant_affected}")
    await admin_panel(update, context)
    return ConversationHandler.END
