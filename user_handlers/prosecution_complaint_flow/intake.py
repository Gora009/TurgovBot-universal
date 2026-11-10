"""Старт разбора: выбор проекта и сервера, ввод ссылки и анкеты, запуск анализа."""

import logging
import re
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

import config
from config import (
    PCH_CHOOSING_PROJECT,
    PCH_CHOOSING_SERVER,
    PCH_AWAITING_URL,
    PCH_AWAITING_FORM,
    PCH_USER_REVIEW,
    PCH_NAVIGATE,
    PROJECTS_CONFIG,
    SERVERS_CONFIG,
)
from database.manager import db_manager
from common.utils import (
    sanitize_telegram_html,
    safe_answer_callback_query,
    get_enabled_servers,
    get_enabled_projects,
    send_project_selection_menu,
)
from knowledge.remote_parser import remote_parse_topic, check_remote_parser_available
from ..core import check_and_update_usage

from .jobs import schedule_pch_session_closure
from .pipeline import (
    get_available_documents_for_stage,
    get_relevant_laws_for_complaint,
    parse_complaint_from_url,
    run_intake_analysis,
)
from .session import (
    ProsecutionComplaintManager,
    get_form_template,
    safe_edit_message_text,
    save_form_template,
    send_debug_document,
)
from .ui import build_stage_keyboard, format_session_status, format_stage_message


# Конец Части 2


# === ЧАСТЬ 3: ОБРАБОТЧИКИ СОСТОЯНИЙ ===

# === ENTRY POINTS ===

async def start_prosecution_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало разбора жалобы - показывает меню с историей."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Проверка доступа: функция доступна тестерам и админам
    user_record = await db_manager.fetchrow(
        "SELECT is_admin, is_tester FROM users WHERE user_id = $1", user_id
    )
    is_admin = (user_record and user_record['is_admin']) or user_id == config.SUPER_ADMIN_ID
    is_tester = user_record and user_record.get('is_tester', False)

    if not is_admin and not is_tester:
        await safe_edit_message_text(
            query,
            "🔒 <b>Функция в бета-тестировании</b>\n\n"
            "Разбор жалоб пока доступен только тестировщикам.\n\n"
            "Следите за обновлениями — скоро функция будет доступна всем!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Получаем историю жалоб
    sessions = await ProsecutionComplaintManager.get_user_sessions(user_id, limit=10)

    # Проверяем, есть ли активная сессия
    active_session = None
    for session in sessions:
        if session['status'] == 'active':
            active_session = session
            break

    # Формируем сообщение
    text = "📋 <b>Разбор жалоб в прокуратуру</b>\n\n"

    if sessions:
        text += "<b>📜 История жалоб:</b>\n"
        for session in sessions[:5]:
            status_info = format_session_status(session)
            date_str = session['created_at'].strftime('%d.%m.%Y %H:%M') if session.get('created_at') else 'N/A'
            complaint_num = session.get('complaint_number', 'Без номера')
            text += f"📋 <b>{complaint_num}</b> ({date_str})\n   {status_info}\n"
        text += "\n"
    else:
        text += "<i>У вас пока нет обработанных жалоб.</i>\n\n"

    # Формируем кнопки
    keyboard = []

    # Кнопка продолжения активной сессии
    if active_session:
        keyboard.append([InlineKeyboardButton(
            f"▶️ Продолжить: {active_session.get('complaint_number', 'N/A')}",
            callback_data=f"pch_continue_{active_session['id']}"
        )])

    # Кнопка создания новой жалобы
    keyboard.append([InlineKeyboardButton("➕ Новая жалоба", callback_data="pch_create_new")])

    # Кнопка истории
    if sessions:
        keyboard.append([InlineKeyboardButton("📜 Вся история", callback_data="pch_history")])

    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PCH_NAVIGATE


async def create_new_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает создание новой жалобы (показывает выбор проекта)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Проверяем лимиты
    allowed, current_usage = await check_and_update_usage(user_id, 'complaint_processing')
    if not allowed:
        await safe_edit_message_text(
            query,
            "❌ <b>Лимит исчерпан</b>\n\n"
            "Вы использовали все доступные запросы на разбор жалоб.\n"
            "Лимит обновится через некоторое время.\n\n"
            "💎 Подпишитесь на канал для получения бонусных запросов!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Показываем выбор проекта
    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор
    if len(enabled_projects) == 1:
        context.user_data['pch_project_id'] = enabled_projects[0]
        return await _show_pch_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="pch_project_",
        text="📋 <b>Разбор жалобы</b>\n\n🌐 <b>Выберите проект:</b>",
        back_callback="start_prosecution_complaint"
    )
    return PCH_CHOOSING_PROJECT


async def _show_pch_servers(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str) -> int:
    """Показывает список серверов для выбранного проекта."""
    query = update.callback_query

    enabled_servers = await get_enabled_servers(project_id)
    if not enabled_servers:
        if query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="На данный момент нет доступных серверов для этого проекта."
            )
        else:
            await safe_edit_message_text(
                query, "На данный момент нет доступных серверов для этого проекта."
            )
        return ConversationHandler.END

    project_data = PROJECTS_CONFIG.get(project_id, {})
    project_name = project_data.get('display_name', project_id)

    keyboard = [
        [InlineKeyboardButton(
            SERVERS_CONFIG[server_id]['display_name'],
            callback_data=f"pch_server_{server_id}"
        )]
        for server_id in enabled_servers
    ]

    # Кнопка "Назад" зависит от количества проектов
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        keyboard.append([InlineKeyboardButton(
            "◀️ К выбору проекта", callback_data="start_prosecution_complaint"
        )])
    else:
        keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    text = (
        f"📋 <b>Разбор жалобы</b>\n"
        f"🎮 <b>{project_name}</b>\n\n"
        "Выберите сервер, законы которого будут использоваться:"
    )
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Проверяем, является ли сообщение фото
    if query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    else:
        await safe_edit_message_text(
            query,
            text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    return PCH_CHOOSING_SERVER


async def select_pch_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает серверы."""
    query = update.callback_query
    await query.answer()

    project_id = query.data.split("pch_project_", 1)[1]
    context.user_data['pch_project_id'] = project_id

    return await _show_pch_servers(update, context, project_id)


async def select_pch_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный сервер и переходит к вводу URL жалобы."""
    query = update.callback_query
    await query.answer()

    server_id = query.data.split("pch_server_", 1)[1]
    context.user_data['pch_server_id'] = server_id

    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)
    user_id = update.effective_user.id

    # Проверяем, есть ли кэшированная форма подачи
    cached_form = await get_form_template(user_id, server_id)

    text = (
        f"📋 <b>Разбор жалобы</b>\n"
        f"🖥️ Сервер: <b>{server_name}</b>\n\n"
        "📎 <b>Отправьте ссылку на тему с жалобой на форуме</b>\n\n"
        "<i>Пример: https://forum.gta5rp.com/threads/12345/, https://forum.rmrp.ru/threads/12345/ "
        "или https://forum.russia.online/threads/12345/</i>"
    )

    if cached_form:
        text += "\n\n✅ <i>Форма подачи жалобы для этого сервера уже сохранена.</i>"
    else:
        text += (
            "\n\n💡 <i>После ввода ссылки я попрошу вас предоставить форму подачи "
            "жалобы для скрининга соответствия.</i>"
        )

    # Формируем клавиатуру
    keyboard = []

    # Кнопка замены формы (если форма уже есть)
    if cached_form:
        keyboard.append([InlineKeyboardButton(
            "🔄 Заменить форму подачи",
            callback_data="pch_replace_form"
        )])

    keyboard.append([InlineKeyboardButton(
        "◀️ Назад",
        callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}"
    )])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_AWAITING_URL


async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод URL жалобы."""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    server_id = context.user_data.get('pch_server_id')

    # Валидация URL
    if not re.match(r'^https?://', url):
        await update.message.reply_text(
            "❌ Неверный формат ссылки.\n"
            "Отправьте полную ссылку на тему форума, начинающуюся с http:// или https://",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "◀️ К выбору сервера",
                    callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}"
                )]
            ])
        )
        return PCH_AWAITING_URL

    context.user_data['pch_complaint_url'] = url

    # Проверяем, есть ли кэшированная форма подачи
    cached_form = await get_form_template(user_id, server_id)

    if cached_form:
        # Форма уже есть - переходим сразу к парсингу
        context.user_data['pch_form_template'] = cached_form
        return await start_parsing_complaint(update, context)
    else:
        # Запрашиваем форму подачи
        await update.message.reply_text(
            "📝 <b>Форма подачи жалобы</b>\n\n"
            "Для корректного скрининга жалобы, пожалуйста, отправьте образец формы "
            "подачи жалобы в прокуратуру, принятый на вашем сервере.\n\n"
            "<b>Варианты:</b>\n"
            "• 🔗 <b>Ссылка на форум</b> — бот автоматически спарсит форму\n"
            "• 📋 <b>Текст формы</b> — скопируйте и отправьте текст шаблона\n\n"
            "<i>Форма будет сохранена для последующих жалоб на этом сервере.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_AWAITING_FORM


async def handle_form_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод формы подачи жалобы (текст или ссылка)."""
    user_id = update.effective_user.id
    input_text = update.message.text.strip()
    server_id = context.user_data.get('pch_server_id')

    # Проверяем, является ли ввод ссылкой
    if re.match(r'^https?://', input_text):
        # Это ссылка - парсим форму с форума
        progress_msg = await update.message.reply_text(
            "⏳ <b>Загружаю форму с форума...</b>",
            parse_mode='HTML'
        )

        # Проверяем доступность парсера
        parser_available = await check_remote_parser_available()
        if not parser_available:
            await context.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text="❌ Сервис парсинга временно недоступен. Пожалуйста, скопируйте текст формы вручную.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
                ])
            )
            return PCH_AWAITING_FORM

        try:
            # Парсим тему с форумом (content_cleanup=False: форма может лежать в цитате)
            result = await remote_parse_topic(input_text, parse_only_author=True, content_cleanup=False)

            if not result or not result.get('content'):
                await context.bot.edit_message_text(
                    chat_id=progress_msg.chat.id,
                    message_id=progress_msg.message_id,
                    text="❌ Не удалось загрузить форму с форума. Проверьте ссылку или скопируйте текст вручную.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
                    ])
                )
                return PCH_AWAITING_FORM

            form_content = result.get('content', '')
            form_title = result.get('title', 'Форма подачи')

            # Сохраняем форму в кэш
            await save_form_template(user_id, server_id, form_content)
            context.user_data['pch_form_template'] = form_content

            # === DEBUG: Отправляем файл с распарсенной формой ===
            debug_content = f"""=== ДЕБАГ: РАСПАРСЕННАЯ ФОРМА ПОДАЧИ ===
Дата парсинга: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
URL: {input_text}
Сервер: {server_id}
Заголовок: {form_title}
Длина контента: {len(form_content)} символов

{'='*50}
СОДЕРЖИМОЕ ФОРМЫ:
{'='*50}

{form_content}

{'='*50}
КОНЕЦ ФОРМЫ
{'='*50}
"""
            await send_debug_document(
                context=context,
                chat_id=update.effective_chat.id,
                content=debug_content,
                filename=f"form_parsed_{server_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                caption=f"🔍 DEBUG: Распарсенная форма подачи\n📋 {form_title}"
            )
            # === END DEBUG ===

            # Проверяем, заменяем ли мы форму или это первичный ввод
            if context.user_data.get('pch_replacing_form'):
                context.user_data['pch_replacing_form'] = False
                server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

                await context.bot.edit_message_text(
                    chat_id=progress_msg.chat.id,
                    message_id=progress_msg.message_id,
                    text=f"✅ <b>Форма обновлена!</b>\n\n"
                         f"📋 <b>{form_title}</b>\n"
                         f"<i>Длина: {len(form_content)} символов</i>\n\n"
                         f"🖥️ Сервер: <b>{server_name}</b>\n\n"
                         "📎 <b>Теперь отправьте ссылку на тему с жалобой на форуме</b>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Заменить форму подачи", callback_data="pch_replace_form")],
                        [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}")]
                    ])
                )
                return PCH_AWAITING_URL

            await context.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text=f"✅ <b>Форма загружена и сохранена!</b>\n\n"
                     f"📋 <b>{form_title}</b>\n\n"
                     f"<i>Длина: {len(form_content)} символов</i>",
                parse_mode='HTML'
            )

            return await start_parsing_complaint(update, context)

        except Exception as e:
            logging.error(f"[PCH] Ошибка парсинга формы: {e}")
            await context.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text=f"❌ Ошибка при загрузке формы: {str(e)[:100]}\n\n"
                     "Пожалуйста, скопируйте текст формы вручную.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
                ])
            )
            return PCH_AWAITING_FORM

    # Это текст формы
    form_content = input_text

    if len(form_content) < 50:
        await update.message.reply_text(
            "⚠️ Форма слишком короткая. Пожалуйста, предоставьте более полный шаблон.\n\n"
            "Если хотите продолжить без формы, нажмите «Пропустить».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_AWAITING_FORM

    # Сохраняем форму в кэш
    await save_form_template(user_id, server_id, form_content)
    context.user_data['pch_form_template'] = form_content

    # Проверяем, заменяем ли мы форму или это первичный ввод
    if context.user_data.get('pch_replacing_form'):
        context.user_data['pch_replacing_form'] = False
        server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

        await update.message.reply_text(
            f"✅ <b>Форма обновлена!</b>\n\n"
            f"📋 <b>Разбор жалобы</b>\n"
            f"🖥️ Сервер: <b>{server_name}</b>\n\n"
            "📎 <b>Теперь отправьте ссылку на тему с жалобой на форуме</b>\n\n"
            "<i>Пример: https://forum.gta5rp.com/threads/12345/, https://forum.rmrp.ru/threads/12345/ "
            "или https://forum.russia.online/threads/12345/</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Заменить форму подачи", callback_data="pch_replace_form")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}")]
            ])
        )
        return PCH_AWAITING_URL

    await update.message.reply_text("✅ Форма сохранена!")

    return await start_parsing_complaint(update, context)


async def skip_form_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пропускает ввод формы подачи."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    context.user_data['pch_form_template'] = None
    return await start_parsing_complaint(update, context, from_callback=True)


async def replace_form_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Позволяет заменить сохранённую форму подачи на новую."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    server_id = context.user_data.get('pch_server_id')
    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    await safe_edit_message_text(
        query,
        f"🔄 <b>Замена формы подачи</b>\n"
        f"🖥️ Сервер: <b>{server_name}</b>\n\n"
        "Отправьте новую форму подачи жалобы:\n\n"
        "<b>Варианты:</b>\n"
        "• 🔗 <b>Ссылка на форум</b> — бот автоматически спарсит форму\n"
        "• 📋 <b>Текст формы</b> — скопируйте и отправьте текст шаблона\n\n"
        "<i>Новая форма заменит ранее сохранённую.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_server_{server_id}")]
        ])
    )

    # Устанавливаем флаг что мы заменяем форму (чтобы после сохранения вернуться к вводу URL)
    context.user_data['pch_replacing_form'] = True
    return PCH_AWAITING_FORM


async def start_parsing_complaint(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    from_callback: bool = False
) -> int:
    """Начинает парсинг жалобы и первичный анализ."""
    user_id = update.effective_user.id
    url = context.user_data.get('pch_complaint_url')
    server_id = context.user_data.get('pch_server_id')
    form_template = context.user_data.get('pch_form_template')

    # Отправляем сообщение о начале парсинга
    if from_callback:
        query = update.callback_query
        progress_msg = await safe_edit_message_text(
            query,
            "⏳ <b>Загружаю жалобу с форума...</b>\n\n"
            "Это может занять несколько секунд.",
            parse_mode='HTML'
        )
    else:
        progress_msg = await update.message.reply_text(
            "⏳ <b>Загружаю жалобу с форума...</b>\n\n"
            "Это может занять несколько секунд.",
            parse_mode='HTML'
        )

    # Парсим жалобу
    success, parsed_complaint, error = await parse_complaint_from_url(url)

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка загрузки жалобы</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data="pch_create_new")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_NAVIGATE

    # Создаём сессию
    manager = await ProsecutionComplaintManager.create_session(
        user_id=user_id,
        server_id=server_id,
        complaint_url=url
    )
    session_id = manager.session_id
    context.user_data['pch_session_id'] = session_id

    # Сохраняем распарсенные данные
    await manager.update_session(parsed_complaint=parsed_complaint)

    # Планируем автозакрытие
    schedule_pch_session_closure(context, session_id, user_id)

    # Обновляем сообщение
    await context.bot.edit_message_text(
        chat_id=progress_msg.chat.id,
        message_id=progress_msg.message_id,
        text="⏳ <b>Анализирую жалобу...</b>\n\n"
             f"📋 <b>{parsed_complaint.get('title', 'Без названия')}</b>\n\n"
             "Провожу первичный анализ и скрининг формы подачи...",
        parse_mode='HTML'
    )

    # Получаем релевантные законы через RAG-систему
    complaint_text = parsed_complaint.get('content', '')
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='intake')

    # Запускаем Phase A: Intake Analysis
    success, content, error = await run_intake_analysis(
        session_id=session_id,
        parsed_complaint=parsed_complaint,
        form_template=form_template,
        relevant_laws=relevant_laws
    )

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка анализа</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"pch_retry_intake_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_NAVIGATE

    # Обновляем индекс этапа
    await manager.update_session(current_stage_index=0)
    context.user_data['pch_current_stage_index'] = 0

    # Получаем данные сессии для отображения
    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    # Форматируем и показываем результат Phase A
    available_docs = get_available_documents_for_stage(0)
    message_text = format_stage_message(
        complaint_number=complaint_number,
        stage_index=0,
        stage_content=sanitize_telegram_html(content[:3500]),
        ai_review="Первичный анализ завершён. Проверьте данные и переходите к следующему этапу."
    )

    keyboard = build_stage_keyboard(
        current_stage_index=0,
        session_id=session_id,
        show_accept=True,
        show_doc_button=bool(available_docs),
        available_docs=available_docs
    )

    await context.bot.edit_message_text(
        chat_id=progress_msg.chat.id,
        message_id=progress_msg.message_id,
        text=message_text,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    return PCH_USER_REVIEW
