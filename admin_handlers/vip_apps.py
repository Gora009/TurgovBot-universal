"""Заявки на ВИП за промокод: приём, канал, одобрение, доработка."""

import logging
import html
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, filters

import config
from database.manager import db_manager
from config import CHOOSING_ACTION, ADMIN_VIP_APPS
from services.vip_applications import (
    is_intake_enabled as vip_intake_enabled,
    set_intake_enabled as vip_set_intake_enabled,
    count_pending as vip_count_pending,
    count_applications as vip_count_applications,
    list_applications as vip_list_applications,
    get_application as vip_get_application,
    set_application_status as vip_set_status,
    get_channel_chat_id as vip_get_channel,
    set_channel_chat_id as vip_set_channel,
)

from .loophole import _loophole_admin_guard


# --- Заявки на ВИП за промокод (тумблер приёма + выгрузка открытых) ---
# Основной разбор — в канале заявок (config.VIP_APPLICATIONS_CHAT_ID), где у каждой
# заявки кнопки Одобрить/Отклонить (top-level хендлеры vip_ok_/vip_no_, работают и из
# канала). Этот раздел даёт тумблер приёма, ссылку и выгрузку открытых заявок в личку
# (фолбэк, если канал не настроен / карточку пропустили).

_VIP_DUMP_MAX = 10


async def manage_vip_applications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню заявок на ВИП: счётчики, deep-link, тумблер приёма, выгрузка открытых."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return CHOOSING_ACTION

    pending = await vip_count_pending()
    total = await vip_count_applications(only_pending=False)
    intake = await vip_intake_enabled()
    intake_s = "🟢 ВКЛ" if intake else "🔴 ВЫКЛ"
    chan = await vip_get_channel() or config.VIP_APPLICATIONS_CHAT_ID
    if chan:
        chan_s = f"<code>{chan}</code> ✅"
    else:
        chan_s = "❗ не задан — заявки идут в личку супер-админа"
    try:
        me = await context.bot.get_me()
        deep_link = f"https://t.me/{me.username}?start=apply_vip"
    except Exception:
        deep_link = "https://t.me/your_bot?start=apply_vip"

    text = (
        "💎 <b>Заявки на ВИП за промокод</b>\n\n"
        f"На рассмотрении: <b>{pending}</b> • всего: {total}\n"
        f"Приём заявок: {intake_s}\n"
        f"Чат заявок: {chan_s}\n\n"
        "📍 <b>Куда летят заявки:</b> создай группу, добавь туда бота и сделай его "
        "администратором, затем напиши В ТОЙ ГРУППЕ команду <code>/set_vip_channel</code> — "
        "бот запомнит её и будет слать заявки туда (с кнопками Одобрить/Отклонить).\n\n"
        "Ссылка для рассылки (по ней юзеры подают заявку):\n"
        f"<code>{html.escape(deep_link)}</code>"
    )
    keyboard = [
        [InlineKeyboardButton(f"📤 Открытые в личку ({pending})", callback_data="vapps_dump")],
        [InlineKeyboardButton(f"🔁 Приём заявок: {intake_s}", callback_data="vip_intake_toggle")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_VIP_APPS


async def vip_apps_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выгрузить открытые заявки (пруфы + карточка с кнопками) в чат админа."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_VIP_APPS
    apps = await vip_list_applications(only_pending=True, offset=0, limit=_VIP_DUMP_MAX)
    if not apps:
        await query.answer("Открытых заявок нет.", show_alert=True)
        return ADMIN_VIP_APPS
    from user_handlers.vip_application_flow import post_application_card
    chat_id = update.effective_chat.id
    sent = 0
    for app in apps:
        try:
            await post_application_card(context, app, chat_id)
            sent += 1
        except Exception:
            logging.exception("[vip_apps] не удалось выгрузить заявку #%s в личку", app.get("id"))
    total_pending = await vip_count_pending()
    tail = f"\n\n(показано {sent} из {total_pending} открытых; листай вверх)" if total_pending > sent else ""
    await context.bot.send_message(
        chat_id, f"📤 Открытые заявки на ВИП выгружены.{tail}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К меню заявок", callback_data="manage_vip_apps")]]),
    )
    return ADMIN_VIP_APPS


async def toggle_vip_intake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включить/выключить приём заявок на ВИП. callback: vip_intake_toggle."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_VIP_APPS
    cur = await vip_intake_enabled()
    await vip_set_intake_enabled(not cur)
    return await manage_vip_applications(update, context)


async def set_vip_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_vip_channel — выполнять В ГРУППЕ/КАНАЛЕ, куда добавлен бот. Запоминает этот чат как
    приёмник заявок на ВИП (туда летят пруфы + карточка с кнопками Одобрить/Отклонить)."""
    chat = update.effective_chat
    msg = update.effective_message
    if not await _loophole_admin_guard(update):
        if msg:
            await msg.reply_text("🚫 Назначить чат заявок может только администратор бота.")
        return
    if chat is None or chat.type not in ("group", "supergroup", "channel"):
        if msg:
            await msg.reply_text(
                "Эту команду нужно отправить В ГРУППЕ или КАНАЛЕ, куда добавлен бот.\n"
                "Создай группу для заявок на ВИП, добавь туда бота (сделай админом), затем "
                "напиши там <code>/set_vip_channel</code>.",
                parse_mode="HTML",
            )
        return
    await vip_set_channel(chat.id)
    title = chat.title or str(chat.id)
    logging.info("[vip_apps] чат заявок установлен: %s (%s) админом %s", chat.id, title, update.effective_user.id if update.effective_user else "?")
    if msg:
        await msg.reply_text(
            f"✅ Готово! Заявки на ВИП теперь будут падать сюда: <b>{html.escape(title)}</b> "
            f"(id <code>{chat.id}</code>).\nКаждая заявка придёт с пруфами и кнопками "
            "«Одобрить»/«Отклонить».",
            parse_mode="HTML",
        )


def _admin_label(user) -> str:
    if user is None:
        return "админом"
    if user.username:
        return f"@{html.escape(user.username)}"
    return html.escape(user.full_name or str(user.id))


async def _vip_mark_card(query, suffix_html: str) -> None:
    """Дописать в карточку статус и убрать кнопки (работает в канале и в личке)."""
    base = query.message.text_html if getattr(query.message, "text_html", None) else (query.message.text or "💎 Заявка на ВИП")
    new_text = base + suffix_html
    try:
        await query.edit_message_text(new_text, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def accept_vip_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Одобрить заявку на ВИП: выдать users.is_unlimited (безлимит + оверлей) + уведомить.
    TOP-LEVEL хендлер — жмётся из канала заявок. callback: vip_ok_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Одобрять может только администратор.", show_alert=True)
        return
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return

    row = await vip_set_status(app_id, "approved", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        await _vip_mark_card(query, "")
        return
    user_id = row["user_id"]
    # Выдача безлимита — авторитетный шаг. При сбое откатываем заявку в pending.
    granted = False
    try:
        res = await db_manager.execute("UPDATE users SET is_unlimited = true WHERE user_id = $1", user_id)
        granted = isinstance(res, str) and res != "UPDATE 0"
    except Exception:
        logging.exception("[vip_apps] не удалось выдать is_unlimited user_id=%s", user_id)
    if not granted:
        try:
            await db_manager.execute(
                "UPDATE vip_applications SET status='pending', reviewed_by=NULL, reviewed_at=NULL WHERE id=$1",
                app_id,
            )
        except Exception:
            logging.exception("[vip_apps] не удалось откатить заявку #%s в pending", app_id)
        await query.answer("Не удалось выдать статус — заявка возвращена в очередь, повтори.", show_alert=True)
        return

    await _vip_mark_card(query, f"\n\n✅ <b>Одобрено</b> {_admin_label(update.effective_user)}")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("💎 <b>Заявка на ВИП одобрена!</b>\n\n"
                  "Тебе выдан <b>безлимит</b> на все функции бота. Спасибо, что с нами! 🎉"),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[vip_apps] не удалось уведомить user_id=%s об одобрении", user_id)


async def reject_vip_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отклонить заявку на ВИП + уведомить (повторная подача разрешена).
    TOP-LEVEL хендлер. callback: vip_no_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Отклонять может только администратор.", show_alert=True)
        return
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return
    row = await vip_set_status(app_id, "rejected", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        await _vip_mark_card(query, "")
        return
    user_id = row["user_id"]
    await _vip_mark_card(query, f"\n\n❌ <b>Отклонено</b> {_admin_label(update.effective_user)}")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("❌ <b>Твоя заявка на ВИП отклонена.</b>\n\n"
                  "Если считаешь это ошибкой или появились новые пруфы — можешь подать заявку снова."),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[vip_apps] не удалось уведомить user_id=%s об отклонении", user_id)


# --- ВИП-заявка: отправить на доработку (запросить доп. инфо) ---
# Админ жмёт «📝 На доработку» в канале → бот просит ответить на сообщение текстом (что
# доработать) → текст уходит заявителю, заявка → 'needs_info', юзер может подать заново.
_VIP_REV_SENTINEL = "✏️ Доработка заявки на ВИП №"


class _VipRevReplyFilter(filters.MessageFilter):
    """Матчит ТОЛЬКО ответ на сообщение-запрос доработки (по сентинелу) — чтобы глобальный
    хендлер не ловил посторонние реплаи."""
    def filter(self, message) -> bool:
        r = getattr(message, "reply_to_message", None)
        return bool(r and (r.text or "").startswith(_VIP_REV_SENTINEL))


vip_rev_reply_filter = _VipRevReplyFilter()


def _parse_vip_rev_id(text):
    try:
        tail = text.split("№", 1)[1]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        return int(digits) if digits else None
    except Exception:
        return None


async def request_vip_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запросить доработку заявки. TOP-LEVEL (из канала). callback: vip_info_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Запросить доработку может только администратор.", show_alert=True)
        return
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return
    app = await vip_get_application(app_id)
    if not app or app.get("status") != "pending":
        await query.answer("Заявка уже обработана.", show_alert=True)
        return
    # Просим админа ответить на это сообщение текстом — что доработать.
    await query.message.reply_text(
        f"{_VIP_REV_SENTINEL}{app_id}\n\n"
        "Ответь на ЭТО сообщение текстом — что нужно доработать (каких пруфов не хватает и т.п.). "
        "Текст уйдёт заявителю, а заявка вернётся ему на доработку.",
        parse_mode="HTML",
    )


async def handle_vip_revision_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ ответил на запрос доработки текстом → шлём заявителю, заявка → needs_info."""
    msg = update.effective_message
    if not await _loophole_admin_guard(update):
        return
    app_id = _parse_vip_rev_id(msg.reply_to_message.text or "")
    if not app_id:
        return
    comment = (msg.text or "").strip()
    if not comment:
        await msg.reply_text("Напиши текстом, что доработать.")
        return
    row = await vip_set_status(app_id, "needs_info", update.effective_user.id)
    if not row:
        await msg.reply_text("Эта заявка уже обработана (одобрена/отклонена/отправлена).")
        return
    user_id = row["user_id"]
    # Уведомляем заявителя.
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("📝 <b>Твою заявку на ВИП вернули на доработку.</b>\n\n"
                  f"<b>Что нужно:</b> {html.escape(comment)}\n\n"
                  "Подай заявку заново с учётом замечаний — кнопка «💎 Получить ВИП за промокод» "
                  "в главном меню."),
            parse_mode="HTML",
        )
        notified = True
    except Exception:
        notified = False
        logging.warning("[vip_apps] не удалось уведомить user_id=%s о доработке", user_id)
    # Снимаем кнопки с исходной карточки (чтобы её не обработали повторно).
    app = await vip_get_application(app_id)
    if app and app.get("channel_chat_id") and app.get("channel_message_id"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=app["channel_chat_id"], message_id=app["channel_message_id"], reply_markup=None
            )
        except Exception:
            pass
    await msg.reply_text(
        f"📝 Заявка #{app_id} отправлена заявителю на доработку"
        + ("." if notified else " (но не удалось доставить ему сообщение — возможно, он не запускал бота).")
    )
