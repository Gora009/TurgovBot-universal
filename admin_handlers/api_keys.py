"""REST API: запуск/остановка сервера, управление API-ключами."""

import logging
import os
import subprocess
import sys
import secrets
import hashlib
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.manager import db_manager
from config import (
    SUPER_ADMIN_ID,
    API_SERVER_PORT,
    API_SERVER_HOST,
    API_DEFAULT_RPM,
    MANAGE_API_KEYS,
    GET_API_KEY_NAME,
    GET_API_KEY_TYPE,
    GET_API_KEY_ORIGINS,
    GET_API_KEY_IPS,
    GET_API_KEY_RPM,
    CONFIRM_DELETE_API_KEY,
    VIEW_API_KEY_DETAILS,
    VIEW_API_KEYS_LIST,
    CONFIRM_ROTATE_API_KEY,
)


# === УПРАВЛЕНИЕ REST API ===

def _generate_api_key() -> tuple[str, str, str]:
    """Генерирует API-ключ, его хеш и префикс."""
    random_bytes = secrets.token_urlsafe(32)
    full_key = f"sk_live_{random_bytes}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key_prefix = full_key[:12]
    return full_key, key_hash, key_prefix


def _is_api_server_running(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, запущен ли API сервер (subprocess или Docker-контейнер)."""
    # Способ 1: subprocess (legacy, для обратной совместимости)
    process = context.application.bot_data.get('api_server_process')
    if process is not None and process.poll() is None:
        return True
    # Способ 2: API может быть запущен как отдельный Docker-сервис — проверяем через HTTP
    try:
        import urllib.request
        url = f"http://{API_SERVER_HOST if API_SERVER_HOST != '0.0.0.0' else '127.0.0.1'}:{API_SERVER_PORT}/health"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _notify_super_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Отправляет уведомление супер-админу."""
    try:
        await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=message, parse_mode='HTML')
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления супер-админу: {e}")


async def _notify_api_action(context: ContextTypes.DEFAULT_TYPE, admin_id: int, action: str, details: str = ""):
    """
    Уведомляет супер-админа о действиях обычных админов в API.
    Если действие выполнил сам супер-админ, уведомление не отправляется.
    """
    if admin_id == SUPER_ADMIN_ID:
        return  # Не уведомляем супер-админа о его собственных действиях

    # Получаем информацию об админе
    admin_info = await db_manager.fetchrow(
        "SELECT user_id, username, full_name FROM users WHERE user_id = $1",
        admin_id
    )
    admin_name = "Неизвестный"
    if admin_info:
        if admin_info.get('username'):
            admin_name = f"@{admin_info['username']}"
        elif admin_info.get('full_name'):
            admin_name = admin_info['full_name']
        else:
            admin_name = f"ID: {admin_id}"

    message = (
        f"🔔 <b>Действие в API</b>\n\n"
        f"👤 <b>Админ:</b> {admin_name} (<code>{admin_id}</code>)\n"
        f"⚡ <b>Действие:</b> {action}\n"
    )
    if details:
        message += f"📝 <b>Детали:</b> {details}"

    await _notify_super_admin(context, message)


async def manage_api_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню управления API."""
    query = update.callback_query
    await query.answer()

    # Проверяем статус сервера
    is_running = _is_api_server_running(context)
    server_status = "🟢 Запущен" if is_running else "🔴 Остановлен"

    # Считаем ключи
    total_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys") or 0
    active_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE is_active = true") or 0

    text = f"""🔑 <b>Управление REST API</b>

<b>Статус сервера:</b> {server_status}
<b>Порт:</b> {API_SERVER_PORT}

<b>API-ключи:</b> {active_keys} активных / {total_keys} всего
"""

    # Кнопки
    if is_running:
        server_btn = InlineKeyboardButton("⏹️ Остановить API", callback_data="api_stop_server")
    else:
        server_btn = InlineKeyboardButton("🚀 Запустить API", callback_data="api_start_server")

    keyboard = [
        [server_btn],
        [InlineKeyboardButton("📊 Статус API", callback_data="api_server_status")],
        [InlineKeyboardButton("➕ Создать ключ", callback_data="api_create_key")],
        [InlineKeyboardButton(f"📋 Список ключей ({total_keys})", callback_data="api_list_keys")],
        [InlineKeyboardButton("📈 Статистика", callback_data="api_stats")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MANAGE_API_KEYS


async def api_start_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает API сервер."""
    query = update.callback_query
    await query.answer()

    if _is_api_server_running(context):
        await query.edit_message_text(
            "⚠️ API сервер уже запущен!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
        return MANAGE_API_KEYS

    try:
        # Получаем корневую директорию проекта
        project_root = os.path.dirname(os.path.abspath(__file__))
        run_server_path = os.path.join(project_root, "api", "run_server.py")

        # Запускаем ��ервер через subprocess
        process = subprocess.Popen(
            [sys.executable, run_server_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_root,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        )

        # Даем серверу время на запуск
        import asyncio
        await asyncio.sleep(2)

        # Проверяем, не упал ли процесс сразу
        if process.poll() is not None:
            # Процесс уже завершился - читаем ошибку
            stdout, stderr = process.communicate(timeout=5)
            error_msg = stderr.decode('utf-8', errors='replace')[:1000] if stderr else "Нет данных об ошибке"
            stdout_msg = stdout.decode('utf-8', errors='replace')[:500] if stdout else ""

            logging.error(f"API сервер упал при запуске: {error_msg}")

            # Экранируем HTML-символы чтобы избежать ошибок парсинга
            import html
            error_msg_safe = html.escape(error_msg)
            stdout_msg_safe = html.escape(stdout_msg)

            await query.edit_message_text(
                f"❌ <b>API сервер упал при запуске!</b>\n\n"
                f"<b>Код возврата:</b> {process.returncode}\n\n"
                f"<b>Ошибка:</b>\n<pre>{error_msg_safe}</pre>\n\n"
                f"<b>Вывод:</b>\n<pre>{stdout_msg_safe}</pre>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Повторить", callback_data="api_start_server")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
                ])
            )
            return MANAGE_API_KEYS

        context.application.bot_data['api_server_process'] = process

        logging.info(f"API сервер запущен, PID: {process.pid}")

        await query.edit_message_text(
            f"✅ <b>API сервер запущен!</b>\n\n"
            f"PID: <code>{process.pid}</code>\n"
            f"Адрес: <code>http://{API_SERVER_HOST}:{API_SERVER_PORT}</code>\n\n"
            f"📚 <b>Документация:</b>\n"
            f"<code>http://localhost:{API_SERVER_PORT}/verblud?api_key=ВАШ_КЛЮЧ</code>\n\n"
            f"⚠️ Для доступа к документации нужен API-ключ!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            query.from_user.id,
            "🚀 Запуск API сервера",
            f"PID: {process.pid}"
        )

    except Exception as e:
        logging.error(f"Ошибка запуска API сервера: {e}")
        await query.edit_message_text(
            f"❌ Ошибка запуска API сервера:\n<code>{str(e)[:500]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

    return MANAGE_API_KEYS


async def api_stop_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Останавливает API сервер."""
    query = update.callback_query
    await query.answer()

    process = context.application.bot_data.get('api_server_process')

    if not process or process.poll() is not None:
        await query.edit_message_text(
            "⚠️ API сервер не запущен!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
        return MANAGE_API_KEYS

    try:
        pid = process.pid
        process.terminate()
        process.wait(timeout=5)

        context.application.bot_data['api_server_process'] = None

        logging.info(f"API сервер остановлен, PID: {pid}")

        await query.edit_message_text(
            f"✅ <b>API сервер остановлен!</b>\n\nPID: <code>{pid}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            query.from_user.id,
            "⏹️ Остановка API сервера",
            f"PID: {pid}"
        )

    except Exception as e:
        logging.error(f"Ошибка остановки API сервера: {e}")
        await query.edit_message_text(
            f"❌ Ошибка остановки API сервера:\n<code>{str(e)[:500]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

    return MANAGE_API_KEYS


async def api_server_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает статус API сервера."""
    query = update.callback_query
    await query.answer()

    is_running = _is_api_server_running(context)
    process = context.application.bot_data.get('api_server_process')

    if is_running and process:
        text = f"""📊 <b>Статус API сервера</b>

<b>Состояние:</b> 🟢 Запущен
<b>PID:</b> <code>{process.pid}</code>
<b>Адрес:</b> <code>http://{API_SERVER_HOST}:{API_SERVER_PORT}</code>

<b>Эндпоинты:</b>
• <code>POST /v1/ask</code> — ответ на вопрос
• <code>POST /v1/criminal-case/start</code> — создание дела
• <code>GET /health</code> — проверка здоровья
• <code>GET /verblud</code> — документация (требует API-ключ)"""
    else:
        text = """📊 <b>Статус API сервера</b>

<b>Состояние:</b> 🔴 Остановлен

Нажмите "Запустить API" для старта сервера."""

    try:
        await query.edit_message_text(
            text=text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="api_server_status")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
    except Exception as e:
        # Игнорируем ошибку "Message is not modified"
        if "Message is not modified" not in str(e):
            raise
    return MANAGE_API_KEYS


async def api_create_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает процесс создания ключа — выбор типа."""
    query = update.callback_query
    await query.answer()

    text = """➕ <b>Создание API-ключа</b>

Выберите тип ключа:

🌐 <b>Web</b> — для сайтов
• Требует указания доменов (CORS)
• Проверяет Origin/Referer

📱 <b>App</b> — для приложений
• Без CORS
• Блокирует браузерные запросы"""

    keyboard = [
        [InlineKeyboardButton("🌐 Для сайта (Web)", callback_data="api_key_type_web")],
        [InlineKeyboardButton("📱 Для приложения (App)", callback_data="api_key_type_app")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GET_API_KEY_TYPE


async def api_key_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор типа ключа."""
    query = update.callback_query
    await query.answer()

    key_type = "web" if "web" in query.data else "app"
    context.user_data['api_key_type'] = key_type

    await query.edit_message_text(
        f"📝 Введите название для ключа (например, 'Production Site' или 'Mobile App'):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
        ])
    )
    return GET_API_KEY_NAME


async def api_key_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает название ключа."""
    name = update.message.text.strip()[:100]
    context.user_data['api_key_name'] = name

    key_type = context.user_data.get('api_key_type', 'app')

    if key_type == 'web':
        await update.message.reply_text(
            f"🌐 Введите разрешённые домены через запятую:\n\n"
            f"Примеры:\n"
            f"• <code>https://mysite.com</code>\n"
            f"• <code>https://mysite.com, https://api.mysite.com</code>\n"
            f"• <code>*.mysite.com</code> (wildcard поддомены)",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
            ])
        )
        return GET_API_KEY_ORIGINS
    else:
        # Для app-ключей сразу спрашиваем IP whitelist
        await update.message.reply_text(
            f"🔒 Введите разрешённые IP-адреса через запятую (или нажмите 'Пропустить'):\n\n"
            f"Примеры:\n"
            f"• <code>1.2.3.4</code>\n"
            f"• <code>1.2.3.4, 5.6.7.8</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Пропустить", callback_data="api_key_skip_ips")],
                [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
            ])
        )
        return GET_API_KEY_IPS


async def api_key_receive_origins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает разрешённые домены для web-ключа."""
    origins_text = update.message.text.strip()
    origins = [o.strip() for o in origins_text.split(',') if o.strip()]
    context.user_data['api_key_origins'] = origins

    await update.message.reply_text(
        f"🔒 Введите разрешённые IP-адреса через запятую (или нажмите 'Пропустить'):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Пропустить", callback_data="api_key_skip_ips")],
            [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
        ])
    )
    return GET_API_KEY_IPS


async def api_key_receive_ips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает IP whitelist."""
    ips_text = update.message.text.strip()
    ips = [ip.strip() for ip in ips_text.split(',') if ip.strip()]
    context.user_data['api_key_ips'] = ips

    return await _create_api_key(update, context)


async def api_key_skip_ips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пропускает ввод IP whitelist."""
    query = update.callback_query
    await query.answer()

    context.user_data['api_key_ips'] = []

    return await _create_api_key(update, context, from_callback=True)


async def _create_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> int:
    """Создаёт API-ключ в БД и показывает результат."""
    user_id = update.effective_user.id

    # Получаем данные из контекста
    key_type = context.user_data.get('api_key_type', 'app')
    name = context.user_data.get('api_key_name', 'Unnamed')
    origins = context.user_data.get('api_key_origins', [])
    ips = context.user_data.get('api_key_ips', [])

    # Генерируем ключ
    full_key, key_hash, key_prefix = _generate_api_key()

    # Сохраняем в БД
    try:
        query = """
            INSERT INTO api_keys (key_hash, key_prefix, name, owner_user_id, key_type, 
                                  allowed_origins, allowed_ips, requests_per_minute)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        """
        key_id = await db_manager.fetchval(
            query, key_hash, key_prefix, name, user_id, key_type,
            origins if origins else None,
            ips if ips else None,
            API_DEFAULT_RPM
        )

        logging.info(f"Создан API-ключ #{key_id} ({key_type}) для пользователя {user_id}")

        # Формируем сообщение с ключом
        type_emoji = "🌐" if key_type == "web" else "📱"
        origins_text = ", ".join(origins) if origins else "Не указаны"
        ips_text = ", ".join(ips) if ips else "Не указаны"

        text = f"""✅ <b>API-ключ создан!</b>

🔑 <b>Ключ (СОХРАНИТЕ, показывается ОДИН раз):</b>
<code>{full_key}</code>

📋 <b>Настройки:</b>
• Тип: {type_emoji} {key_type.upper()}
• Название: {name}
• Домены: {origins_text}
• IP whitelist: {ips_text}
• Лимит: {API_DEFAULT_RPM} запросов/мин

⚠️ <b>Этот ключ больше не будет показан!</b>"""

        keyboard = [[InlineKeyboardButton("◀️ К списку ключей", callback_data="manage_api_keys")]]

        if from_callback:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            user_id,
            "🔑 Создание API-ключа",
            f"ID: #{key_id}, Тип: {key_type}, Название: {name}"
        )

    except Exception as e:
        logging.error(f"Ошибка создания API-ключа: {e}")
        error_text = f"❌ Ошибка создания ключа:\n<code>{str(e)[:500]}</code>"
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]]

        if from_callback:
            await update.callback_query.edit_message_text(
                text=error_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=error_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    # Очищаем контекст
    for key in ['api_key_type', 'api_key_name', 'api_key_origins', 'api_key_ips']:
        context.user_data.pop(key, None)

    return MANAGE_API_KEYS


async def api_list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список API-ключей с пагинацией."""
    query = update.callback_query
    await query.answer()

    page = context.user_data.get('api_keys_page', 0)
    per_page = 5

    # Получаем ключи
    keys = await db_manager.fetch("""
        SELECT id, key_prefix, name, key_type, is_active, total_requests, last_used_at
        FROM api_keys
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
    """, per_page + 1, page * per_page)

    total = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys") or 0

    if not keys:
        await query.edit_message_text(
            "📋 <b>API-ключи</b>\n\n<i>Нет созданных ключей.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Создать ключ", callback_data="api_create_key")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
        return MANAGE_API_KEYS

    has_next = len(keys) > per_page
    keys = keys[:per_page]

    total_pages = (total + per_page - 1) // per_page
    text = f"📋 <b>API-ключи</b> (стр. {page + 1}/{total_pages})\n\n"

    keyboard = []
    for key in keys:
        status_emoji = "🟢" if key['is_active'] else "🔴"
        type_emoji = "🌐" if key['key_type'] == 'web' else "📱"

        last_used = "никогда"
        if key['last_used_at']:
            delta = datetime.now(key['last_used_at'].tzinfo) - key['last_used_at']
            if delta.days > 0:
                last_used = f"{delta.days}д назад"
            elif delta.seconds > 3600:
                last_used = f"{delta.seconds // 3600}ч назад"
            elif delta.seconds > 60:
                last_used = f"{delta.seconds // 60}м назад"
            else:
                last_used = "только что"

        text += f"{status_emoji} <code>{key['key_prefix']}...</code> — {key['name']} {type_emoji}\n"
        text += f"   📊 {key['total_requests']} запросов | ⏱ {last_used}\n\n"

        keyboard.append([InlineKeyboardButton(
            f"{status_emoji} {key['key_prefix']}... — {key['name'][:20]}",
            callback_data=f"api_key_details_{key['id']}"
        )])

    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data="api_keys_prev_page"))
    if has_next:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data="api_keys_next_page"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("◀️ Меню API", callback_data="manage_api_keys")])

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return VIEW_API_KEYS_LIST


async def api_keys_change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меняет страницу списка ключей."""
    query = update.callback_query
    await query.answer()

    page = context.user_data.get('api_keys_page', 0)

    if "next" in query.data:
        context.user_data['api_keys_page'] = page + 1
    else:
        context.user_data['api_keys_page'] = max(0, page - 1)

    return await api_list_keys(update, context)


async def api_key_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детали API-ключа."""
    query = update.callback_query
    await query.answer()

    # Получаем key_id либо из callback_data, либо из контекста
    if query.data and query.data.startswith('api_key_details_'):
        key_id = int(query.data.split('_')[-1])
    else:
        key_id = context.user_data.get('viewing_api_key_id')

    if not key_id:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    context.user_data['viewing_api_key_id'] = key_id

    key = await db_manager.fetchrow("""
        SELECT id, key_prefix, name, key_type, owner_user_id, allowed_origins, allowed_ips,
               is_active, requests_per_minute, total_requests, last_used_at, last_ip, created_at
        FROM api_keys WHERE id = $1
    """, key_id)

    if not key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    status = "🟢 Активен" if key['is_active'] else "🔴 Отключён"
    type_emoji = "🌐 Web" if key['key_type'] == 'web' else "📱 App"

    origins = ", ".join(key['allowed_origins']) if key['allowed_origins'] else "Не указаны"
    ips = ", ".join(key['allowed_ips']) if key['allowed_ips'] else "Не указаны"
    last_used = key['last_used_at'].strftime('%d.%m.%Y %H:%M') if key['last_used_at'] else "никогда"
    created = key['created_at'].strftime('%d.%m.%Y %H:%M') if key['created_at'] else "N/A"

    text = f"""🔑 <b>Детали API-ключа</b>

<b>Префикс:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}
<b>Тип:</b> {type_emoji}
<b>Статус:</b> {status}

<b>Владелец:</b> <code>{key['owner_user_id']}</code>
<b>Создан:</b> {created}

<b>Лимит:</b> {key['requests_per_minute']} RPM
<b>Всего запросов:</b> {key['total_requests']}
<b>Последний запрос:</b> {last_used}
<b>Последний IP:</b> <code>{key['last_ip'] or 'N/A'}</code>

<b>Домены (CORS):</b>
{origins}

<b>IP whitelist:</b>
{ips}"""

    # Кнопки управления
    keyboard = []

    if key['is_active']:
        keyboard.append([InlineKeyboardButton("🔴 Отключить", callback_data=f"api_key_disable_{key_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🟢 Включить", callback_data=f"api_key_enable_{key_id}")])

    keyboard.append([InlineKeyboardButton(f"⚡ Изменить лимит ({key['requests_per_minute']} RPM)", callback_data=f"api_key_rpm_{key_id}")])
    keyboard.append([InlineKeyboardButton("🔄 Ротировать ключ", callback_data=f"api_key_rotate_{key_id}")])
    keyboard.append([InlineKeyboardButton("🗑️ Удалить", callback_data=f"api_key_delete_{key_id}")])
    keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data="api_list_keys")])

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return VIEW_API_KEY_DETAILS


async def api_key_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включает/отключает API-ключ."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])
    enable = "enable" in query.data

    # Получаем информацию о ключе для уведомления
    key_info = await db_manager.fetchrow(
        "SELECT name, owner_user_id FROM api_keys WHERE id = $1", key_id
    )

    await db_manager.execute(
        "UPDATE api_keys SET is_active = $1 WHERE id = $2",
        enable, key_id
    )

    action = "включён" if enable else "отключён"
    action_emoji = "🟢" if enable else "🔴"
    logging.info(f"API-ключ #{key_id} {action} администратором {query.from_user.id}")

    # Уведомляем супер-админа (если это не сам супер-админ)
    if key_info:
        await _notify_api_action(
            context,
            query.from_user.id,
            f"{action_emoji} Ключ {action}",
            f"ID: #{key_id}, Название: {key_info['name']}"
        )

    await query.answer(f"✅ Ключ {action}!")
    return await api_key_details(update, context)


async def api_key_rpm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает меню выбора лимита RPM."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])
    context.user_data['editing_api_key_id'] = key_id

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name, requests_per_minute FROM api_keys WHERE id = $1", key_id
    )

    if not key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    current_rpm = key['requests_per_minute']

    text = f"""⚡ <b>Изменение лимита RPM</b>

<b>Ключ:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}
<b>Текущий лимит:</b> {current_rpm} запросов/мин

Выберите новый лимит или введите своё значение:"""

    # Предустановленные значения
    presets = [5, 10, 20, 30, 50, 100]
    keyboard = []

    # Создаём кнопки по 3 в ряд
    row = []
    for rpm in presets:
        label = f"{'✓ ' if rpm == current_rpm else ''}{rpm} RPM"
        row.append(InlineKeyboardButton(label, callback_data=f"api_key_set_rpm_{key_id}_{rpm}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"api_key_rpm_custom_{key_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"api_key_details_{key_id}")])

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return VIEW_API_KEY_DETAILS


async def api_key_set_rpm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Устанавливает выбранный лимит RPM."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    key_id = int(parts[-2])
    new_rpm = int(parts[-1])

    key_info = await db_manager.fetchrow(
        "SELECT name, requests_per_minute FROM api_keys WHERE id = $1", key_id
    )

    if not key_info:
        await query.edit_message_text("❌ Ключ не найден.")
        return MANAGE_API_KEYS

    old_rpm = key_info['requests_per_minute']

    await db_manager.execute(
        "UPDATE api_keys SET requests_per_minute = $1 WHERE id = $2",
        new_rpm, key_id
    )

    logging.info(f"API-ключ #{key_id}: лимит изменён {old_rpm} -> {new_rpm} RPM администратором {query.from_user.id}")

    # Уведомляем супер-админа
    await _notify_api_action(
        context,
        query.from_user.id,
        "⚡ Изменение лимита RPM",
        f"ID: #{key_id}, Название: {key_info['name']}, {old_rpm} → {new_rpm} RPM"
    )

    await query.answer(f"✅ Лимит изменён на {new_rpm} RPM!")

    # Обновляем контекст для возврата к деталям ключа
    context.user_data['viewing_api_key_id'] = key_id
    return await api_key_details(update, context)


async def api_key_rpm_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает ввод кастомного лимита RPM."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])
    context.user_data['editing_api_key_id'] = key_id

    await query.edit_message_text(
        "✏️ <b>Введите новый лимит RPM</b>\n\n"
        "Укажите число от 1 до 1000:\n"
        "(например: <code>25</code>)",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
        ])
    )
    return GET_API_KEY_RPM


async def api_key_rpm_custom_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает кастомный лимит RPM от пользователя."""
    key_id = context.user_data.get('editing_api_key_id')

    if not key_id:
        await update.message.reply_text("❌ Ошибка: ключ не найден. Попробуйте снова.")
        return MANAGE_API_KEYS

    try:
        new_rpm = int(update.message.text.strip())
        if new_rpm < 1 or new_rpm > 1000:
            raise ValueError("Out of range")
    except ValueError:
        await update.message.reply_text(
            "❌ Некорректное значение. Введите число от 1 до 1000:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
            ])
        )
        return GET_API_KEY_RPM

    key_info = await db_manager.fetchrow(
        "SELECT name, requests_per_minute FROM api_keys WHERE id = $1", key_id
    )

    if not key_info:
        await update.message.reply_text("❌ Ключ не найден.")
        return MANAGE_API_KEYS

    old_rpm = key_info['requests_per_minute']

    await db_manager.execute(
        "UPDATE api_keys SET requests_per_minute = $1 WHERE id = $2",
        new_rpm, key_id
    )

    logging.info(f"API-ключ #{key_id}: лимит изменён {old_rpm} -> {new_rpm} RPM администратором {update.effective_user.id}")

    # Уведомляем супер-админа
    await _notify_api_action(
        context,
        update.effective_user.id,
        "⚡ Изменение лимита RPM",
        f"ID: #{key_id}, Название: {key_info['name']}, {old_rpm} → {new_rpm} RPM"
    )

    await update.message.reply_text(
        f"✅ <b>Лимит успешно изменён!</b>\n\n"
        f"<b>Ключ:</b> {key_info['name']}\n"
        f"<b>Новый лимит:</b> {new_rpm} RPM",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К деталям ключа", callback_data=f"api_key_details_{key_id}")]
        ])
    )
    return VIEW_API_KEY_DETAILS


async def api_key_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение удаления ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name FROM api_keys WHERE id = $1", key_id
    )

    if not key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    text = f"""⚠️ <b>Подтверждение удаления</b>

Вы действительно хотите удалить ключ?

<b>Префикс:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}

<b>Это действие необратимо!</b>"""

    keyboard = [
        [InlineKeyboardButton("🗑️ Да, удалить", callback_data=f"api_key_delete_exec_{key_id}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_DELETE_API_KEY


async def api_key_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет удаление ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name, owner_user_id FROM api_keys WHERE id = $1", key_id
    )

    if key:
        await db_manager.execute("DELETE FROM api_keys WHERE id = $1", key_id)
        logging.info(f"API-ключ #{key_id} удалён администратором {query.from_user.id}")

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            query.from_user.id,
            "🗑️ Удаление API-ключа",
            f"ID: #{key_id}, Название: {key['name']}, Владелец: {key['owner_user_id']}"
        )

    await query.edit_message_text(
        "✅ Ключ успешно удалён!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К списку ключей", callback_data="api_list_keys")]
        ])
    )
    return MANAGE_API_KEYS


async def api_key_rotate_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение ротации ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name FROM api_keys WHERE id = $1", key_id
    )

    text = f"""🔄 <b>Ротация ключа</b>

Будет создан новый ключ с теми же настройками.
Старый ключ перестанет работать!

<b>Ключ:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}

Продолжить?"""

    keyboard = [
        [InlineKeyboardButton("🔄 Да, ротировать", callback_data=f"api_key_rotate_exec_{key_id}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_ROTATE_API_KEY


async def api_key_rotate_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет ротацию ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    # Получаем старый ключ
    old_key = await db_manager.fetchrow("""
        SELECT name, key_type, owner_user_id, allowed_origins, allowed_ips, requests_per_minute
        FROM api_keys WHERE id = $1
    """, key_id)

    if not old_key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    # Генерируем новый ключ
    full_key, key_hash, key_prefix = _generate_api_key()

    # Обновляем в БД
    await db_manager.execute("""
        UPDATE api_keys 
        SET key_hash = $1, key_prefix = $2, total_requests = 0, last_used_at = NULL, last_ip = NULL
        WHERE id = $3
    """, key_hash, key_prefix, key_id)

    logging.info(f"API-ключ #{key_id} ротирован администратором {query.from_user.id}")

    text = f"""✅ <b>Ключ успешно ротирован!</b>

🔑 <b>Новый ключ (СОХРАНИТЕ):</b>
<code>{full_key}</code>

<b>Название:</b> {old_key['name']}

⚠️ <b>Старый ключ больше не работает!</b>
⚠️ <b>Этот ключ больше не будет показан!</b>"""

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К списку ключей", callback_data="api_list_keys")]
        ])
    )

    # Уведомляем супер-админа (если это не сам супер-админ)
    await _notify_api_action(
        context,
        query.from_user.id,
        "🔄 Ротация API-ключа",
        f"ID: #{key_id}, Название: {old_key['name']}, Владелец: {old_key['owner_user_id']}"
    )

    return MANAGE_API_KEYS


async def api_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает общую статистику API."""
    query = update.callback_query
    await query.answer()

    # Собираем статистику
    total_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys") or 0
    active_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE is_active = true") or 0
    web_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE key_type = 'web'") or 0
    app_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE key_type = 'app'") or 0
    total_requests = await db_manager.fetchval("SELECT COALESCE(SUM(total_requests), 0) FROM api_keys") or 0

    # Топ ключей по использованию
    top_keys = await db_manager.fetch("""
        SELECT key_prefix, name, total_requests 
        FROM api_keys 
        ORDER BY total_requests DESC 
        LIMIT 5
    """)

    text = f"""📈 <b>Статистика REST API</b>

<b>Ключи:</b>
├ Всего: {total_keys}
├ Активных: {active_keys}
├ 🌐 Web: {web_keys}
└ 📱 App: {app_keys}

<b>Запросы:</b>
└ Всего выполнено: {total_requests}

<b>Топ-5 ключей по использованию:</b>
"""

    for i, key in enumerate(top_keys, 1):
        text += f"{i}. <code>{key['key_prefix']}...</code> — {key['total_requests']} запр.\n"
        text += f"   {key['name'][:30]}\n"

    if not top_keys:
        text += "<i>Нет данных</i>\n"

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="api_stats")],
            [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
        ])
    )
    return MANAGE_API_KEYS
