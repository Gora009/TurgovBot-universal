"""Вход в админ-панель, главное меню, общий ввод списков пользователей."""

import logging
import html
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    MessageOriginUser,
)
from telegram.ext import ContextTypes, ConversationHandler

from database.manager import db_manager
from integrations.admin_log import log_admin_action_to_sheet
from config import SUPER_ADMIN_ID, CHOOSING_ACTION
from services.promo_service import is_promo_active
from services.tester_applications import count_pending as ta_count_pending
from services.vip_applications import count_pending as vip_count_pending
from services.support_tickets import count_open as supp_count_open


async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Проверка, что есть и пользователь, и обычное сообщение (не канал / не отредактированное)
    if not user or not update.message:
        logging.warning("handle_forwarded_message: нет user или message, игнорирую.")
        return

    # Проверка бана
    user_record = await db_manager.fetchrow("SELECT is_banned, is_admin FROM users WHERE user_id = $1", user.id)
    if user_record and user_record['is_banned']:
        await update.message.reply_text("❌ Вы заблокированы и не можете использовать этого бота.")
        logging.warning(f"Заблокированный пользователь {user.id} попытался использовать handle_forwarded_message.")
        return

    is_admin = (user_record and user_record['is_admin']) or user.id == SUPER_ADMIN_ID

    # PTB 22: атрибут forward_from удалён. Источник пересылки теперь в forward_origin
    # (объект MessageOrigin*). ID исходного отправителя доступен ТОЛЬКО если это пользователь
    # без скрытия профиля (MessageOriginUser); для скрытого профиля/канала/чата ID недоступен.
    origin = update.message.forward_origin
    if is_admin and origin is not None:
        if isinstance(origin, MessageOriginUser):
            fwd_user = origin.sender_user
            logging.info(f"Администратор {user.id} запросил ID пользователя {fwd_user.id} через пересылку сообщения.")
            await update.message.reply_text(
                f"ID пользователя {html.escape(fwd_user.full_name)}: <code>{fwd_user.id}</code>",
                parse_mode='HTML')
        else:
            await update.message.reply_text(
                "Не удалось получить ID: отправитель скрыл профиль настройками приватности "
                "либо сообщение переслано из канала/чата.")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    user_record = await db_manager.fetchrow("SELECT is_admin, is_banned FROM users WHERE user_id = $1", user_id)

    # Проверка бана
    if user_record and user_record['is_banned']:
        logging.warning(f"Заблокированный пользователь {user_id} попытался получить доступ к админ-панели.")
        if update.callback_query:
            await update.callback_query.answer("❌ Вы заблокированы.", show_alert=True)
        return ConversationHandler.END

    is_admin = (user_record and user_record['is_admin']) or user_id == SUPER_ADMIN_ID

    if not is_admin:
        logging.warning(f"Пользователь {user_id} попытался получить доступ к админ-панели без прав.")
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещен.")
        return ConversationHandler.END

    context.user_data.clear()

    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'")
    reports_button_text = f"📬 Управление репортами ({open_reports_count or 0})"

    loophole_queued_count = await db_manager.fetchval(
        "SELECT COUNT(*) FROM loophole_jobs WHERE status = 'queued' AND is_out_of_band = false"
    )
    loophole_button_text = f"🕳 Очередь анализатора ({loophole_queued_count or 0})"

    tester_apps_pending = await ta_count_pending()
    tester_apps_button_text = f"📝 Заявки в тестеры ({tester_apps_pending})"

    vip_apps_pending = await vip_count_pending()
    vip_apps_button_text = f"💎 Заявки на ВИП ({vip_apps_pending})"

    supp_open = await supp_count_open()
    supp_button_text = f"🆘 Обращения ({supp_open})"

    # Проверяем статус режима тех. работ
    maintenance_mode = context.application.bot_data.get('maintenance_mode', False)
    maintenance_status = "🔴 ВКЛ" if maintenance_mode else "🟢 ВЫКЛ"

    keyboard = [
        [InlineKeyboardButton("🚫 Управление банами", callback_data="manage_bans")],
        [InlineKeyboardButton("🖥 HWID-привязка оверлея", callback_data="manage_hwid")],
        [InlineKeyboardButton("👑 Управление VIP", callback_data="manage_vips")],
        [InlineKeyboardButton("🧪 Управление тестерами", callback_data="manage_testers")],
        [InlineKeyboardButton(tester_apps_button_text, callback_data="manage_tester_apps")],
        [InlineKeyboardButton(vip_apps_button_text, callback_data="manage_vip_apps")],
        [InlineKeyboardButton(supp_button_text, callback_data="manage_support")],
        [InlineKeyboardButton("🔍 Поиск пользователей", callback_data="user_search")],
        [InlineKeyboardButton("📋 Просмотр дела по ID", callback_data="view_case_by_id")],
        [InlineKeyboardButton("🖥️ Управление серверами", callback_data="manage_servers")],
        [InlineKeyboardButton("⚖️ Просмотр прецедентов", callback_data="manage_precedents")],
        [InlineKeyboardButton("⚙️ Управление лимитами", callback_data="manage_limits")],
        [InlineKeyboardButton(reports_button_text, callback_data="manage_reports")],
        [InlineKeyboardButton(loophole_button_text, callback_data="manage_loophole_queue")],
        [InlineKeyboardButton("📊 Мониторинг API", callback_data="api_monitoring")],
        [InlineKeyboardButton("🔑 Управление REST API", callback_data="manage_api_keys")],
        [InlineKeyboardButton(f"🛠️ Тех. работы: {maintenance_status}", callback_data="toggle_maintenance")],
    ]
    if user_id == SUPER_ADMIN_ID:
        keyboard.insert(3, [InlineKeyboardButton("🧑‍⚖️ Управление админами", callback_data="manage_admins")])
        keyboard.append([InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")])
        # Кнопка промо-акции
        promo_active = await is_promo_active()
        promo_status = "🟢 ВКЛ" if promo_active else "⚪ ВЫКЛ"
        keyboard.append([InlineKeyboardButton(f"🎉 Промо-акция: {promo_status}", callback_data="manage_promo")])
        keyboard.append([InlineKeyboardButton("🔑 Модели и ключи", callback_data="llm_status")])

    keyboard.append([InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "🔑 Админ-панель"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=text, reply_markup=reply_markup)

    return CHOOSING_ACTION


async def request_user_id(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, action_text: str,
                          state_to_return: int) -> int:
    await query.answer()
    context.user_data['admin_action'] = query.data
    context.user_data['next_state'] = state_to_return
    await query.edit_message_text(f"Введите Telegram ID для {action_text}:")
    return state_to_return


async def modify_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        target_id = int(update.message.text)
    except (ValueError, TypeError):
        await update.message.reply_text("ID должен быть числом. Попробуйте снова.")
        return context.user_data.get('next_state')

    target_user_exists = await db_manager.fetchval("SELECT 1 FROM users WHERE user_id = $1", target_id)
    if not target_user_exists:
        await update.message.reply_text(f"Пользователь с ID {target_id} не найден в базе данных.")
        return await admin_panel(update, context)

    action_key = context.user_data.get('admin_action')
    actions_map = {
        'ban_user': ('is_banned', True, 'бан'),
        'unban_user': ('is_banned', False, 'разбан'),
        'add_vip': ('is_vip', True, 'добавление VIP'),
        'remove_vip': ('is_vip', False, 'удаление VIP'),
        'add_admin': ('is_admin', True, 'назначение админом'),
        'remove_admin': ('is_admin', False, 'снятие с админа'),
        'add_tester': ('is_tester', True, 'добавление тестера'),
        'remove_tester': ('is_tester', False, 'удаление тестера'),
    }

    params = actions_map.get(action_key)
    if not params:
        await update.message.reply_text("Произошла внутренняя ошибка. Действие не найдено.")
        logging.error(f"Не найдены параметры для действия '{action_key}' в admin_handlers.modify_user_list.")
        return await admin_panel(update, context)

    db_field, db_value, log_name = params

    if target_id == SUPER_ADMIN_ID and db_field in ('is_admin', 'is_banned'):
        await update.message.reply_text("Нельзя изменить статус или забанить Супер-Админа.")
        return await admin_panel(update, context)

    query = f"UPDATE users SET {db_field} = $1 WHERE user_id = $2;"
    await db_manager.execute(query, db_value, target_id)

    # Бан-каскад на HWID: бан юзера → его привязанное железо в hwid_bans (нельзя переактивировать
    # оверлей на том же ПК даже с нового аккаунта); разбан → снять КАСКАДНЫЕ HWID-баны (ручные не трогаем).
    hwid_note = ""
    if db_field == 'is_banned':
        try:
            from services import license as _lic
            if db_value:
                n = await _lic.ban_hwids_for_user(target_id, banned_by=update.effective_user.id)
                hwid_note = f" (HWID забанено: {n})" if n else " (привязанного HWID нет)"
            else:
                await _lic.unban_hwids_for_user(target_id)
                hwid_note = " (каскадные HWID-баны сняты)"
        except Exception:
            logging.exception("[LICENSE] ban-cascade failed user_id=%s", target_id)

    result_message = f"Статус '{log_name}' успешно {'применен к' if db_value else 'снят с'} пользователя {target_id}.{hwid_note}"
    await update.message.reply_text(result_message)

    log_action = f"{log_name.upper().replace(' ', '_')}"
    logging.info(
        f"Администратор {update.effective_user.id} выполнил действие: {log_action} для пользователя {target_id}.")
    await log_admin_action_to_sheet(update.effective_user.id, log_action, f"Target ID: {target_id}")

    return await admin_panel(update, context)
