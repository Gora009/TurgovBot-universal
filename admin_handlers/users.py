"""Баны, HWID, универсальные ключи, ВИП, тестеры, техработы, админы."""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from telegram.ext import ContextTypes

from database.manager import db_manager
from config import (
    SUPER_ADMIN_ID,
    CHOOSING_ACTION,
    MANAGE_BANS,
    MANAGE_VIPS,
    MANAGE_ADMINS,
    GET_ID_FOR_BAN,
    GET_ID_FOR_UNBAN,
    GET_ID_FOR_VIP_ADD,
    GET_ID_FOR_VIP_REMOVE,
    GET_ID_FOR_ADMIN_ADD,
    GET_ID_FOR_ADMIN_REMOVE,
    MANAGE_TESTERS,
    GET_ID_FOR_TESTER_ADD,
    GET_ID_FOR_TESTER_REMOVE,
    MANAGE_HWID,
    GET_ID_FOR_HWID_RESET,
    GET_HWID_FOR_BAN,
    GET_HWID_FOR_UNBAN,
    GET_ID_FOR_HWID_VIEW,
)

from .core import admin_panel, request_user_id


async def manage_bans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🚫 Забанить пользователя", callback_data="ban_user")],
        [InlineKeyboardButton("🟢 Разбанить пользователя", callback_data="unban_user")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление блокировками:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_BANS


async def pre_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "бана", GET_ID_FOR_BAN)


async def pre_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "разбана", GET_ID_FOR_UNBAN)


# === Управление HWID-привязкой лицензий оверлея ===
async def manage_hwid_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🔓 Сбросить привязку (user_id)", callback_data="hwid_reset")],
        [InlineKeyboardButton("🔎 Показать привязку (user_id)", callback_data="hwid_view")],
        [InlineKeyboardButton("🚫 Забанить HWID", callback_data="hwid_ban")],
        [InlineKeyboardButton("🟢 Разбанить HWID", callback_data="hwid_unban")],
        [InlineKeyboardButton("🗝 Универсальный ключ (законка)", callback_data="unikey_menu")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
    ]
    await query.answer()
    await query.edit_message_text(
        "🖥 <b>Управление HWID-привязкой лицензий</b>\n"
        "Сброс привязки — позволяет юзеру переактивировать ключ на новом ПК.\n"
        "Бан HWID — блокирует железо (как каскад при бане юзера).\n"
        "Универсальный ключ — общий ключ laws-билда (поиск по законке) без привязки к железу.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
    )
    return MANAGE_HWID


async def _request_hwid_input(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                              prompt: str, state: int) -> int:
    await query.answer()
    context.user_data['admin_action'] = query.data
    context.user_data['next_state'] = state
    await query.edit_message_text(prompt)
    return state


async def pre_hwid_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🔓 Сброс привязки — пришли Telegram ID пользователя:", GET_ID_FOR_HWID_RESET)


async def pre_hwid_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🔎 Просмотр привязки — пришли Telegram ID пользователя:", GET_ID_FOR_HWID_VIEW)


async def pre_hwid_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🚫 Бан HWID — пришли строку HWID (отпечаток железа):", GET_HWID_FOR_BAN)


async def pre_hwid_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🟢 Разбан HWID — пришли строку HWID:", GET_HWID_FOR_UNBAN)


async def modify_hwid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Текстовый ввод действий HWID (диспетчеризация по admin_action)."""
    from services import license as _lic
    action = context.user_data.get('admin_action')
    text = (update.message.text or "").strip()
    admin_id = update.effective_user.id
    try:
        if action == 'hwid_reset':
            uid = int(text)
            ok = await _lic.reset_hwid(uid)
            await update.message.reply_text(
                f"✅ Привязка ключа пользователя {uid} сброшена — может активировать на новом ПК."
                if ok else f"У пользователя {uid} нет активного ключа.")
        elif action == 'hwid_view':
            uid = int(text)
            v = await _lic.user_license_view(uid)
            if not v.get("has_key"):
                await update.message.reply_text(f"У пользователя {uid} нет активного ключа.")
            else:
                # Полный HWID в <code> — тап в Telegram копирует целиком (нужно для бана).
                hwid_line = (f"<code>{v['hwid_full']}</code>" if v.get('hwid_full')
                             else "— (не активирован)")
                await update.message.reply_text(
                    f"🔑 Ключ: <code>{v['key']}</code>\n"
                    f"Привязка HWID: {hwid_line}\n"
                    f"Ребайндов: {v['rebind_count']}/{v['rebind_max']}\n"
                    f"HWID забанен: {'да' if v['hwid_banned'] else 'нет'}",
                    parse_mode='HTML')
        elif action == 'hwid_ban':
            await _lic.ban_hwid(text, banned_by=admin_id, reason="manual via admin")
            await update.message.reply_text(f"🚫 HWID забанен:\n<code>{text}</code>", parse_mode='HTML')
        elif action == 'hwid_unban':
            await _lic.unban_hwid(text)
            await update.message.reply_text(f"🟢 HWID разбанен:\n<code>{text}</code>", parse_mode='HTML')
        else:
            await update.message.reply_text("Неизвестное действие.")
    except (ValueError, TypeError):
        await update.message.reply_text("Нужно число (Telegram ID). Попробуй снова.")
        return context.user_data.get('next_state')
    except Exception:
        logging.exception("[LICENSE] modify_hwid_input failed action=%s", action)
        await update.message.reply_text("Ошибка. Подробности в логах.")
    return await admin_panel(update, context)


# === Универсальный ключ laws-билда (поиск по законке) ===
# Один ключ раздаётся всем вместе с laws-установщиком: активация на любом числе машин,
# без HWID-привязки. Отзыв централизованно гасит все устройства (≤ ~15 мин, TTL access-токена).
async def _unikey_render(query: CallbackQuery, notice: str = "") -> int:
    from services import license as _lic
    keys = await _lic.list_universal_keys()
    lines = [notice] if notice else []
    lines.append("🗝 <b>Универсальные ключи laws-билда</b>")
    lines.append("Ключ подходит всем (без привязки к железу), даёт ТОЛЬКО поиск по законке. "
                 "Отзыв гасит все устройства в течение ~15 минут.")
    keyboard = []
    if not keys:
        lines.append("\nАктивных универсальных ключей нет.")
    for k in keys:
        created = f" · создан {k['created_at']:%d.%m.%Y}" if k.get('created_at') else ""
        lines.append(f"\n<code>{k['key']}</code>\nустройств: {k['active_devices']}{created}")
        keyboard.append([InlineKeyboardButton(
            f"⛔️ Отозвать …{k['key'][-5:]}", callback_data=f"unikey_revoke_{k['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Создать ключ", callback_data="unikey_create")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_hwid")])
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return MANAGE_HWID


async def unikey_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _unikey_render(query)


async def unikey_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    from services import license as _lic
    key = await _lic.issue_universal_key(update.effective_user.id)
    return await _unikey_render(query, notice=f"✅ Создан универсальный ключ:\n<code>{key}</code>\n")


async def unikey_revoke_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key_id = int(query.data.rsplit('_', 1)[-1])
    from services import license as _lic
    k = next((x for x in await _lic.list_universal_keys() if x['id'] == key_id), None)
    if not k:
        return await _unikey_render(query, notice="Ключ уже отозван или не найден.\n")
    keyboard = [
        [InlineKeyboardButton("⛔️ Да, отозвать", callback_data=f"unikey_revoke_yes_{key_id}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="unikey_menu")],
    ]
    await query.edit_message_text(
        f"⛔️ Отозвать универсальный ключ?\n\n<code>{k['key']}</code>\n"
        f"Активных устройств: {k['active_devices']}\n\n"
        "ВСЕ laws-билды с этим ключом перестанут работать (в пределах ~15 минут), "
        "повторная активация станет невозможна. Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return MANAGE_HWID


async def unikey_revoke_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key_id = int(query.data.rsplit('_', 1)[-1])
    from services import license as _lic
    n = await _lic.revoke_universal_key(key_id)
    notice = ("Ключ уже был отозван или не найден.\n" if n is None
              else f"⛔️ Ключ отозван, устройств погашено: {n}.\n")
    return await _unikey_render(query, notice=notice)


async def manage_vips_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("➕ Добавить VIP", callback_data="add_vip")],
        [InlineKeyboardButton("➖ Удалить VIP", callback_data="remove_vip")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление VIP пользователями:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_VIPS


async def pre_add_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "добавления VIP", GET_ID_FOR_VIP_ADD)


async def pre_remove_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "удаления VIP", GET_ID_FOR_VIP_REMOVE)


# === УПРАВЛЕНИЕ ТЕСТЕРАМИ ===

async def manage_testers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню управления тестировщиками."""
    query = update.callback_query

    # Получаем список текущих тестеров
    testers = await db_manager.fetch(
        "SELECT user_id, full_name, username FROM users WHERE is_tester = TRUE ORDER BY full_name"
    )

    text = "🧪 <b>Управление тестировщиками</b>\n\n"
    if testers:
        text += "<b>Текущие тестеры:</b>\n"
        for t in testers[:10]:  # Показываем первых 10
            username_str = f"@{t['username']}" if t['username'] else "без username"
            text += f"• {t['full_name']} ({username_str}) — <code>{t['user_id']}</code>\n"
        if len(testers) > 10:
            text += f"\n<i>...и ещё {len(testers) - 10}</i>\n"
    else:
        text += "<i>Нет назначенных тестировщиков.</i>\n"

    text += "\n<i>Тестировщики имеют доступ к бета-функциям (например, создание уголовных дел).</i>"

    keyboard = [
        [InlineKeyboardButton("➕ Добавить тестера", callback_data="add_tester")],
        [InlineKeyboardButton("➖ Удалить тестера", callback_data="remove_tester")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_TESTERS


async def pre_add_tester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "добавления тестера", GET_ID_FOR_TESTER_ADD)


async def pre_remove_tester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "удаления тестера", GET_ID_FOR_TESTER_REMOVE)


# === РЕЖИМ ТЕХНИЧЕСКИХ РАБОТ ===

async def toggle_maintenance_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключает режим технических работ."""
    query = update.callback_query

    current_mode = context.application.bot_data.get('maintenance_mode', False)
    new_mode = not current_mode
    context.application.bot_data['maintenance_mode'] = new_mode
    # Персистим — чтобы режим переживал рестарт bot/БД.
    try:
        from common.utils import set_maintenance_mode
        await set_maintenance_mode(new_mode)
    except Exception:
        logging.warning("Не удалось сохранить флаг тех. работ в system_cache", exc_info=True)

    status_text = "🔴 ВКЛЮЧЁН" if new_mode else "🟢 ВЫКЛЮЧЕН"
    logging.info(f"Администратор {query.from_user.id} {'включил' if new_mode else 'выключил'} режим тех. работ")

    await query.answer(f"Режим тех. работ {status_text}", show_alert=True)

    # Возвращаемся в админ-панель для обновления статуса кнопки
    return await admin_panel(update, context)


async def manage_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Доступ запрещен.", show_alert=True)
        return CHOOSING_ACTION
    keyboard = [
        [InlineKeyboardButton("➕ Добавить админа", callback_data="add_admin")],
        [InlineKeyboardButton("➖ Удалить админа", callback_data="remove_admin")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление администраторами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_ADMINS


async def pre_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "добавления админа", GET_ID_FOR_ADMIN_ADD)


async def pre_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "удаления админа", GET_ID_FOR_ADMIN_REMOVE)
