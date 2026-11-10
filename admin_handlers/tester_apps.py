"""Заявки в тестировщики и приглашение тестеров в группу."""

import asyncio
import logging
import json
import html
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import Forbidden, RetryAfter

from database.manager import db_manager
from config import CHOOSING_ACTION, ADMIN_TESTER_APPS
from services.tester_applications import (
    is_intake_enabled as ta_intake_enabled,
    set_intake_enabled as ta_set_intake_enabled,
    count_pending as ta_count_pending,
    count_applications as ta_count_applications,
    list_applications as ta_list_applications,
    get_application as ta_get_application,
    set_application_status as ta_set_status,
    gather_user_metrics as ta_gather_metrics,
    format_metrics_block as ta_format_metrics,
    clip_html as ta_clip_html,
)

from .loophole import _loophole_admin_guard, _loophole_user_label


# --- Заявки в тестировщики (просмотр/отбор; доступно всем админам) ---

_TAPPS_PAGE = 10

_TAPP_Q_LABELS = {
    "q1": "О себе / опыт в RP",
    "q2": "Мотивация",
    "q3": "ПК / Windows",
}
_TAPP_STATUS_H = {
    "pending": "⏳ на рассмотрении",
    "approved": "✅ принята",
    "rejected": "❌ отклонена",
}
_TAPP_STATUS_MARK = {"pending": "⏳", "approved": "✅", "rejected": "❌"}


# ---------------------------------------------------------------------------
# Беседа тестеров: бот зовёт всех тестировщиков в группу админа.
# ВАЖНО: Bot API НЕ умеет добавлять участников в чат напрямую (это умеет только
# userbot/MTProto). Поэтому схема: админ создаёт группу → добавляет бота и делает его
# админом с правом приглашать → пишет в группе /invite_testers → бот делает ссылку-
# приглашение и рассылает её всем тестерам в личку (один тап — человек в беседе).
# ---------------------------------------------------------------------------

_TESTERS_GROUP_KEY = "testers_group_chat_id"


async def invite_testers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invite_testers — выполнять В ГРУППЕ тестеров. Бот создаёт invite-ссылку и зовёт
    всех тестеров в личку. Повторный запуск зовёт только тех, кого ещё нет в беседе."""
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or chat.type not in ("group", "supergroup"):
        await msg.reply_text(
            "Эту команду нужно отправить В ГРУППЕ, куда добавлен бот.\n"
            "Создай беседу тестеров, добавь бота, сделай его администратором (с правом "
            "приглашать по ссылке) и напиши там /invite_testers."
        )
        return
    if not await _loophole_admin_guard(update):
        await msg.reply_text("🚫 Звать тестеров может только администратор бота.")
        return

    # Бот должен быть админом группы с правом приглашать → проверяем созданием ссылки.
    try:
        link = await context.bot.create_chat_invite_link(chat.id, name="Тестировщики")
        invite_url = link.invite_link
    except Exception as e:
        logging.warning("[INVITE_TESTERS] create_chat_invite_link failed: %s", e)
        await msg.reply_text(
            "Не получилось создать ссылку-приглашение.\n"
            "Сделай бота <b>администратором</b> этой группы с правом "
            "«Пригласить пользователей по ссылке» и повтори <code>/invite_testers</code>.",
            parse_mode="HTML",
        )
        return

    # Запоминаем группу (для будущих повторных приглашений).
    try:
        await db_manager.execute(
            "INSERT INTO system_cache (key, value, updated_at) VALUES ($1, $2::jsonb, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            _TESTERS_GROUP_KEY, json.dumps(chat.id),
        )
    except Exception:
        logging.warning("[INVITE_TESTERS] не удалось сохранить chat_id группы", exc_info=True)

    rows = await db_manager.fetch(
        "SELECT user_id FROM users WHERE is_tester = TRUE AND is_banned = FALSE "
        "AND COALESCE(is_blocked, FALSE) = FALSE"
    )
    tester_ids = [r["user_id"] for r in rows]
    if not tester_ids:
        await msg.reply_text("В базе нет активных тестировщиков для приглашения.")
        return

    status = await msg.reply_text(f"📨 Зову тестеров в беседу… (всего {len(tester_ids)})")
    # Тяжёлую рассылку — в фон (concurrent_updates выключен, иначе блокирует остальных).
    asyncio.create_task(
        _invite_testers_job(context.bot, chat.id, invite_url, tester_ids, status)
    )


async def _invite_testers_job(bot, group_chat_id, invite_url, tester_ids, status_msg) -> None:
    """Фон: разослать ссылку-приглашение всем тестерам в личку (кого ещё нет в беседе)."""
    invited = skipped = blocked = failed = 0
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Вступить в беседу", url=invite_url)]])
    text = (
        "👋 Тебя зовут в <b>беседу тестировщиков</b>.\n"
        "Жми кнопку ниже, чтобы вступить:"
    )
    for i, uid in enumerate(tester_ids):
        # Уже в беседе? — не спамим (актуально для повторных запусков).
        try:
            member = await bot.get_chat_member(group_chat_id, uid)
            if member.status in ("creator", "administrator", "member", "restricted"):
                skipped += 1
                await asyncio.sleep(0.03)
                continue
        except Exception:
            pass  # не участник / не найден → приглашаем

        try:
            await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
            invited += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
                invited += 1
            except Forbidden:
                blocked += 1
                await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", uid)
            except Exception:
                failed += 1
        except Forbidden:
            blocked += 1
            await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", uid)
        except Exception as e:
            failed += 1
            logging.warning("[INVITE_TESTERS] send to %s failed: %s", uid, e)

        # Rate limiting ~25 msg/sec (как в рассылке).
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1.1)
        else:
            await asyncio.sleep(0.05)

    report = (
        f"✅ Готово.\n"
        f"Приглашено: <b>{invited}</b>\n"
        f"Уже в беседе: {skipped}\n"
        f"Не дошло (заблокировали/не писали боту): {blocked}\n"
    )
    if failed:
        report += f"Ошибок: {failed}\n"
    report += (
        "\nКого «не дошло» — попроси написать боту <code>/start</code> и вступить по ссылке, "
        "либо добавь вручную из своего аккаунта."
    )
    try:
        await status_msg.edit_text(report, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(group_chat_id, report, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Отчёт не доставлен в группу {group_chat_id}: {e}")


async def manage_tester_applications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню заявок в тестеры: счётчики, deep-link для канала, тумблер приёма."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return CHOOSING_ACTION

    pending = await ta_count_pending()
    total = await ta_count_applications(only_pending=False)
    intake = await ta_intake_enabled()
    intake_s = "🟢 ВКЛ" if intake else "🔴 ВЫКЛ"
    try:
        me = await context.bot.get_me()
        deep_link = f"https://t.me/{me.username}?start=apply_tester"
    except Exception:
        deep_link = "https://t.me/your_bot?start=apply_tester"

    text = (
        "📝 <b>Заявки в тестировщики</b>\n\n"
        f"На рассмотрении: <b>{pending}</b> • всего: {total}\n"
        f"Приём заявок: {intake_s}\n\n"
        "Ссылка для канала (по ней юзеры подают заявку):\n"
        f"<code>{html.escape(deep_link)}</code>\n\n"
        "👥 <b>Беседа тестеров:</b> создай группу, добавь бота и сделай его админом "
        "(с правом приглашать), затем напиши в группе <code>/invite_testers</code> — "
        "бот разошлёт всем тестерам приглашение."
    )
    keyboard = [
        [InlineKeyboardButton(f"📋 Открытые ({pending})", callback_data="tapps_0_0")],
        [InlineKeyboardButton("📜 Все заявки", callback_data="tapps_0_1")],
        [InlineKeyboardButton(f"🔁 Приём заявок: {intake_s}", callback_data="tester_intake_toggle")],
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
    return ADMIN_TESTER_APPS


async def show_tester_apps_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Листинг заявок с пагинацией. callback: tapps_<offset>_<0=открытые|1=все>."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS

    offset, only_pending = 0, True
    data = query.data if query else "tapps_0_0"
    if data.startswith("tapps_"):
        try:
            _, off_s, f_s = data.split("_")
            offset = max(0, int(off_s))
            only_pending = (f_s == "0")
        except (ValueError, IndexError):
            offset, only_pending = 0, True

    total = await ta_count_applications(only_pending=only_pending)
    rows = await ta_list_applications(only_pending=only_pending, offset=offset, limit=_TAPPS_PAGE)

    title = "📋 <b>Открытые заявки</b>" if only_pending else "📜 <b>Все заявки</b>"
    lines = [title, f"Всего: <b>{total}</b>" + (f" • показаны {offset + 1}–{offset + len(rows)}" if rows else ""), ""]
    if not rows:
        lines.append("Заявок нет.")

    keyboard = []
    for r in rows:
        ts = r["created_at"].strftime("%d.%m %H:%M") if r.get("created_at") else "?"
        label = await _loophole_user_label(r["user_id"])
        mark = _TAPP_STATUS_MARK.get(r.get("status"), "•")
        keyboard.append([InlineKeyboardButton(
            f"{mark} {ts} • {label}",
            callback_data=f"tapp_{r['id']}",
        )])

    f = "0" if only_pending else "1"
    # Запоминаем текущий список (фильтр+страницу), чтобы кнопка «К списку» в карточке
    # возвращала именно сюда, а не всегда в «Открытые, стр.0».
    context.user_data["tapps_last"] = f"tapps_{offset}_{f}"
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tapps_{max(0, offset - _TAPPS_PAGE)}_{f}"))
    if offset + _TAPPS_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tapps_{offset + _TAPPS_PAGE}_{f}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "📜 Показать все" if only_pending else "📋 Только открытые",
        callback_data=f"tapps_0_{1 if only_pending else 0}",
    )])
    keyboard.append([InlineKeyboardButton("◀️ К меню заявок", callback_data="manage_tester_apps")])

    markup = InlineKeyboardMarkup(keyboard)
    text = "\n".join(lines)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_TESTER_APPS


async def _render_app_detail(query, context: ContextTypes.DEFAULT_TYPE, app_id: int) -> int:
    """Карточка одной заявки: ответы анкеты + метрики активности юзера."""
    app = await ta_get_application(app_id)
    if not app:
        await query.answer("Заявка не найдена.", show_alert=True)
        return ADMIN_TESTER_APPS

    user_id = app["user_id"]
    label = html.escape(await _loophole_user_label(user_id))
    answers = app.get("answers") or {}
    st = app.get("status")
    st_h = _TAPP_STATUS_H.get(st, st or "?")
    ts = app["created_at"].strftime("%d.%m.%Y %H:%M") if app.get("created_at") else "?"

    answers_block = "\n\n".join(
        f"<b>{_TAPP_Q_LABELS.get(k, k)}:</b>\n{html.escape(str(answers.get(k) or '—'))}"
        for k in ("q1", "q2", "q3")
    )
    metrics = await ta_gather_metrics(user_id)
    metrics_block = ta_format_metrics(metrics)

    text = (
        f"📝 <b>Заявка #{app_id}</b> — {st_h}\n"
        f"👤 {label} (<code>{user_id}</code>)\n"
        f"🕒 подана: {ts}\n\n"
        f"{answers_block}\n\n"
        f"{metrics_block}"
    )
    # Ответы юзера произвольной длины + html.escape могут раздуть карточку за лимит
    # Telegram (4096). Обрезаем по безопасной границе, иначе карточку нельзя было бы
    # ни открыть, ни принять/отклонить — заявка зависала бы в pending.
    text = ta_clip_html(text)

    keyboard = []
    if st == "pending":
        keyboard.append([
            InlineKeyboardButton("✅ Принять", callback_data=f"tapp_ok_{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"tapp_no_{app_id}"),
        ])
    back_cb = context.user_data.get("tapps_last", "tapps_0_0")
    keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data=back_cb)])
    markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            # Последний рубеж: без разметки, чтобы кнопки Принять/Отклонить всё равно дошли.
            await query.message.reply_text(text, reply_markup=markup)
    return ADMIN_TESTER_APPS


async def show_tester_app_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Открыть карточку заявки. callback: tapp_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    try:
        app_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.answer("Некорректная заявка.", show_alert=True)
        return ADMIN_TESTER_APPS
    return await _render_app_detail(query, context, app_id)


async def accept_tester_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принять заявку: выдать роль тестера + уведомить. callback: tapp_ok_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return ADMIN_TESTER_APPS

    row = await ta_set_status(app_id, "approved", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        return await _render_app_detail(query, context, app_id)

    user_id = row["user_id"]
    # Выдача роли — авторитетный шаг. Если она НЕ применилась (транзиентный сбой БД),
    # откатываем заявку обратно в pending и НЕ уведомляем юзера об одобрении — иначе
    # будет рассинхрон: статус approved при is_tester=false (юзер без доступа, заявку
    # уже не видно в «Открытые»).
    granted = False
    try:
        res = await db_manager.execute("UPDATE users SET is_tester = true WHERE user_id = $1", user_id)
        granted = isinstance(res, str) and res != "UPDATE 0"
    except Exception:
        logging.exception("[tester_apps] не удалось выдать роль тестера user_id=%s", user_id)

    if not granted:
        try:
            await db_manager.execute(
                "UPDATE tester_applications SET status='pending', reviewed_by=NULL, reviewed_at=NULL "
                "WHERE id=$1",
                app_id,
            )
        except Exception:
            logging.exception("[tester_apps] не удалось откатить заявку #%s в pending", app_id)
        await query.answer("Не удалось выдать роль — заявка возвращена в очередь, повтори.", show_alert=True)
        return await _render_app_detail(query, context, app_id)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("✅ <b>Твоя заявка в тестировщики одобрена!</b>\n\n"
                  "Открой /test, чтобы скачать оверлей и посмотреть инструкцию по входу. "
                  "Добро пожаловать в команду! 🎉"),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[tester_apps] не удалось уведомить user_id=%s об одобрении", user_id)

    return await _render_app_detail(query, context, app_id)


async def reject_tester_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отклонить заявку + уведомить (повторная подача разрешена). callback: tapp_no_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return ADMIN_TESTER_APPS

    row = await ta_set_status(app_id, "rejected", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        return await _render_app_detail(query, context, app_id)

    user_id = row["user_id"]
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("❌ <b>Твоя заявка в тестировщики отклонена.</b>\n\n"
                  "Спасибо за интерес! Ты можешь подать заявку снова позже."),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[tester_apps] не удалось уведомить user_id=%s об отклонении", user_id)

    return await _render_app_detail(query, context, app_id)


async def toggle_tester_intake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включить/выключить приём заявок. callback: tester_intake_toggle."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    cur = await ta_intake_enabled()
    await ta_set_intake_enabled(not cur)
    # manage_tester_applications сам ответит на callback и перерисует меню.
    return await manage_tester_applications(update, context)
