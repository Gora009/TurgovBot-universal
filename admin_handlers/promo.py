"""Промо-акция: включение, срок, выключение."""

import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from integrations.admin_log import log_admin_action_to_sheet
from config import MANAGE_PROMO, GET_PROMO_END_DATE
from services.promo_service import is_promo_active, get_promo_info, set_promo

from .core import admin_panel


# === ПРОМО-АКЦИИ ===

async def manage_promo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню управления промо-акцией."""
    query = update.callback_query
    await query.answer()

    promo = await get_promo_info()
    is_active = promo.get('enabled', False)

    # Проверяем автоистечение
    if is_active:
        if not await is_promo_active():
            promo = await get_promo_info()
            is_active = False

    if is_active:
        status = "🟢 <b>АКТИВНА</b>"
        desc = promo.get('description') or '—'
        end_date_str = promo.get('end_date')
        if end_date_str:
            try:
                from datetime import datetime as dt_cls
                end_dt = dt_cls.fromisoformat(end_date_str)
                end_info = f"⏰ До: <b>{end_dt.strftime('%d.%m.%Y %H:%M')}</b> (UTC)"
            except (ValueError, TypeError):
                end_info = f"⏰ До: {end_date_str}"
        else:
            end_info = "⏰ <i>Бессрочно</i>"
        admin_id = promo.get('enabled_by', '—')

        text = (
            f"🎉 <b>Промо-акция</b>\n\n"
            f"Статус: {status}\n"
            f"📝 Описание: {desc}\n"
            f"{end_info}\n"
            f"👤 Включил: <code>{admin_id}</code>\n\n"
            f"<i>Пока акция активна, все пользователи получают безлимитные запросы.</i>"
        )
        keyboard = [
            [InlineKeyboardButton("🔴 Выключить акцию", callback_data="promo_disable")],
            [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
        ]
    else:
        text = (
            "🎉 <b>Промо-акция</b>\n\n"
            "Статус: ⚪ <b>НЕАКТИВНА</b>\n\n"
            "Включите акцию, чтобы снять лимиты для всех пользователей.\n"
            "Можно задать дату окончания — акция выключится автоматически."
        )
        keyboard = [
            [InlineKeyboardButton("🟢 Включить акцию", callback_data="promo_enable")],
            [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
        ]

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return MANAGE_PROMO


async def promo_enable_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает дату окончания промо."""
    query = update.callback_query
    await query.answer()

    text = (
        "🎉 <b>Включение промо-акции</b>\n\n"
        "Отправьте дату и время окончания в формате:\n"
        "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
        "Например: <code>23.02.2026 23:59</code>\n\n"
        "Или нажмите кнопку ниже для бессрочной акции."
    )
    keyboard = [
        [InlineKeyboardButton("♾ Без даты окончания", callback_data="promo_no_end_date")],
        [InlineKeyboardButton("◀️ Назад", callback_data="manage_promo")],
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return GET_PROMO_END_DATE


async def promo_receive_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает дату окончания и включает промо."""
    text_input = update.message.text.strip()
    admin_id = update.effective_user.id

    try:
        end_date = datetime.strptime(text_input, "%d.%m.%Y %H:%M")
        end_date = end_date.replace(tzinfo=None)  # Сохраняем как UTC
        from datetime import timezone as tz
        end_date = end_date.replace(tzinfo=tz.utc)
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты.\n\n"
            "Используйте: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
            "Например: <code>23.02.2026 23:59</code>",
            parse_mode="HTML"
        )
        return GET_PROMO_END_DATE

    await set_promo(
        enabled=True,
        end_date=end_date,
        admin_id=admin_id,
        description="Свободные выходные! 🎉"
    )

    await update.message.reply_text(
        f"✅ <b>Промо-акция включена!</b>\n\n"
        f"📝 Описание: Свободные выходные! 🎉\n"
        f"⏰ Окончание: <b>{end_date.strftime('%d.%m.%Y %H:%M')}</b> (UTC)\n\n"
        f"Все пользователи теперь имеют безлимитные запросы.",
        parse_mode="HTML"
    )

    await log_admin_action_to_sheet(admin_id, "PROMO_ENABLE", f"end_date={end_date.isoformat()}")
    return await admin_panel(update, context)


async def promo_enable_no_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включает бессрочную промо-акцию."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    await set_promo(
        enabled=True,
        end_date=None,
        admin_id=admin_id,
        description="Свободные выходные! 🎉"
    )

    await query.edit_message_text(
        "✅ <b>Промо-акция включена!</b>\n\n"
        "📝 Описание: Свободные выходные! 🎉\n"
        "⏰ Окончание: <i>Бессрочно</i>\n\n"
        "Все пользователи теперь имеют безлимитные запросы.\n"
        "Не забудьте выключить вручную!",
        parse_mode="HTML"
    )

    await log_admin_action_to_sheet(admin_id, "PROMO_ENABLE", "end_date=бессрочно")
    await asyncio.sleep(2)
    return await admin_panel(update, context)


async def promo_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выключает промо-акцию."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    await set_promo(enabled=False, admin_id=admin_id)

    await query.edit_message_text(
        "🔴 <b>Промо-акция выключена.</b>\n\n"
        "Лимиты пользователей восстановлены в обычном режиме.",
        parse_mode="HTML"
    )

    await log_admin_action_to_sheet(admin_id, "PROMO_DISABLE", "")
    await asyncio.sleep(2)
    return await admin_panel(update, context)
