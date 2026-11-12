"""Серверы, обновление базы законов, синхронизация и просмотр прецедентов."""

import asyncio
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import BadRequest

import config
from database.manager import db_manager
from integrations.admin_log import log_admin_action_to_sheet
from knowledge.base_manager import (
    update_knowledge_base_for_server,
    update_all_servers_knowledge_base,
)
from config import (
    SUPER_ADMIN_ID,
    CHOOSING_ACTION,
    MANAGE_SERVERS,
    CHOOSE_SERVER_TO_TOGGLE,
    CHOOSE_SERVER_FOR_UPDATE,
    CHOOSE_SERVER_FOR_FORCE_UPDATE,
    ADMIN_MANAGE_PRECEDENTS,
    ADMIN_VIEWING_PRECEDENTS_LIST,
    ADMIN_VIEWING_PRECEDENT_DETAIL,
    ADMIN_CHOOSE_SERVER_FORCE_PRECEDENTS,
)


async def manage_servers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🚦 Вкл/Выкл серверы", callback_data="toggle_server_list")],
        [InlineKeyboardButton("🔄 Обновить базу знаний (по одному)", callback_data="update_kb_list")],
        [InlineKeyboardButton("💥 Принудительно обновить (по одному)", callback_data="force_update_kb_list")],
        [InlineKeyboardButton("🌐 Обновить ВСЕ серверы", callback_data="update_all_servers")],
        [InlineKeyboardButton("💥 Принудительно обновить ВСЕ", callback_data="force_update_all_servers")],
        [InlineKeyboardButton("⚖️ Синхронизировать прецеденты", callback_data="sync_precedents")],
        [InlineKeyboardButton("💥 Принудительно обновить прецеденты", callback_data="force_sync_precedents_list")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление серверами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_SERVERS


async def choose_server_to_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с группировкой по проектам для включения/выключения."""
    query = update.callback_query
    await query.answer()

    # Получаем все серверы с информацией о проекте
    server_records = await db_manager.fetch("""
        SELECT s.server_id, s.display_name, s.is_disabled, s.project_id, p.display_name as project_name
        FROM servers s
        LEFT JOIN projects p ON s.project_id = p.project_id
        ORDER BY p.display_name, s.display_name;
    """)

    keyboard = []
    current_project = None

    for record in server_records:
        project_name = record['project_name'] or 'Без проекта'
        project_id = record['project_id']

        # Добавляем разделитель для нового проекта
        if project_id != current_project:
            current_project = project_id
            keyboard.append([InlineKeyboardButton(f"📂 {project_name}", callback_data="noop")])

        status_icon = "🅾️" if record['is_disabled'] else "✅"
        button_text = f"{status_icon} {record['display_name']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"toggle_{record['server_id']}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])
    await query.edit_message_text("Выберите сервер для изменения статуса:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_SERVER_TO_TOGGLE


async def toggle_server_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    server_id_to_toggle = query.data.split("_", 1)[1]
    new_status = await db_manager.fetchval(
        "UPDATE servers SET is_disabled = NOT is_disabled WHERE server_id = $1 RETURNING is_disabled;",
        server_id_to_toggle
    )
    status_text = 'ВЫКЛЮЧЕН' if new_status else 'ВКЛЮЧЕН'
    await query.answer(f"Статус изменен на {status_text}")
    log_action = f"SERVER_{'DISABLE' if new_status else 'ENABLE'}"
    logging.info(f"Администратор {query.from_user.id} изменил статус сервера {server_id_to_toggle} на: {status_text}.")
    await log_admin_action_to_sheet(query.from_user.id, log_action, f"Server ID: {server_id_to_toggle}")
    return await choose_server_to_toggle(update, context)


async def choose_server_for_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с группировкой по проектам для обновления БЗ."""
    query = update.callback_query
    await query.answer()

    keyboard = []
    from config import PROJECTS_CONFIG

    for project_id, project_data in PROJECTS_CONFIG.items():
        project_name = project_data.get('display_name', project_id)
        keyboard.append([InlineKeyboardButton(f"📂 {project_name}", callback_data="noop")])

        servers = project_data.get('servers', {})
        for server_id, server_data in servers.items():
            button_text = f"🔄 {server_data['display_name']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"update_{server_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])
    await query.edit_message_text("Выберите сервер, базу знаний которого нужно обновить:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_SERVER_FOR_UPDATE


async def choose_server_for_force_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с группировкой по проектам для принудительного обновления БЗ."""
    query = update.callback_query
    await query.answer()

    keyboard = []
    from config import PROJECTS_CONFIG

    for project_id, project_data in PROJECTS_CONFIG.items():
        project_name = project_data.get('display_name', project_id)
        keyboard.append([InlineKeyboardButton(f"📂 {project_name}", callback_data="noop")])

        servers = project_data.get('servers', {})
        for server_id, server_data in servers.items():
            button_text = f"💥 {server_data['display_name']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"force_update_{server_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])
    await query.edit_message_text("Выберите сервер для ПРИНУДИТЕЛЬНОГО обновления:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_SERVER_FOR_FORCE_UPDATE


async def run_kb_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    server_id_to_update = query.data.split("_", 1)[1]
    server_name = config.SERVERS_CONFIG[server_id_to_update]['display_name']
    await query.answer(f"Запуск обновления для {server_name}...")
    await query.edit_message_text(
        f"⏳ Начинаю полное обновление базы знаний для сервера *{server_name}*... Это может занять несколько минут. Я сообщу о результате.",
        parse_mode="Markdown")
    logging.info(f"Администратор {query.from_user.id} запустил ручное обновление БЗ для сервера {server_id_to_update}.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_UPDATE_SINGLE", f"Server ID: {server_id_to_update}")
    asyncio.create_task(
        update_server_and_notify_admin(context, query.message.chat_id, query.message.message_id, server_id_to_update,
                                       force=False))
    return MANAGE_SERVERS


async def run_kb_force_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    server_id_to_update = query.data.split("_", 2)[2]  # force_update_SERVERID
    server_name = config.SERVERS_CONFIG[server_id_to_update]['display_name']
    await query.answer(f"Запуск ПРИНУДИТЕЛЬНОГО обновления для {server_name}...")
    await query.edit_message_text(
        f"⏳ Начинаю **принудительное** полное обновление базы знаний для сервера *{server_name}*... Это может занять несколько минут. Я сообщу о результате.",
        parse_mode="Markdown")
    logging.info(
        f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ ручное обновление БЗ для сервера {server_id_to_update}.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_FORCE_UPDATE_SINGLE", f"Server ID: {server_id_to_update}")
    asyncio.create_task(
        update_server_and_notify_admin(context, query.message.chat_id, query.message.message_id, server_id_to_update,
                                       force=True))
    return MANAGE_SERVERS


async def update_server_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int,
                                         server_id: str, force: bool = False):
    server_name = config.SERVERS_CONFIG[server_id]['display_name']
    success = await update_knowledge_base_for_server(server_id, force_update=force)
    final_text = (
        f"✅ {'Принудительное о' if force else 'О'}бновление базы знаний для *{server_name}* успешно завершено!" if success
        else f"❌ Ошибка при обновлении *{server_name}*. Проверьте лог-файлы для подробностей.")
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=final_text,
                                            parse_mode="Markdown")
    except Exception as e:
        logging.warning(f"Не удалось отредактировать сообщение с результатом обновления БЗ для админа {chat_id}.",
                        exc_info=e)


async def bypass_kb_protection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Снять защиту и запустить повторно' из уведомления о срабатывании ЗАЩИТЫ БЗ."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != SUPER_ADMIN_ID:
        await query.answer("Доступ запрещен.", show_alert=True)
        return

    # callback_data = "bypass_prot_{server_id}"
    server_id = query.data.split("_", 2)[2]
    server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    await query.answer(f"Защита снята. Запускаю обновление для {server_name}...")
    await query.edit_message_text(
        f"🔓 Защита снята для <b>{server_name}</b>.\n"
        f"⏳ Запускаю обновление повторно с обходом защиты...",
        parse_mode="HTML"
    )

    logging.info(f"Супер-админ {user_id} снял защиту БЗ для сервера {server_id}. Запуск повторного обновления.")
    await log_admin_action_to_sheet(user_id, "KB_BYPASS_PROTECTION", f"Server ID: {server_id}")
    asyncio.create_task(_bypass_protection_and_notify(context, user_id, server_id, server_name))


async def _bypass_protection_and_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, server_id: str,
                                         server_name: str):
    """Фоновая задача: запускает обновление БЗ с обходом защиты и уведомляет супер-админа о результате."""
    success = await update_knowledge_base_for_server(server_id, force_update=False, bypass_protection=True)
    if success:
        msg = f"✅ Обновление <b>{server_name}</b> с обходом защиты успешно завершено!"
    else:
        msg = f"❌ Ошибка при обновлении <b>{server_name}</b> даже с обходом защиты. Проверьте логи."
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить результат обхода защиты супер-админу {chat_id}", exc_info=e)


async def run_kb_update_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Запускаю процесс...")
    await query.edit_message_text(
        "⏳ Начинаю полное обновление баз знаний для **ВСЕХ** серверов... Это может занять **очень много времени**. По завершении я пришлю отчет.")
    logging.info(f"Администратор {query.from_user.id} запустил ручное обновление БЗ для ВСЕХ серверов.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_UPDATE_ALL_MANUAL")
    asyncio.create_task(update_all_and_notify_admin(context, query.message.chat_id, force=False))
    return MANAGE_SERVERS


async def run_kb_force_update_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик для кнопки принудительного обновления всех серверов."""
    query = update.callback_query
    await query.answer("Запускаю принудительный процесс...")
    await query.edit_message_text(
        "⏳ Начинаю **принудительное** полное обновление баз знаний для **ВСЕХ** серверов... Это может занять **очень много времени**. По завершении я пришлю отчет.")
    logging.info(f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ ручное обновление БЗ для ВСЕХ серверов.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_FORCE_UPDATE_ALL_MANUAL")
    asyncio.create_task(update_all_and_notify_admin(context, query.message.chat_id, force=True))
    return MANAGE_SERVERS


async def update_all_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, force: bool = False):
    successful_updates, failed_servers = await update_all_servers_knowledge_base(force_update=force)
    total_servers = len(config.SERVERS_CONFIG)
    update_type = "Принудительное глобальное" if force else "Глобальное"
    final_text = (f"✅ **{update_type} обновление завершено!**\n\n"
                  f"- Успешно: {successful_updates}/{total_servers}\n")
    if failed_servers:
        final_text += f"- Ошибки на серверах: {', '.join(failed_servers)}\n"
    await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode="Markdown")
    if 'scheduler_callback' in context.application.bot_data:
        await context.application.bot_data['scheduler_callback']()


# ========== СИНХРОНИЗАЦИЯ ПРЕЦЕДЕНТОВ ==========

async def sync_precedents_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик для кнопки синхронизации прецедентов."""
    query = update.callback_query
    await query.answer("Запускаю синхронизацию прецедентов...")
    await query.edit_message_text(
        "⚖️ Начинаю синхронизацию судебных прецедентов...\n\n"
        "Будут обработаны только серверы с настроенной системой прецедентов.\n"
        "По завершении я пришлю отчет.")
    logging.info(f"Администратор {query.from_user.id} запустил синхронизацию прецедентов.")
    await log_admin_action_to_sheet(query.from_user.id, "PRECEDENTS_SYNC")
    asyncio.create_task(sync_precedents_and_notify_admin(context, query.message.chat_id))
    return MANAGE_SERVERS


async def sync_precedents_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Асинхронная задача для синхронизации прецедентов с уведомлением админа."""
    from knowledge.base_manager import sync_precedents_for_all_servers

    genai_client = context.bot_data.get('genai_client')
    successful, failed = await sync_precedents_for_all_servers(genai_client, force_update=False)

    final_text = f"⚖️ **Синхронизация прецедентов завершена!**\n\n"
    final_text += f"- Серверов обработано успешно: {successful}\n"
    if failed:
        final_text += f"- Ошибки на серверах: {', '.join(failed)}\n"
    else:
        final_text += "- Ошибок не обнаружено\n"

    await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode="Markdown")


async def choose_server_for_force_precedents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с прецедентами для принудительного обновления."""
    query = update.callback_query
    await query.answer()

    keyboard = []
    servers_with_precedents = []

    for server_id, server_config in config.SERVERS_CONFIG.items():
        if server_config.get('precedents_registry_url'):
            servers_with_precedents.append({
                'id': server_id,
                'name': server_config['display_name']
            })

    if not servers_with_precedents:
        await query.edit_message_text(
            "❌ Нет серверов с настроенной системой прецедентов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")]])
        )
        return MANAGE_SERVERS

    for server in servers_with_precedents:
        btn_text = f"💥 {server['name']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"force_prec_{server['id']}")])

    keyboard.append([InlineKeyboardButton("💥 ВСЕ серверы", callback_data="force_prec_all")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])

    await query.edit_message_text(
        "⚖️ **Принудительное обновление прецедентов**\n\n"
        "⚠️ Это обновит ВСЕ прецеденты, игнорируя проверку изменений изображений.\n"
        "Может занять много времени и API-запросов.\n\n"
        "Выберите сервер:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_CHOOSE_SERVER_FORCE_PRECEDENTS


async def run_force_precedents_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает принудительное обновление прецедентов для выбранного сервера."""
    query = update.callback_query

    # Определяем, один сервер или все
    callback_data = query.data
    if callback_data == "force_prec_all":
        await query.answer("Запускаю принудительное обновление для ВСЕХ серверов...")
        await query.edit_message_text(
            "⚖️💥 Начинаю **ПРИНУДИТЕЛЬНОЕ** обновление прецедентов для **ВСЕХ** серверов...\n\n"
            "⚠️ Это может занять очень много времени.\n"
            "По завершении я пришлю отчет."
        )
        logging.info(f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ обновление прецедентов для ВСЕХ серверов.")
        await log_admin_action_to_sheet(query.from_user.id, "PRECEDENTS_FORCE_SYNC_ALL")
        asyncio.create_task(force_sync_precedents_and_notify_admin(context, query.message.chat_id, server_id=None))
    else:
        server_id = callback_data.replace("force_prec_", "")
        server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

        await query.answer(f"Запускаю для {server_name}...")
        await query.edit_message_text(
            f"⚖️💥 Начинаю **ПРИНУДИТЕЛЬНОЕ** обновление прецедентов для сервера **{server_name}**...\n\n"
            f"⚠️ Это может занять несколько минут.\n"
            f"По завершении я пришлю отчет."
        )
        logging.info(f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ обновление прецедентов для сервера {server_id}.")
        await log_admin_action_to_sheet(query.from_user.id, "PRECEDENTS_FORCE_SYNC_SINGLE", f"Server: {server_id}")
        asyncio.create_task(force_sync_precedents_and_notify_admin(context, query.message.chat_id, server_id=server_id))

    return MANAGE_SERVERS


async def force_sync_precedents_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, server_id: str = None):
    """Асинхронная задача для ПРИНУДИТЕЛЬНОЙ синхронизации прецедентов."""
    from knowledge.precedents import sync_precedents_for_server

    genai_client = context.bot_data.get('genai_client')

    if server_id:
        # Обновляем один сервер
        try:
            updated_count = await sync_precedents_for_server(server_id, genai_client, force_update=True)
            server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)
            final_text = (
                f"⚖️💥 **Принудительное обновление прецедентов завершено!**\n\n"
                f"**Сервер:** {server_name}\n"
                f"**Обновлено прецедентов:** {updated_count}"
            )
        except Exception as e:
            logging.error(f"Ошибка принудительного обновления прецедентов для {server_id}", exc_info=e)
            final_text = f"❌ Ошибка при обновлении прецедентов для сервера {server_id}: {str(e)[:200]}"
    else:
        # Обновляем все серверы с прецедентами
        from knowledge.base_manager import sync_precedents_for_all_servers
        successful, failed = await sync_precedents_for_all_servers(genai_client, force_update=True)

        final_text = f"⚖️💥 **Принудительное обновление прецедентов завершено!**\n\n"
        final_text += f"- Серверов обработано успешно: {successful}\n"
        if failed:
            final_text += f"- Ошибки на серверах: {', '.join(failed)}\n"
        else:
            final_text += "- Ошибок не обнаружено\n"

    await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode="Markdown")


# ========== УПРАВЛЕНИЕ ПРЕЦЕДЕНТАМИ ==========

async def manage_precedents_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню управления прецедентами."""
    query = update.callback_query
    await query.answer()

    # Получаем статистику по серверам с прецедентами
    servers_with_precedents = []
    for server_id, server_config in config.SERVERS_CONFIG.items():
        if server_config.get('precedents_registry_url'):
            count = await db_manager.fetchval(
                "SELECT COUNT(*) FROM precedents WHERE server_id = $1 AND is_active = TRUE",
                server_id
            ) or 0
            servers_with_precedents.append({
                'id': server_id,
                'name': server_config['display_name'],
                'count': count
            })

    if not servers_with_precedents:
        await query.edit_message_text(
            "⚖️ **Прецеденты**\n\n"
            "❌ Нет серверов с настроенной системой прецедентов.\n\n"
            "Для настройки добавьте `precedents_registry_url` в конфигурацию сервера.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]])
        )
        return CHOOSING_ACTION

    keyboard = []
    for server in servers_with_precedents:
        btn_text = f"📂 {server['name']} ({server['count']} прецедентов)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"prec_server_{server['id']}")])

    keyboard.append([InlineKeyboardButton("🔄 Синхронизировать все", callback_data="sync_precedents")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])

    total_count = sum(s['count'] for s in servers_with_precedents)
    await query.edit_message_text(
        f"⚖️ **Просмотр прецедентов**\n\n"
        f"Всего активных прецедентов: {total_count}\n"
        f"Серверов с прецедентами: {len(servers_with_precedents)}\n\n"
        f"Выберите сервер для просмотра:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_MANAGE_PRECEDENTS


async def view_server_precedents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список прецедентов для выбранного сервера."""
    query = update.callback_query
    await query.answer()

    # Если callback_data начинается с prec_server_, то это новый выбор сервера
    # Иначе используем сохранённый server_id из context.user_data
    if query.data.startswith("prec_server_"):
        server_id = query.data.replace("prec_server_", "")
        context.user_data['precedents_server_id'] = server_id
        context.user_data['precedents_page'] = 0  # Сбрасываем страницу при выборе нового сервера
    else:
        server_id = context.user_data.get('precedents_server_id')
        if not server_id:
            # Если нет server_id, возвращаемся к списку серверов
            return await manage_precedents_menu(update, context)

    # Получаем страницу (по умолчанию 0)
    page = context.user_data.get('precedents_page', 0)
    items_per_page = 8

    # Получаем прецеденты
    precedents = await db_manager.fetch(
        """SELECT id, title, precedent_type, is_active, summary, last_updated
           FROM precedents 
           WHERE server_id = $1 
           ORDER BY is_active DESC, last_updated DESC
           LIMIT $2 OFFSET $3""",
        server_id, items_per_page + 1, page * items_per_page  # +1 для проверки наличия следующей страницы
    )

    total_count = await db_manager.fetchval(
        "SELECT COUNT(*) FROM precedents WHERE server_id = $1", server_id
    ) or 0

    server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    if not precedents:
        await query.edit_message_text(
            f"⚖️ **{server_name}**\n\n"
            f"Прецеденты не найдены.\n\n"
            f"Запустите синхронизацию для загрузки прецедентов.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Синхронизировать", callback_data="sync_precedents")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_precedents")]
            ])
        )
        return ADMIN_MANAGE_PRECEDENTS

    has_next_page = len(precedents) > items_per_page
    precedents = precedents[:items_per_page]  # Убираем лишний элемент

    keyboard = []
    for prec in precedents:
        # Иконка только для статуса
        status_icon = "" if prec['is_active'] else "❌ "

        # Обрезаем заголовок если слишком длинный
        title = prec['title'][:40] + "..." if len(prec['title']) > 40 else prec['title']

        btn_text = f"{status_icon}📜 {title}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"prec_view_{prec['id']}")])

    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"prec_page_{page - 1}"))
    if has_next_page:
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"prec_page_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("◀️ К списку серверов", callback_data="manage_precedents")])

    active_count = await db_manager.fetchval(
        "SELECT COUNT(*) FROM precedents WHERE server_id = $1 AND is_active = TRUE", server_id
    ) or 0

    await query.edit_message_text(
        f"⚖️ **{server_name}**\n\n"
        f"Всего прецедентов: {total_count}\n"
        f"Активных: {active_count}\n"
        f"Страница: {page + 1}\n\n"
        f"❌ = Неактивен",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_VIEWING_PRECEDENTS_LIST


async def precedents_change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключение страницы списка прецедентов."""
    query = update.callback_query
    page = int(query.data.replace("prec_page_", ""))
    context.user_data['precedents_page'] = page
    return await view_server_precedents(update, context)


async def view_precedent_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детальную информацию о прецеденте."""
    query = update.callback_query
    await query.answer()

    precedent_id = int(query.data.replace("prec_view_", ""))
    context.user_data['current_precedent_id'] = precedent_id

    prec = await db_manager.fetchrow(
        """SELECT id, server_id, source_url, title, summary, precedent_type, 
                  is_active, last_updated, full_content, image_urls
           FROM precedents WHERE id = $1""",
        precedent_id
    )

    if not prec:
        await query.edit_message_text(
            "❌ Прецедент не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="manage_precedents")]])
        )
        return ADMIN_MANAGE_PRECEDENTS

    server_name = config.SERVERS_CONFIG.get(prec['server_id'], {}).get('display_name', prec['server_id'])
    status = "✅ Активен" if prec['is_active'] else "❌ Неактивен"

    # Форматируем дату
    last_updated = prec['last_updated'].strftime("%d.%m.%Y %H:%M") if prec['last_updated'] else "Неизвестно"

    # Количество изображений
    image_urls = prec['image_urls'] or []
    images_info = f"🖼 Изображений: {len(image_urls)}" if image_urls else "🖼 Изображений: нет"

    # Краткое описание (summary)
    summary = prec['summary'] or "Краткое описание не сгенерировано"
    if len(summary) > 500:
        summary = summary[:500] + "..."

    text = (
        f"⚖️ **{prec['title']}**\n\n"
        f"**Сервер:** {server_name}\n"
        f"**Статус:** {status}\n"
        f"**{images_info}**\n"
        f"**Обновлено:** {last_updated}\n\n"
        f"**📝 Краткое описание:**\n{summary}\n\n"
        f"🔗 [Открыть на форуме]({prec['source_url']})"
    )

    # Проверяем, есть ли полный контент
    has_full_content = bool(prec['full_content'] and len(prec['full_content']) > 0)
    content_length = len(prec['full_content']) if prec['full_content'] else 0

    keyboard = []
    if has_full_content:
        keyboard.append([InlineKeyboardButton(f"📄 Полный текст ({content_length} симв.)", callback_data=f"prec_full_{precedent_id}")])

    # Кнопка для просмотра изображений
    if image_urls:
        keyboard.append([InlineKeyboardButton(f"🖼 Показать изображения ({len(image_urls)})", callback_data=f"prec_images_{precedent_id}")])

    keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data=f"prec_server_{prec['server_id']}")])
    keyboard.append([InlineKeyboardButton("◀️ К серверам", callback_data="manage_precedents")])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )
    return ADMIN_VIEWING_PRECEDENT_DETAIL


async def view_precedent_full_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает полный текст прецедента (может быть разбит на несколько сообщений)."""
    query = update.callback_query
    await query.answer("Загружаю полный текст...")

    precedent_id = int(query.data.replace("prec_full_", ""))

    prec = await db_manager.fetchrow(
        "SELECT id, server_id, title, full_content FROM precedents WHERE id = $1",
        precedent_id
    )

    if not prec or not prec['full_content']:
        await query.edit_message_text(
            "❌ Полный текст не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"prec_view_{precedent_id}")]])
        )
        return ADMIN_VIEWING_PRECEDENT_DETAIL

    full_content = prec['full_content']
    title = prec['title']

    # Разбиваем на части по 4000 символов (лимит Telegram ~4096)
    max_length = 4000
    parts = []
    while full_content:
        if len(full_content) <= max_length:
            parts.append(full_content)
            break
        # Ищем последний перенос строки в пределах лимита
        split_pos = full_content.rfind('\n', 0, max_length)
        if split_pos == -1:
            split_pos = max_length
        parts.append(full_content[:split_pos])
        full_content = full_content[split_pos:].lstrip()

    # Отправляем первую часть как редактирование текущего сообщения
    header = f"📄 **{title}**\n\n"
    first_part = header + f"```\n{parts[0]}\n```"

    # Если текст слишком длинный даже для первой части
    if len(first_part) > 4096:
        first_part = header + f"```\n{parts[0][:3800]}\n```\n\n_(продолжение в следующем сообщении)_"

    keyboard = [[InlineKeyboardButton("◀️ Назад к прецеденту", callback_data=f"prec_view_{precedent_id}")]]

    try:
        await query.edit_message_text(
            first_part,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except BadRequest:
        # Если не удалось с Markdown, пробуем без форматирования
        await query.edit_message_text(
            f"📄 {title}\n\n{parts[0][:3900]}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Отправляем остальные части как новые сообщения
    chat_id = update.effective_chat.id
    for i, part in enumerate(parts[1:], start=2):
        try:
            await asyncio.sleep(0.5)  # Небольшая задержка между сообщениями
            part_text = f"_(Часть {i}/{len(parts)})_\n\n```\n{part}\n```"
            if len(part_text) > 4096:
                part_text = f"_(Часть {i}/{len(parts)})_\n\n{part[:3900]}"
            await context.bot.send_message(chat_id, part_text, parse_mode="Markdown")
        except Exception as e:
            logging.warning(f"Ошибка отправки части {i} прецедента: {e}")
            await context.bot.send_message(chat_id, f"_(Часть {i}/{len(parts)})_\n\n{part[:3900]}")

    return ADMIN_VIEWING_PRECEDENT_DETAIL


async def view_precedent_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список URL изображений прецедента."""
    query = update.callback_query
    await query.answer()

    precedent_id = int(query.data.replace("prec_images_", ""))

    prec = await db_manager.fetchrow(
        "SELECT id, server_id, title, image_urls FROM precedents WHERE id = $1",
        precedent_id
    )

    if not prec or not prec['image_urls']:
        await query.edit_message_text(
            "❌ Изображения не найдены.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"prec_view_{precedent_id}")]])
        )
        return ADMIN_VIEWING_PRECEDENT_DETAIL

    image_urls = prec['image_urls']
    title = prec['title']

    # Формируем текст со списком URL
    text = f"🖼 **Изображения прецедента**\n\n"
    text += f"📌 {title}\n\n"
    text += f"Всего изображений: {len(image_urls)}\n\n"

    for i, url in enumerate(image_urls, 1):
        # Сокращаем URL если слишком длинный
        short_url = url[:60] + "..." if len(url) > 60 else url
        text += f"{i}. `{short_url}`\n"

    keyboard = [[InlineKeyboardButton("◀️ Назад к прецеденту", callback_data=f"prec_view_{precedent_id}")]]

    try:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
    except BadRequest:
        # Если слишком длинный текст
        await query.edit_message_text(
            f"🖼 Изображения прецедента: {title}\n\nВсего: {len(image_urls)}\n\n(Список слишком длинный для отображения)",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return ADMIN_VIEWING_PRECEDENT_DETAIL
