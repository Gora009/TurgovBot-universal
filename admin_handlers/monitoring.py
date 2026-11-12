"""Мониторинг Vertex AI API и очистка хранилища Google Drive."""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import CHOOSING_ACTION


async def show_api_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает статистику использования Vertex AI API через rate limiter."""
    from common.rate_limiter import vertex_rate_limiter

    query = update.callback_query
    await query.answer()

    # Получаем статистику
    stats = vertex_rate_limiter.get_stats()

    # Формируем сообщение
    text = "📊 <b>Мониторинг Vertex AI API</b>\n\n"

    # Общая статистика
    text += "<b>📈 Общая статистика:</b>\n"
    text += f"├ Всего запросов: <code>{stats['total_requests']}</code>\n"
    text += f"├ Успешных: <code>{stats['successful_requests']}</code> ✅\n"
    text += f"├ Неудачных: <code>{stats['failed_requests']}</code> ❌\n"
    text += f"├ Таймауты очереди: <code>{stats['queue_timeouts']}</code> ⏱\n"
    text += f"└ ResourceExhausted: <code>{stats['resource_exhausted_errors']}</code> 🚫\n\n"

    # Success rate
    if stats['total_requests'] > 0:
        success_rate = (stats['successful_requests'] / stats['total_requests']) * 100
        text += f"<b>Success Rate:</b> {success_rate:.1f}%\n\n"

    # Текущая нагрузка по моделям
    text += "<b>⚡ Текущая нагрузка (QPM):</b>\n"
    current_qpm = stats.get('current_qpm', {})
    qpm_limits = stats.get('qpm_limits', {})

    for model_name, limit in qpm_limits.items():
        current = current_qpm.get(model_name, 0)
        percentage = (current / limit) * 100 if limit > 0 else 0

        # Выбираем эмодзи в зависимости от загрузки
        if percentage < 50:
            emoji = "🟢"
        elif percentage < 80:
            emoji = "🟡"
        else:
            emoji = "🔴"

        text += f"{emoji} <code>{model_name}</code>:\n"
        text += f"   └ {current}/{limit} QPM ({percentage:.0f}%)\n"

    # Adaptive throttling статус
    text += "\n<b>🎛 Adaptive Throttling:</b>\n"
    adaptive = stats.get('adaptive_throttling', {})
    if any(adaptive.values()):
        for model_name, is_active in adaptive.items():
            if is_active:
                text += f"⚠️ <code>{model_name}</code>: АКТИВЕН\n"
    else:
        text += "✅ Не активен (все модели работают нормально)\n"

    # Счетчики ошибок
    error_counts = stats.get('error_counts', {})
    if any(error_counts.values()):
        text += "\n<b>⚠️ Счетчики ошибок:</b>\n"
        for model_name, count in error_counts.items():
            if count > 0:
                text += f"├ <code>{model_name}</code>: {count}\n"

    # Кнопки
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="api_monitoring")],
        [InlineKeyboardButton("♻️ Сбросить статистику", callback_data="api_reset_stats")],
        [InlineKeyboardButton("🗑️ Очистить Google Drive", callback_data="api_cleanup_gdrive")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel_from_monitoring")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    return CHOOSING_ACTION


async def reset_api_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сбрасывает статистику rate limiter."""
    from common.rate_limiter import vertex_rate_limiter

    query = update.callback_query
    await query.answer("Статистика сброшена ✅")

    vertex_rate_limiter.reset_stats()
    logging.info(f"Админ {query.from_user.id} сбросил статистику rate limiter")

    # Показываем обновленную статистику
    return await show_api_monitoring(update, context)


async def cleanup_gdrive_storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Очищает хранилище Google Drive сервисного аккаунта."""
    from integrations.case_documents import _cleanup_service_account_storage

    query = update.callback_query
    await query.answer("Начинаю очистку...")

    try:
        cleaned = _cleanup_service_account_storage()
        logging.info(f"Админ {query.from_user.id} запустил очистку Google Drive: удалено {cleaned} файлов")

        # Обновляем сообщение
        await query.edit_message_text(
            text=f"🗑️ <b>Очистка Google Drive завершена</b>\n\n"
                 f"✅ Очищено/удалено: {cleaned} файлов\n\n"
                 f"<i>Корзина очищена, старые документы CCW (>7 дней) удалены.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 К мониторингу", callback_data="api_monitoring")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel_from_monitoring")]
            ])
        )
    except Exception as e:
        logging.error(f"Ошибка при очистке Google Drive: {e}")
        await query.edit_message_text(
            text=f"❌ <b>Ошибка при очистке</b>\n\n<code>{str(e)[:500]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_monitoring")]
            ])
        )

    return CHOOSING_ACTION
