"""Анализатор дыр в законах: очередь, кэш, результаты, задачи."""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

import config
from database.manager import db_manager
from config import (
    SUPER_ADMIN_ID,
    ADMIN_LOOPHOLE_QUEUE,
    ADMIN_LOOPHOLE_JOB_DETAIL,
    ADMIN_LOOPHOLE_SET_POSITION,
    ADMIN_LOOPHOLE_RESULTS,
)
from services.law_loophole.queue import (
    list_queued_jobs as lq_list_queued,
    list_running_jobs as lq_list_running,
    get_job as lq_get_job,
    get_queue_position as lq_position,
    cancel_job as lq_cancel,
    move_job_to_top as lq_move_top,
    set_job_position as lq_set_position,
    get_current_position_in_list as lq_current_pos,
    admin_run_out_of_band as lq_run_oob,
    is_queue_paused as lq_is_paused,
    set_queue_paused as lq_set_paused,
    is_loophole_enabled as lq_feature_enabled,
    set_loophole_enabled as lq_set_feature_enabled,
)
from services.law_loophole.analysis_cache import (
    is_caching_enabled as lq_cache_enabled,
    set_caching_enabled as lq_set_cache_enabled,
    cache_count as lq_cache_count,
    clear_cache as lq_clear_cache,
    clear_zero_cache as lq_clear_zero_cache,
    list_analysis_results as lq_list_results,
    count_analysis_results as lq_count_results,
    get_analysis_result as lq_get_result,
)
from services.law_loophole.report import (
    format_loophole_summary as lq_format_summary,
    build_loophole_report_html_doc as lq_build_html_doc,
)


# ===========================================================================
# Очередь анализатора дыр в законке
# ===========================================================================

_LOOPHOLE_QUEUE_DISPLAY_CAP = 20


def _loophole_priority_label(priority: int) -> str:
    if priority >= config.LOOPHOLE_PRIORITY_ADMIN:
        return "👑"
    if priority >= config.LOOPHOLE_PRIORITY_VIP:
        return "💎"
    return "👤"


async def _loophole_user_label(user_id: int) -> str:
    rec = await db_manager.fetchrow(
        "SELECT full_name, username FROM users WHERE user_id = $1", user_id
    )
    if rec and rec.get('username'):
        return f"@{rec['username']}"
    if rec and rec.get('full_name'):
        return str(rec['full_name'])[:30]
    return f"id{user_id}"


def _wake_loophole(context: ContextTypes.DEFAULT_TYPE) -> None:
    wake = context.application.bot_data.get("loophole_wake_event")
    if wake is not None:
        wake.set()


async def _loophole_admin_guard(update: Update) -> bool:
    user_id = update.effective_user.id
    rec = await db_manager.fetchrow("SELECT is_admin FROM users WHERE user_id = $1", user_id)
    return bool((rec and rec['is_admin']) or user_id == SUPER_ADMIN_ID)


def _loophole_job_id(query) -> int:
    return int(query.data.rsplit("_", 1)[1])


async def manage_loophole_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Список очереди анализатора: выполняемые + queued (с кнопками на каждую)."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return ConversationHandler.END

    queued = await lq_list_queued()
    running = await lq_list_running(out_of_band=False)
    oob_running = await lq_list_running(out_of_band=True)
    paused = await lq_is_paused()
    cache_on = await lq_cache_enabled()
    cache_n = await lq_cache_count()
    feature_on = await lq_feature_enabled()

    status_line = "⏸ <b>НА ПАУЗЕ</b> (новые не стартуют)" if paused else "▶️ Работает"
    cache_line = f"🗄 Кэш «весь свод»: {'🟢 ВКЛ' if cache_on else '⚪ ВЫКЛ'} (записей: {cache_n})"
    feature_line = f"🔌 Функция у юзеров: {'🟢 ВКЛ' if feature_on else '🔴 ВЫКЛ — кнопка скрыта'}"
    lines = ["🕳 <b>Очередь анализатора дыр</b>", feature_line, status_line, cache_line, ""]
    if paused and not running:
        lines.append("✅ Активных анализов нет — можно безопасно перезапускать бота.")
        lines.append("")
    if running:
        lines.append("⚙️ <b>Выполняется:</b>")
        for j in running:
            label = await _loophole_user_label(j['user_id'])
            lines.append(f"  {_loophole_priority_label(j['priority'])} {label}")
        lines.append("")
    if oob_running:
        lines.append(f"⚡ <b>Параллельно (без очереди):</b> {len(oob_running)}")
        lines.append("")

    keyboard = []
    if queued:
        lines.append(f"⏳ <b>В очереди:</b> {len(queued)}")
        for idx, j in enumerate(queued[:_LOOPHOLE_QUEUE_DISPLAY_CAP], start=1):
            label = await _loophole_user_label(j['user_id'])
            keyboard.append([InlineKeyboardButton(
                f"{idx}. {_loophole_priority_label(j['priority'])} {label}",
                callback_data=f"loophole_job_{j['id']}",
            )])
        if len(queued) > _LOOPHOLE_QUEUE_DISPLAY_CAP:
            lines.append(f"… и ещё {len(queued) - _LOOPHOLE_QUEUE_DISPLAY_CAP} (показаны первые {_LOOPHOLE_QUEUE_DISPLAY_CAP})")
    else:
        lines.append("⏳ Очередь пуста.")

    keyboard.append([InlineKeyboardButton(
        "🔌 Выключить функцию (скрыть у юзеров)" if feature_on else "🔌 Включить функцию (показать юзерам)",
        callback_data="loophole_feature_toggle",
    )])
    if paused:
        keyboard.append([InlineKeyboardButton("▶️ Возобновить очередь", callback_data="loophole_queue_resume")])
    else:
        keyboard.append([InlineKeyboardButton("⏸ Пауза (для рестарта)", callback_data="loophole_queue_pause")])
    keyboard.append([
        InlineKeyboardButton(
            "🗄 Кэш: ВЫКЛ" if cache_on else "🗄 Кэш: ВКЛ",
            callback_data="loophole_cache_toggle",
        ),
        InlineKeyboardButton("🧹 Очистить кэш", callback_data="loophole_cache_clear"),
    ])
    keyboard.append([
        InlineKeyboardButton("📊 Все анализы", callback_data="show_all_analyses"),
        InlineKeyboardButton("🧹 0-дыры из кэша", callback_data="loophole_clear_zero"),
    ])
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="manage_loophole_queue")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
    markup = InlineKeyboardMarkup(keyboard)
    text = "\n".join(lines)

    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_LOOPHOLE_QUEUE


async def loophole_queue_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    await lq_set_paused(True)
    await query.answer("⏸ Очередь на паузе. Текущие анализы доработают, новые не стартуют.")
    return await manage_loophole_queue(update, context)


async def loophole_queue_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    await lq_set_paused(False)
    _wake_loophole(context)
    await query.answer("▶️ Очередь возобновлена.")
    return await manage_loophole_queue(update, context)


async def loophole_cache_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    new_state = not await lq_cache_enabled()
    await lq_set_cache_enabled(new_state)
    await query.answer("🗄 Кэширование включено." if new_state else "🗄 Кэширование выключено.")
    return await manage_loophole_queue(update, context)


async def loophole_cache_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    n = await lq_clear_cache()
    await query.answer(f"🧹 Кэш очищен (удалено: {n}).")
    return await manage_loophole_queue(update, context)


async def loophole_clear_zero(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Удалить из КЭША записи с 0 дыр (обычно их нет — нули не кэшируются). Архив не трогает."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    n = await lq_clear_zero_cache()
    await query.answer(f"Удалено 0-дырочных из кэша: {n}." if n else "0-дырочных в кэше нет.")
    return await manage_loophole_queue(update, context)


async def loophole_feature_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ВКЛ/ВЫКЛ функцию «Анализатор законов»: при ВЫКЛ кнопка скрывается у пользователей."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    new_state = not await lq_feature_enabled()
    await lq_set_feature_enabled(new_state)
    await query.answer("🟢 Анализатор включён — кнопка видна юзерам." if new_state
                       else "🔴 Анализатор выключен — кнопка скрыта у юзеров.")
    return await manage_loophole_queue(update, context)


# --- Архив всех анализов (просмотр/разбор из админки) -----------------------

_RESULTS_PAGE = 10


async def show_all_analyses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Листинг архива всех анализов (с пагинацией и фильтром «только 0 дыр»).
    callback: show_all_analyses (старт) | lres_<offset>_<0|1> (навигация/фильтр)."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE

    offset, only_zero = 0, False
    data = query.data if query else "show_all_analyses"
    if data.startswith("lres_"):
        try:
            _, off_s, z_s = data.split("_")
            offset = max(0, int(off_s))
            only_zero = (z_s == "1")
        except (ValueError, IndexError):
            offset, only_zero = 0, False

    total = await lq_count_results(only_zero=only_zero)
    rows = await lq_list_results(limit=_RESULTS_PAGE, offset=offset, only_zero=only_zero)

    title = "📊 <b>Все анализы</b>" + (" — только 0 дыр" if only_zero else "")
    lines = [title, f"Всего: <b>{total}</b>" + (f" • показаны {offset + 1}–{offset + len(rows)}" if rows else ""), ""]
    if not rows:
        lines.append("Записей нет.")

    keyboard = []
    for r in rows:
        ts = r["created_at"].strftime("%d.%m %H:%M") if r.get("created_at") else "?"
        nf = int(r.get("total_findings") or 0)
        mark = "⚠️0" if nf == 0 else f"🕳{nf}"
        oob = " ⚡" if r.get("is_out_of_band") else ""
        srv = r.get("server_id") or "?"
        atype = r.get("analysis_type") or "?"
        keyboard.append([InlineKeyboardButton(
            f"{mark} {ts} • {srv} • {atype}{oob}",
            callback_data=f"lresult_{r['id']}",
        )])

    z = "1" if only_zero else "0"
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"lres_{max(0, offset - _RESULTS_PAGE)}_{z}"))
    if offset + _RESULTS_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"lres_{offset + _RESULTS_PAGE}_{z}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "Показать все" if only_zero else "⚠️ Только 0 дыр",
        callback_data=f"lres_0_{0 if only_zero else 1}",
    )])
    keyboard.append([InlineKeyboardButton("◀️ К очереди", callback_data="manage_loophole_queue")])

    markup = InlineKeyboardMarkup(keyboard)
    text = "\n".join(lines)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_LOOPHOLE_RESULTS


async def loophole_result_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Карточка одного анализа: сводка + кнопка скачать HTML-отчёт. callback: lresult_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS
    try:
        rid = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.answer("Некорректная запись.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS

    rec = await lq_get_result(rid)
    if not rec:
        await query.answer("Анализ не найден.", show_alert=True)
        return await show_all_analyses(update, context)

    report = rec.get("result") or {}
    label = await _loophole_user_label(rec["user_id"]) if rec.get("user_id") else "—"
    ts = rec["created_at"].strftime("%d.%m.%Y %H:%M") if rec.get("created_at") else "?"
    meta = (
        f"\n\n<i>#{rid} • {ts}{' ⚡oob' if rec.get('is_out_of_band') else ''}\n"
        f"сервер: {rec.get('server_id')} • тип: {rec.get('analysis_type')}\n"
        f"профиль: {rec.get('model_profile')} • режим: {rec.get('selection_mode')}\n"
        f"инициатор: {label} • дыр: {rec.get('total_findings')}</i>"
    )
    text = lq_format_summary(report) + meta

    keyboard = [
        [InlineKeyboardButton("📄 Скачать HTML-отчёт", callback_data=f"lresdoc_{rid}")],
        [InlineKeyboardButton("◀️ К списку", callback_data="show_all_analyses")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ADMIN_LOOPHOLE_RESULTS


async def loophole_result_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Прислать оформленный HTML-отчёт по архивной записи. callback: lresdoc_<id>."""
    import io
    query = update.callback_query
    await query.answer("Готовлю отчёт…")
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS
    try:
        rid = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        return ADMIN_LOOPHOLE_RESULTS
    rec = await lq_get_result(rid)
    if not rec:
        await query.answer("Анализ не найден.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS
    report = rec.get("result") or {}
    try:
        buf = io.BytesIO(lq_build_html_doc(report, title_suffix=f"архив #{rid}"))
        buf.name = f"analysis_{rid}.html"
        await context.bot.send_document(
            chat_id=query.message.chat_id, document=buf, filename=f"analysis_{rid}.html",
            caption=f"Анализ #{rid} • дыр: {rec.get('total_findings')}",
        )
    except Exception:
        logging.exception("[ADMIN] не удалось отправить HTML-отчёт анализа %s", rid)
        await query.answer("Не удалось сформировать отчёт.", show_alert=True)
    return ADMIN_LOOPHOLE_RESULTS


async def _render_job_detail(query, job_id: int) -> int:
    job = await lq_get_job(job_id)
    if not job:
        await query.answer("Задача не найдена.", show_alert=True)
        try:
            await query.edit_message_text(
                "Задача не найдена.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К очереди", callback_data="manage_loophole_queue")]]),
            )
        except Exception:
            pass
        return ADMIN_LOOPHOLE_QUEUE

    label = await _loophole_user_label(job['user_id'])
    pos = await lq_position(job_id) if job['status'] == 'queued' else 0
    p = job.get('params') or {}
    lines = [
        f"🕳 <b>Задача #{job_id}</b>",
        f"Пользователь: {label} (<code>{job['user_id']}</code>)",
        f"Приоритет: {_loophole_priority_label(job['priority'])} ({job['priority']})",
        f"Статус: <b>{job['status']}</b>" + (f", позиция {pos}" if pos else ""),
        f"Сервер: {p.get('server_id', '—')}",
        f"Тип: {p.get('analysis_type', '—')} | источник: {p.get('mode', '—')} | профиль: {p.get('profile', '—')}",
    ]
    keyboard = []
    if job['status'] == 'queued':
        keyboard.append([InlineKeyboardButton("⬆️ В начало", callback_data=f"loophole_jtop_{job_id}")])
        keyboard.append([
            InlineKeyboardButton("🔼 Выше", callback_data=f"loophole_jup_{job_id}"),
            InlineKeyboardButton("🔽 Ниже", callback_data=f"loophole_jdown_{job_id}"),
        ])
        keyboard.append([InlineKeyboardButton("🔢 Задать позицию", callback_data=f"loophole_jsetpos_{job_id}")])
        keyboard.append([InlineKeyboardButton("⚡ Запустить без очереди", callback_data=f"loophole_joob_{job_id}")])
        keyboard.append([InlineKeyboardButton("🚫 Отменить", callback_data=f"loophole_jcancel_{job_id}")])
    elif job['status'] == 'running':
        lines.append("\n⚙️ Задача выполняется — изменить/отменить нельзя (без прерывания).")
    keyboard.append([InlineKeyboardButton("◀️ К очереди", callback_data="manage_loophole_queue")])

    try:
        await query.edit_message_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception:
        pass
    return ADMIN_LOOPHOLE_JOB_DETAIL


async def loophole_job_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _render_job_detail(query, _loophole_job_id(query))


async def loophole_job_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    await lq_move_top(job_id)
    _wake_loophole(context)
    await query.answer("Поднято в начало очереди.")
    return await _render_job_detail(query, job_id)


async def loophole_job_up(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    cur = await lq_current_pos(job_id)
    if cur > 1:
        await lq_set_position(job_id, cur - 1)
        _wake_loophole(context)
        await query.answer("Перемещено выше.")
    else:
        await query.answer("Уже в начале очереди.")
    return await _render_job_detail(query, job_id)


async def loophole_job_down(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    cur = await lq_current_pos(job_id)
    if cur >= 1:
        await lq_set_position(job_id, cur + 1)
        _wake_loophole(context)
        await query.answer("Перемещено ниже.")
    else:
        await query.answer("Задачи нет в очереди.")
    return await _render_job_detail(query, job_id)


async def loophole_job_setpos_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    job_id = _loophole_job_id(query)
    context.user_data['loophole_setpos_job'] = job_id
    await query.edit_message_text(
        f"🔢 Введите новую позицию для задачи #{job_id} (целое число от 1).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data="manage_loophole_queue")]]),
    )
    return ADMIN_LOOPHOLE_SET_POSITION


async def loophole_job_set_position_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_id = context.user_data.get('loophole_setpos_job')
    txt = (update.message.text or "").strip()
    if not job_id or not txt.isdigit() or int(txt) < 1:
        await update.message.reply_text("Нужно положительное число. Попробуйте снова.")
        return ADMIN_LOOPHOLE_SET_POSITION
    await lq_set_position(int(job_id), int(txt))
    _wake_loophole(context)
    context.user_data.pop('loophole_setpos_job', None)
    await update.message.reply_text(f"✅ Позиция задачи #{job_id} обновлена.")
    return await manage_loophole_queue(update, context)


async def loophole_job_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    cancelled = await lq_cancel(job_id)
    if cancelled:
        await query.answer("Задача отменена.")
        try:
            await context.bot.send_message(
                chat_id=cancelled['chat_id'],
                text="⚠ Ваш запрос на анализ был отменён администратором.",
            )
        except Exception:
            pass
        return await manage_loophole_queue(update, context)
    await query.answer("Нельзя отменить (уже выполняется или завершена).", show_alert=True)
    return await _render_job_detail(query, job_id)


async def loophole_job_oob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    job = await lq_get_job(job_id)
    if not job or job['status'] != 'queued':
        await query.answer("Задачу нельзя запустить (не в очереди).", show_alert=True)
        return await _render_job_detail(query, job_id)
    # Отменяем исходную queued-задачу и запускаем её копию параллельно, без очереди.
    await lq_cancel(job_id)
    await lq_run_oob(context.application, job['params'], job['user_id'], job['chat_id'])
    _wake_loophole(context)
    await query.answer("Запущено параллельно, без очереди.")
    try:
        await context.bot.send_message(
            chat_id=job['chat_id'],
            text="⚡ Ваш анализ запущен администратором вне очереди — отчёт придёт в этот чат.",
        )
    except Exception:
        pass
    return await manage_loophole_queue(update, context)
