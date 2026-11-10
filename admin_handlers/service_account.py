"""Горячая смена сервис-аккаунта Vertex/Gemini (только суперадмин)."""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from config import SUPER_ADMIN_ID, CHOOSING_ACTION, ADMIN_SA_UPLOAD, ADMIN_SA_BUCKET


# --- Горячая смена сервис-аккаунта Vertex/Gemini (только суперадмин) ---

async def sa_change_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Суперадмин: запросить .json сервис-аккаунта."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для супер-админа.", show_alert=True)
        return CHOOSING_ACTION
    text = (
        "🔑 <b>Смена сервис-аккаунта Vertex/Gemini</b>\n\n"
        "Пришли сюда <b>.json-файл</b> сервис-аккаунта (можно из нового GCP-проекта — ради свежей "
        "квоты Gemini). Я проверю его, заменю активный ключ и СРАЗУ переинициализирую и бота, и оверлей (API).\n\n"
        "⚠️ Если проект новый — доступа к GCS-бакету старого проекта (botlaw2) у него не будет "
        "(это про квоту Gemini, не про хранилище законов). Старый ключ сохраню в .bak."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    return ADMIN_SA_UPLOAD


async def sa_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принять .json, заменить активный SA и переинициализировать бота + оверлей (API)."""
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("Только для супер-админа.")
        return ConversationHandler.END

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Пришли именно .json-файл сервис-аккаунта.")
        return ADMIN_SA_UPLOAD
    if doc.file_size and doc.file_size > 100_000:
        await update.message.reply_text("Слишком большой файл для сервис-аккаунта (>100 КБ). Это точно SA-ключ?")
        return ADMIN_SA_UPLOAD

    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Не удалось скачать файл: {e}")
        return ADMIN_SA_UPLOAD

    from services.sa_admin import swap_service_account, reload_local_vertex, trigger_api_reload
    ok, msg, info = swap_service_account(data)
    # Гигиена: убрать сообщение с приватным ключом из чата (best-effort).
    try:
        await update.message.delete()
    except Exception:
        pass
    if not ok:
        await update.effective_chat.send_message(f"❌ {msg}")
        return ADMIN_SA_UPLOAD

    ok_bot, bot_msg = reload_local_vertex()           # этот процесс (бот)
    ok_api, api_msg = await trigger_api_reload()       # процесс API (оверлей)

    proj = (info or {}).get("project_id")
    email = (info or {}).get("client_email")
    lines = [
        "✅ <b>Сервис-аккаунт заменён.</b>",
        f"Проект: <code>{proj}</code>",
        f"SA: <code>{email}</code>",
        f"Бот: {'🟢 переинициализирован (' + bot_msg + ')' if ok_bot else '🔴 ' + bot_msg}",
        f"Оверлей (API): {'🟢 ' + api_msg if ok_api else '🔴 ' + api_msg}",
    ]
    if not ok_api:
        lines.append("<i>API подхватит ключ при следующем рестарте; проверь логи API.</i>")
    lines.append("\nЕсли проект новый — перенеси законку в новый бакет (старый недоступен):")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Перенести законку в новый бакет", callback_data="sa_bucket")],
        [InlineKeyboardButton("◀️ В админку", callback_data="admin_panel")],
    ])
    await update.effective_chat.send_message("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    return CHOOSING_ACTION


# --- Перенос законки в новый бакет (после смены проекта) ---

async def sa_bucket_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для супер-админа.", show_alert=True)
        return CHOOSING_ACTION
    await query.edit_message_text(
        "📦 <b>Перенос законки в новый бакет</b>\n\n"
        "Пришли <b>имя нового бакета</b> (он уже должен быть создан в новом проекте, и текущий "
        "сервис-аккаунт должен иметь к нему доступ на запись).\n"
        "Бот перезальёт туда все законы и прецеденты <b>из БД</b> и обновит ссылки.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        parse_mode="HTML",
    )
    return ADMIN_SA_BUCKET


async def sa_bucket_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("Только для супер-админа.")
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    name = raw.replace("gs://", "").strip("/").split("/")[0].strip()
    if not name:
        await update.message.reply_text("Пришли имя бакета одной строкой (напр. botlaw3).")
        return ADMIN_SA_BUCKET
    from services.sa_admin import set_active_bucket, trigger_api_reload
    await set_active_bucket(name)
    ok_api, api_msg = await trigger_api_reload()   # пробросить новый бакет в API
    text = (
        f"Активный бакет: <code>{name}</code>\n"
        f"Оверлей (API): {'🟢 ' + api_msg if ok_api else '🔴 ' + api_msg}\n\n"
        "Запустить перенос законки из БД в этот бакет?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Только включённые сервера", callback_data="sa_mig_enabled")],
        [InlineKeyboardButton("▶️ Все (вкл. отключённые)", callback_data="sa_mig_all")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")],
    ])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ADMIN_SA_BUCKET


async def _run_sa_migration(application, chat_id: int, enabled_only: bool) -> None:
    from services.sa_admin import migrate_corpus_to_active_bucket
    import config as _cfg
    bot = application.bot
    try:
        status = await bot.send_message(
            chat_id=chat_id,
            text=f"📦 Переношу законку в <code>{_cfg.active_gcs_bucket()}</code>… старт",
            parse_mode="HTML",
        )
    except Exception:
        status = None

    async def progress(stats, i, total):
        if not status:
            return
        pct = int(i * 100 / max(total, 1))
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=status.message_id,
                text=f"📦 Перенос: {i}/{total} ({pct}%) • залито законов: {stats['laws_done']}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    try:
        stats = await migrate_corpus_to_active_bucket(enabled_only=enabled_only, progress_cb=progress)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Перенос завершён</b> → <code>{stats['bucket']}</code>\n"
                f"Законов залито: <b>{stats['laws_done']}</b> (пропущено отключённых: {stats['laws_skipped']})\n"
                f"Прецедентов: <b>{stats['precedents_done']}</b>\n"
                f"Ошибок: <b>{stats['errors']}</b>\n\n"
                "Ссылки в БД обновлены — Q&A/анализ работают по новому бакету."
            ),
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("[ADMIN] миграция законки в бакет упала")
        await bot.send_message(chat_id=chat_id, text=f"❌ Перенос упал: {e}")


async def sa_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для супер-админа.", show_alert=True)
        return ADMIN_SA_BUCKET
    enabled_only = (query.data == "sa_mig_enabled")
    chat_id = query.message.chat_id
    context.application.create_task(_run_sa_migration(context.application, chat_id, enabled_only))
    await query.edit_message_text(
        "📦 Запустил перенос в фоне — пришлю прогресс и итог сюда.\n"
        "<i>Пока идёт перенос, часть ответов может временно не находить законы.</i>",
        parse_mode="HTML",
    )
    return CHOOSING_ACTION
