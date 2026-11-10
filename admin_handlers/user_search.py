"""Поиск пользователей, карточка пользователя, просмотр и закрытие дел."""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import config
from database.manager import db_manager
from integrations.google_sheets import log_admin_action_to_sheet
from config import (
    CHOOSING_ACTION,
    USER_SEARCH_INPUT,
    USER_SEARCH_RESULTS,
    USER_DETAILS_VIEW,
    ADMIN_VIEW_CASE_BY_ID,
)


# === ПОИСК ПОЛЬЗОВАТЕЛЕЙ ===

async def start_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает процесс поиска пользователей."""
    query = update.callback_query
    await query.answer()

    text = (
        "🔍 <b>Поиск пользователей</b>\n\n"
        "Введите запрос для поиска:\n"
        "• Telegram ID (число)\n"
        "• Username (с @ или без)\n"
        "• Имя или часть имени\n\n"
        "<i>Будут найдены первые 20 совпадений.</i>"
    )

    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return USER_SEARCH_INPUT


async def execute_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет поиск пользователей по запросу."""
    search_query = update.message.text.strip()

    # Сохраняем запрос для пагинации
    context.user_data['search_query'] = search_query
    context.user_data['search_page'] = 0

    # Определяем тип поиска
    results = []

    # Убираем @ если есть
    clean_query = search_query.lstrip('@')

    # Пробуем как ID
    try:
        user_id = int(clean_query)
        results = await db_manager.fetch(
            "SELECT user_id, full_name, username, is_admin, is_vip, is_unlimited, is_banned, is_tester "
            "FROM users WHERE user_id = $1",
            user_id
        )
    except ValueError:
        # Ищем по username или имени
        results = await db_manager.fetch(
            """
            SELECT user_id, full_name, username, is_admin, is_vip, is_unlimited, is_banned, is_tester
            FROM users
            WHERE username ILIKE $1 OR full_name ILIKE $1
            ORDER BY last_seen DESC
            LIMIT 20
            """,
            f"%{clean_query}%"
        )

    context.user_data['search_results'] = [dict(r) for r in results]

    return await show_user_search_results(update, context, is_message=True)


async def show_user_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, is_message: bool = False) -> int:
    """Показывает результаты поиска с пагинацией (10 на страницу)."""
    results = context.user_data.get('search_results', [])
    page = context.user_data.get('search_page', 0)
    search_query = context.user_data.get('search_query', '')

    per_page = 10
    total_pages = (len(results) + per_page - 1) // per_page if results else 1
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_results = results[start_idx:end_idx]

    if not results:
        text = f"🔍 <b>Результаты поиска:</b> «{search_query}»\n\n<i>Ничего не найдено.</i>"
    else:
        text = f"🔍 <b>Результаты поиска:</b> «{search_query}»\n"
        text += f"Найдено: {len(results)} | Страница {page + 1}/{total_pages}\n\n"

        for user in page_results:
            # Формируем статусы
            statuses = []
            if user['is_admin']:
                statuses.append("👑 Админ")
            if user['is_vip']:
                statuses.append("💎 VIP")
            if user.get('is_unlimited'):
                statuses.append("♾ Безлимит")
            if user['is_tester']:
                statuses.append("🧪 Тестер")
            if user['is_banned']:
                statuses.append("🚫 Бан")

            status_str = " | ".join(statuses) if statuses else "👤 Обычный"
            username_str = f"@{user['username']}" if user['username'] else "—"

            text += f"<b>{user['full_name']}</b>\n"
            text += f"   ID: <code>{user['user_id']}</code> | {username_str}\n"
            text += f"   {status_str}\n\n"

    # Кнопки
    keyboard = []

    # Кнопки для выбора пользователя
    for user in page_results:
        btn_text = f"👤 {user['full_name'][:20]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"user_details_{user['user_id']}")])

    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data="search_prev_page"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data="search_next_page"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔍 Новый поиск", callback_data="user_search")])
    keyboard.append([InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_message:
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    else:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

    return USER_SEARCH_RESULTS


async def search_change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меняет страницу результатов поиска."""
    query = update.callback_query

    if query.data == "search_prev_page":
        context.user_data['search_page'] = max(0, context.user_data.get('search_page', 0) - 1)
    else:  # search_next_page
        context.user_data['search_page'] = context.user_data.get('search_page', 0) + 1

    return await show_user_search_results(update, context)


async def show_user_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает карточку пользователя с кнопками управления."""
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.split("user_details_")[1])
    context.user_data['view_user_id'] = user_id
    return await _show_user_details_by_id(update, context, user_id)


async def quick_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Быстрое действие над пользователем из карточки."""
    query = update.callback_query
    data = query.data

    # Парсим действие и user_id
    if data.startswith("quick_ban_"):
        user_id = int(data.split("quick_ban_")[1])
        await db_manager.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1", user_id)
        action_text = "заблокирован"
        try:
            from services import license as _lic
            await _lic.ban_hwids_for_user(user_id, banned_by=query.from_user.id)  # каскад HWID-бана
        except Exception:
            logging.exception("[LICENSE] quick ban-cascade failed user_id=%s", user_id)
    elif data.startswith("quick_unban_"):
        user_id = int(data.split("quick_unban_")[1])
        await db_manager.execute("UPDATE users SET is_banned = FALSE WHERE user_id = $1", user_id)
        action_text = "разблокирован"
        try:
            from services import license as _lic
            await _lic.unban_hwids_for_user(user_id)  # снять каскадные HWID-баны
        except Exception:
            logging.exception("[LICENSE] quick unban-cascade failed user_id=%s", user_id)
    elif data.startswith("quick_add_vip_"):
        user_id = int(data.split("quick_add_vip_")[1])
        await db_manager.execute("UPDATE users SET is_vip = TRUE WHERE user_id = $1", user_id)
        action_text = "получил VIP"
    elif data.startswith("quick_remove_vip_"):
        user_id = int(data.split("quick_remove_vip_")[1])
        await db_manager.execute("UPDATE users SET is_vip = FALSE WHERE user_id = $1", user_id)
        action_text = "лишён VIP"
    elif data.startswith("quick_add_unlim_"):
        user_id = int(data.split("quick_add_unlim_")[1])
        await db_manager.execute("UPDATE users SET is_unlimited = TRUE WHERE user_id = $1", user_id)
        action_text = "получил безлимит (промокод)"
    elif data.startswith("quick_remove_unlim_"):
        user_id = int(data.split("quick_remove_unlim_")[1])
        await db_manager.execute("UPDATE users SET is_unlimited = FALSE WHERE user_id = $1", user_id)
        action_text = "лишён безлимита (промокод)"
    elif data.startswith("quick_add_tester_"):
        user_id = int(data.split("quick_add_tester_")[1])
        await db_manager.execute("UPDATE users SET is_tester = TRUE WHERE user_id = $1", user_id)
        action_text = "стал тестером"
    elif data.startswith("quick_remove_tester_"):
        user_id = int(data.split("quick_remove_tester_")[1])
        await db_manager.execute("UPDATE users SET is_tester = FALSE WHERE user_id = $1", user_id)
        action_text = "лишён статуса тестера"
    else:
        await query.answer("Неизвестное действие", show_alert=True)
        return USER_DETAILS_VIEW

    logging.info(f"Админ {query.from_user.id} выполнил действие: пользователь {user_id} {action_text}")
    await log_admin_action_to_sheet(query.from_user.id, f"QUICK_{data.upper()}", f"Target ID: {user_id}")

    await query.answer(f"✅ Пользователь {action_text}", show_alert=True)

    # Обновляем карточку — сохраняем user_id для повторного показа
    context.user_data['view_user_id'] = user_id
    return await _show_user_details_by_id(update, context, user_id)


async def _show_user_details_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Внутренняя функция для показа карточки пользователя по ID."""
    query = update.callback_query

    user = await db_manager.fetchrow(
        """
        SELECT user_id, full_name, username, is_admin, is_vip, is_unlimited, is_banned, is_tester,
               first_seen, last_seen
        FROM users WHERE user_id = $1
        """,
        user_id
    )

    if not user:
        await query.edit_message_text(
            "❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]])
        )
        return CHOOSING_ACTION

    # Формируем карточку
    statuses = []
    if user['is_admin']:
        statuses.append("👑 Администратор")
    if user['is_vip']:
        statuses.append("💎 VIP")
    if user.get('is_unlimited'):
        statuses.append("♾ Безлимит (промокод)")
    if user.get('is_tester'):
        statuses.append("🧪 Тестировщик")
    if user['is_banned']:
        statuses.append("🚫 Заблокирован")
    if not statuses:
        statuses.append("👤 Обычный пользователь")

    username_str = f"@{user['username']}" if user['username'] else "не указан"
    first_seen = user['first_seen'].strftime('%d.%m.%Y %H:%M') if user['first_seen'] else "—"
    last_seen = user['last_seen'].strftime('%d.%m.%Y %H:%M') if user['last_seen'] else "—"

    text = (
        f"👤 <b>Карточка пользователя</b>\n\n"
        f"<b>Имя:</b> {user['full_name']}\n"
        f"<b>Username:</b> {username_str}\n"
        f"<b>ID:</b> <code>{user['user_id']}</code>\n\n"
        f"<b>Статусы:</b>\n" + "\n".join(f"  • {s}" for s in statuses) + "\n\n"
        f"<b>Первый визит:</b> {first_seen}\n"
        f"<b>Последний визит:</b> {last_seen}"
    )

    # Кнопки управления
    keyboard = []

    # Бан/Разбан
    if user['is_banned']:
        keyboard.append([InlineKeyboardButton("🟢 Разбанить", callback_data=f"quick_unban_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 Забанить", callback_data=f"quick_ban_{user_id}")])

    # VIP
    if user['is_vip']:
        keyboard.append([InlineKeyboardButton("💎 Снять VIP", callback_data=f"quick_remove_vip_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("💎 Дать VIP", callback_data=f"quick_add_vip_{user_id}")])

    # Безлимит за промокод (is_unlimited): безлимит + оверлей, как VIP
    if user.get('is_unlimited'):
        keyboard.append([InlineKeyboardButton("♾ Снять безлимит", callback_data=f"quick_remove_unlim_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("♾ Дать безлимит", callback_data=f"quick_add_unlim_{user_id}")])

    # Тестер
    if user.get('is_tester'):
        keyboard.append([InlineKeyboardButton("🧪 Снять тестера", callback_data=f"quick_remove_tester_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🧪 Дать тестера", callback_data=f"quick_add_tester_{user_id}")])

    keyboard.append([InlineKeyboardButton("◀️ К результатам", callback_data="back_to_search_results")])
    keyboard.append([InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")])

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return USER_DETAILS_VIEW



async def back_to_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает к результатам поиска."""
    return await show_user_search_results(update, context)


# === ПРОСМОТР ДЕЛА ПО ID ===

async def prompt_view_case_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает у админа ID дела для просмотра."""
    query = update.callback_query
    await query.answer()

    text = (
        "📋 <b>Просмотр дела по ID</b>\n\n"
        "Введите номер дела (ID) для просмотра:\n\n"
        "<i>Это может быть ID дела из Ролевого Помощника (assistant_cases) "
        "или ID уголовного дела (criminal_case_sessions).</i>"
    )

    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_VIEW_CASE_BY_ID


async def view_case_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает информацию о деле по введённому ID."""
    case_id_str = update.message.text.strip()

    try:
        case_id = int(case_id_str)
    except ValueError:
        await update.message.reply_text(
            "❌ ID должен быть числом. Попробуйте снова или нажмите 'Отмена'.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]])
        )
        return ADMIN_VIEW_CASE_BY_ID

    # Пробуем найти в assistant_cases
    assistant_case = await db_manager.fetchrow("""
        SELECT ac.id, ac.user_id, ac.server_id, ac.rp_role, ac.case_stage, ac.status,
               ac.created_at, ac.last_updated_at,
               u.full_name, u.username
        FROM assistant_cases ac
        LEFT JOIN users u ON ac.user_id = u.user_id
        WHERE ac.id = $1
    """, case_id)

    # Пробуем найти в criminal_case_sessions
    criminal_case = await db_manager.fetchrow("""
        SELECT ccs.id, ccs.user_id, ccs.case_number, ccs.server_id, ccs.status,
               ccs.created_at, ccs.updated_at, ccs.google_doc_url,
               u.full_name, u.username
        FROM criminal_case_sessions ccs
        LEFT JOIN users u ON ccs.user_id = u.user_id
        WHERE ccs.id = $1
    """, case_id)

    if not assistant_case and not criminal_case:
        await update.message.reply_text(
            f"❌ Дело с ID <code>{case_id}</code> не найдено ни в Ролевом Помощнике, ни в Уголовных делах.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
        return CHOOSING_ACTION

    text = ""
    keyboard = []

    # Если нашли в assistant_cases
    if assistant_case:
        from user_handlers.assistant_flow import ASSISTANT_ROLES, ASSISTANT_STAGES

        role_name = ASSISTANT_ROLES.get(assistant_case['rp_role'], assistant_case['rp_role'])
        stage_name = ASSISTANT_STAGES.get(assistant_case['case_stage'], assistant_case['case_stage'])
        server_name = config.SERVERS_CONFIG.get(assistant_case['server_id'], {}).get('display_name', assistant_case['server_id'])

        username_str = f"@{assistant_case['username']}" if assistant_case['username'] else "—"
        created = assistant_case['created_at'].strftime('%d.%m.%Y %H:%M') if assistant_case['created_at'] else "—"
        updated = assistant_case['last_updated_at'].strftime('%d.%m.%Y %H:%M') if assistant_case['last_updated_at'] else "—"

        status_emoji = "🟢" if assistant_case['status'] == 'active' else "🔴"

        text = (
            f"📋 <b>Дело Ролевого Помощника #{case_id}</b>\n\n"
            f"<b>Пользователь:</b> {assistant_case['full_name']} ({username_str})\n"
            f"<b>User ID:</b> <code>{assistant_case['user_id']}</code>\n\n"
            f"<b>Сервер:</b> {server_name}\n"
            f"<b>Роль:</b> {role_name}\n"
            f"<b>Стадия:</b> {stage_name}\n"
            f"<b>Статус:</b> {status_emoji} {assistant_case['status']}\n\n"
            f"<b>Создано:</b> {created}\n"
            f"<b>Обновлено:</b> {updated}"
        )

        keyboard.append([InlineKeyboardButton("💬 Показать историю сообщений", callback_data=f"admin_case_history_{case_id}")])
        if assistant_case['status'] == 'active':
            keyboard.append([InlineKeyboardButton("🔴 Закрыть дело", callback_data=f"admin_close_assist_{case_id}")])

    # Если нашли в criminal_case_sessions
    if criminal_case:
        if text:
            text += "\n\n" + "=" * 30 + "\n\n"

        server_name = config.SERVERS_CONFIG.get(criminal_case['server_id'], {}).get('display_name', criminal_case['server_id'] or "—")
        username_str = f"@{criminal_case['username']}" if criminal_case['username'] else "—"
        created = criminal_case['created_at'].strftime('%d.%m.%Y %H:%M') if criminal_case['created_at'] else "—"
        updated = criminal_case['updated_at'].strftime('%d.%m.%Y %H:%M') if criminal_case['updated_at'] else "—"

        status_emoji = "🟢" if criminal_case['status'] == 'active' else "✅" if criminal_case['status'] == 'completed' else "🔴"

        text += (
            f"📋 <b>Уголовное дело #{case_id}</b>\n\n"
            f"<b>Номер дела:</b> {criminal_case['case_number'] or '—'}\n"
            f"<b>Пользователь:</b> {criminal_case['full_name']} ({username_str})\n"
            f"<b>User ID:</b> <code>{criminal_case['user_id']}</code>\n\n"
            f"<b>Сервер:</b> {server_name}\n"
            f"<b>Статус:</b> {status_emoji} {criminal_case['status']}\n\n"
            f"<b>Создано:</b> {created}\n"
            f"<b>Обновлено:</b> {updated}"
        )

        if criminal_case['google_doc_url']:
            keyboard.append([InlineKeyboardButton("📄 Открыть Google Doc", url=criminal_case['google_doc_url'])])
        if criminal_case['status'] == 'active':
            keyboard.append([InlineKeyboardButton("🔴 Закрыть дело", callback_data=f"admin_close_ccw_{case_id}")])

    keyboard.append([InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")])
    keyboard.append([InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")])

    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_ACTION


async def show_admin_case_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает историю сообщений дела для администратора."""
    query = update.callback_query
    await query.answer()

    case_id = int(query.data.split("admin_case_history_")[1])

    # Получаем историю сообщений
    messages = await db_manager.fetch("""
        SELECT sender_role, content, timestamp
        FROM assistant_messages
        WHERE case_id = $1
        ORDER BY timestamp ASC
    """, case_id)

    if not messages:
        await query.edit_message_text(
            f"📋 <b>История дела #{case_id}</b>\n\n<i>Сообщений не найдено.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад к делу", callback_data=f"view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
        return CHOOSING_ACTION

    # Формируем текст истории
    text = f"📋 <b>История дела #{case_id}</b>\n"
    text += f"<i>Всего сообщений: {len(messages)}</i>\n\n"

    for msg in messages:
        sender = "👤 Пользователь" if msg['sender_role'] == 'user' else "🤖 Ассистент"
        time_str = msg['timestamp'].strftime('%d.%m %H:%M') if msg['timestamp'] else ""

        # Ограничиваем длину сообщения
        content = msg['content']
        if len(content) > 500:
            content = content[:500] + "..."

        # Экранируем HTML
        import html
        content = html.escape(content)

        text += f"<b>{sender}</b> <i>({time_str})</i>\n{content}\n\n"

        # Ограничиваем общую длину
        if len(text) > 3500:
            text += f"\n<i>... и ещё {len(messages) - messages.index(msg) - 1} сообщений</i>"
            break

    await query.edit_message_text(
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
            [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
        ])
    )
    return CHOOSING_ACTION


async def admin_close_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Закрывает дело из админ-панели (assistant_cases или criminal_case_sessions)."""
    query = update.callback_query
    await query.answer()

    # callback_data: admin_close_assist_{id} или admin_close_ccw_{id}
    parts = query.data.split("_")  # ['admin', 'close', 'assist'|'ccw', '{id}']
    case_type = parts[2]  # 'assist' или 'ccw'
    case_id = int(parts[3])

    if case_type == "assist":
        await db_manager.execute(
            "UPDATE assistant_cases SET status = 'closed', last_updated_at = NOW() WHERE id = $1", case_id
        )
        logging.info(f"[Admin] Администратор {query.from_user.id} закрыл дело ассистента #{case_id}")
        await log_admin_action_to_sheet(query.from_user.id, "CLOSE_ASSISTANT_CASE", f"Case ID: {case_id}")
        await query.edit_message_text(
            f"✅ Дело Ролевого Помощника <b>#{case_id}</b> закрыто.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
    elif case_type == "ccw":
        await db_manager.execute(
            "UPDATE criminal_case_sessions SET status = 'closed', updated_at = NOW() WHERE id = $1", case_id
        )
        logging.info(f"[Admin] Администратор {query.from_user.id} закрыл уголовное дело #{case_id}")
        await log_admin_action_to_sheet(query.from_user.id, "CLOSE_CRIMINAL_CASE", f"Case ID: {case_id}")
        await query.edit_message_text(
            f"✅ Уголовное дело <b>#{case_id}</b> закрыто.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
    else:
        await query.edit_message_text("❌ Неизвестный тип дела.")

    return CHOOSING_ACTION
