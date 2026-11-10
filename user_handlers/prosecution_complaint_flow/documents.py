"""История сессий, документы, доп. материалы, перевод в уголовное дело, завершение и отмена."""

from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from config import (
    PCH_USER_REVIEW,
    PCH_NAVIGATE,
    PCH_ADD_MATERIAL,
    PCH_EXPORT,
    PCH_STAGES_ORDER,
    PCH_STAGES_DISPLAY,
    PCH_DOCUMENT_TYPES,
)
from common.utils import sanitize_telegram_html, safe_answer_callback_query
from ..core import _display_main_menu

from .jobs import remove_pch_scheduled_jobs
from .pipeline import (
    build_document_selection_keyboard,
    convert_to_criminal_case,
    generate_document,
    get_available_documents_for_stage,
)
from .session import ProsecutionComplaintManager, safe_edit_message_text
from .stages import show_current_stage
from .ui import format_session_status


# === ПРОДОЛЖЕНИЕ СЕССИИ ===

async def handle_continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Продолжает существующую сессию."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_continue_")[1])
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    if not session:
        await safe_edit_message_text(
            query,
            "❌ Сессия не найдена. Начните заново.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Новая жалоба", callback_data="pch_create_new")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Восстанавливаем контекст
    context.user_data['pch_session_id'] = session_id
    context.user_data['pch_current_stage_index'] = session.get('current_stage_index', 0)
    context.user_data['pch_server_id'] = session.get('server_id')

    return await show_current_stage(update, context, session_id=session_id)


# === ИСТОРИЯ ЖАЛОБ ===

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает полную историю жалоб пользователя."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id
    sessions = await ProsecutionComplaintManager.get_user_sessions(user_id, limit=15)

    if not sessions:
        await safe_edit_message_text(
            query,
            "📜 <b>История жалоб</b>\n\n<i>У вас пока нет обработанных жалоб.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Новая жалоба", callback_data="pch_create_new")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_NAVIGATE

    text = "📜 <b>ИСТОРИЯ ЖАЛОБ</b>\n══════════════════════════════════════════\n\n"

    for session in sessions:
        status_info = format_session_status(session)
        date_str = session['created_at'].strftime('%d.%m.%Y') if session.get('created_at') else 'N/A'
        complaint_num = session.get('complaint_number', 'Без номера')
        text += f"📋 <b>{complaint_num}</b> ({date_str})\n   {status_info}\n\n"

    text += "──────────────────────────────────────────\n"
    text += "<i>Выберите жалобу для просмотра:</i>"

    # Формируем кнопки
    keyboard = []
    for session in sessions[:5]:  # Максимум 5 кнопок
        complaint_num = session.get('complaint_number', 'N/A')
        status = session.get('status', '')
        btn_text = f"📋 {complaint_num}"
        if status == 'active':
            btn_text = f"⏳ {complaint_num}"
        keyboard.append([InlineKeyboardButton(
            btn_text,
            callback_data=f"pch_view_session_{session['id']}"
        )])

    keyboard.append([InlineKeyboardButton("➕ Новая жалоба", callback_data="pch_create_new")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_NAVIGATE


async def view_session_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детали конкретной сессии."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_view_session_")[1])
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    if not session:
        await safe_edit_message_text(
            query,
            "❌ Жалоба не найдена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ К истории", callback_data="pch_history")]
            ])
        )
        return PCH_NAVIGATE

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    status = session.get('status', 'unknown')
    created_at = session.get('created_at')
    date_str = created_at.strftime('%d.%m.%Y %H:%M') if created_at else 'N/A'
    stage_index = session.get('current_stage_index', 0)

    text = f"📋 <b>ЖАЛОБА {complaint_number}</b>\n══════════════════════════════════════════\n\n"
    text += f"📅 <b>Создана:</b> {date_str}\n"
    text += f"📊 <b>Статус:</b> {format_session_status(session)}\n"

    if status == 'active':
        stage_type = PCH_STAGES_ORDER[stage_index] if stage_index < len(PCH_STAGES_ORDER) else 'N/A'
        text += f"📍 <b>Текущий этап:</b> {PCH_STAGES_DISPLAY.get(stage_type, stage_type)}\n"

    # Получаем сгенерированные документы
    generated_docs = await manager.get_generated_documents()
    if generated_docs:
        text += f"\n📄 <b>Документы ({len(generated_docs)}):</b>\n"
        for doc in generated_docs[:5]:
            text += f"  • {doc.get('type_display', doc.get('type'))}\n"

    if session.get('linked_criminal_case_id'):
        text += f"\n🔗 <b>Связано с уголовным делом:</b> #{session['linked_criminal_case_id']}"

    # Формируем кнопки
    keyboard = []

    if status == 'active':
        keyboard.append([InlineKeyboardButton(
            "▶️ Продолжить работу",
            callback_data=f"pch_continue_{session_id}"
        )])
    elif status == 'completed' or status == 'converted_to_case':
        if generated_docs:
            keyboard.append([InlineKeyboardButton(
                "📄 Просмотреть документы",
                callback_data=f"pch_view_docs_{session_id}"
            )])

    keyboard.append([InlineKeyboardButton("◀️ К истории", callback_data="pch_history")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_NAVIGATE


# === РАБОТА С ДОКУМЕНТАМИ ===

async def show_document_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает меню выбора типа документа для генерации."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_gen_doc_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    available_docs = get_available_documents_for_stage(current_stage_index)

    if not available_docs:
        await query.answer("На этом этапе нет доступных документов для генерации.", show_alert=True)
        return PCH_USER_REVIEW

    stage_name = PCH_STAGES_DISPLAY.get(PCH_STAGES_ORDER[current_stage_index], 'текущего этапа')
    text = (
        f"📄 <b>Генерация документа</b>\n\n"
        f"Этап: {stage_name}\n\n"
        "Выберите тип документа для генерации:"
    )

    keyboard = build_document_selection_keyboard(session_id, available_docs)

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    return PCH_USER_REVIEW


async def handle_generate_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Генерирует выбранный документ."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    # Парсим callback_data: pch_doc_{doc_type}_{session_id}
    # doc_type может содержать _, поэтому берём всё между pch_doc_ и последним _
    data = query.data  # например: pch_doc_acceptance_notice_7
    prefix = "pch_doc_"
    suffix_part = data[len(prefix):]  # acceptance_notice_7
    last_underscore = suffix_part.rfind("_")
    doc_type = suffix_part[:last_underscore]  # acceptance_notice
    session_id = int(suffix_part[last_underscore + 1:])  # 7

    doc_name = PCH_DOCUMENT_TYPES.get(doc_type, doc_type)

    # Показываем индикатор
    await safe_edit_message_text(
        query,
        f"⏳ <b>Генерирую документ...</b>\n\n📄 {doc_name}",
        parse_mode='HTML'
    )

    # Генерируем документ
    success, content, error = await generate_document(session_id, doc_type)

    if not success:
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка генерации документа</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_doc_{doc_type}_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Показываем сгенерированный документ
    text = (
        f"✅ <b>ДОКУМЕНТ СГЕНЕРИРОВАН</b>\n"
        f"📄 <b>{doc_name}</b>\n"
        f"══════════════════════════════════════════\n\n"
        f"{sanitize_telegram_html(content[:3500])}\n\n"
        f"──────────────────────────────────────────"
    )

    keyboard = [
        [InlineKeyboardButton("📄 Создать ещё документ", callback_data=f"pch_gen_doc_{session_id}")],
        [InlineKeyboardButton("◀️ К этапу", callback_data=f"pch_show_stage_{session_id}")]
    ]

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_USER_REVIEW


async def show_custom_document_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает форму для ввода своего запроса на документ."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_custom_doc_")[1])
    context.user_data['pch_custom_doc_session_id'] = session_id

    text = (
        "✏️ <b>Свой запрос на документ</b>\n\n"
        "Опишите, какой документ вам нужен. Например:\n"
        "• <i>Повестка для свидетеля John Doe</i>\n"
        "• <i>Запрос в LSPD о служебной проверке офицера</i>\n"
        "• <i>Требование о предоставлении видеозаписей с камер</i>\n"
        "• <i>Уведомление заявителю о продлении срока рассмотрения</i>\n\n"
        "📝 <b>Напишите ваш запрос:</b>"
    )

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_gen_doc_{session_id}")]
        ])
    )

    return PCH_ADD_MATERIAL  # Используем состояние для ввода текста


async def handle_custom_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает текстовый запрос на кастомный документ."""
    user_request = update.message.text.strip()
    session_id = context.user_data.get('pch_custom_doc_session_id')

    if not session_id:
        await update.message.reply_text(
            "❌ Сессия не найдена. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")]
            ])
        )
        return PCH_USER_REVIEW

    if len(user_request) < 10:
        await update.message.reply_text(
            "⚠️ Слишком короткий запрос. Опишите подробнее, какой документ вам нужен.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_gen_doc_{session_id}")]
            ])
        )
        return PCH_ADD_MATERIAL

    # Показываем индикатор генерации
    progress_msg = await update.message.reply_text(
        f"⏳ <b>Генерирую документ по вашему запросу...</b>\n\n"
        f"📝 <i>{user_request[:100]}{'...' if len(user_request) > 100 else ''}</i>",
        parse_mode='HTML'
    )

    # Генерируем документ с кастомными инструкциями
    success, content, error = await generate_document(
        session_id,
        doc_type='custom',
        additional_instructions=user_request
    )

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка генерации документа</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"pch_custom_doc_{session_id}")],
                [InlineKeyboardButton("◀️ К списку документов", callback_data=f"pch_gen_doc_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Показываем результат
    text = (
        f"✅ <b>ДОКУМЕНТ СГЕНЕРИРОВАН</b>\n"
        f"📝 <b>По запросу:</b> <i>{user_request[:50]}{'...' if len(user_request) > 50 else ''}</i>\n"
        f"══════════════════════════════════════════\n\n"
        f"{sanitize_telegram_html(content[:3500])}\n\n"
        f"──────────────────────────────────────────"
    )

    keyboard = [
        [InlineKeyboardButton("📄 Создать ещё документ", callback_data=f"pch_gen_doc_{session_id}")],
        [InlineKeyboardButton("◀️ К этапу", callback_data=f"pch_show_stage_{session_id}")]
    ]

    await context.bot.edit_message_text(
        chat_id=progress_msg.chat.id,
        message_id=progress_msg.message_id,
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # Очищаем временные данные
    context.user_data.pop('pch_custom_doc_session_id', None)

    return PCH_USER_REVIEW


async def view_generated_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список сгенерированных документов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_view_docs_")[1])
    manager = ProsecutionComplaintManager(session_id)
    generated_docs = await manager.get_generated_documents()

    if not generated_docs:
        await query.answer("Документы ещё не генерировались.", show_alert=True)
        return PCH_EXPORT

    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    text = (
        f"📄 <b>ДОКУМЕНТЫ ЖАЛОБЫ {complaint_number}</b>\n"
        f"══════════════════════════════════════════\n\n"
    )

    for i, doc in enumerate(generated_docs, 1):
        doc_name = doc.get('type_display', doc.get('type'))
        created_at = doc.get('created_at', 'N/A')
        if isinstance(created_at, str) and 'T' in created_at:
            created_at = created_at.split('T')[0]
        text += f"<b>{i}. {doc_name}</b>\n   📅 {created_at}\n\n"

    text += "──────────────────────────────────────────\n"
    text += "<i>Выберите документ для просмотра:</i>"

    # Кнопки для каждого документа
    keyboard = []
    for i, doc in enumerate(generated_docs):
        doc_name = doc.get('type_display', doc.get('type'))
        keyboard.append([InlineKeyboardButton(
            f"📄 {doc_name}",
            callback_data=f"pch_show_doc_{session_id}_{i}"
        )])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_EXPORT


async def show_single_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает содержимое конкретного документа."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    # Парсим: pch_show_doc_{session_id}_{doc_index}
    parts = query.data.split("_")
    session_id = int(parts[3])
    doc_index = int(parts[4])

    manager = ProsecutionComplaintManager(session_id)
    generated_docs = await manager.get_generated_documents()

    if doc_index >= len(generated_docs):
        await query.answer("Документ не найден.", show_alert=True)
        return PCH_EXPORT

    doc = generated_docs[doc_index]
    doc_name = doc.get('type_display', doc.get('type'))
    content = doc.get('content', 'Содержимое не найдено')

    text = (
        f"📄 <b>{doc_name}</b>\n"
        f"══════════════════════════════════════════\n\n"
        f"{sanitize_telegram_html(content[:3800])}\n\n"
        f"──────────────────────────────────────────"
    )

    keyboard = [
        [InlineKeyboardButton("◀️ К списку документов", callback_data=f"pch_view_docs_{session_id}")]
    ]

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_EXPORT


# === ДОБАВЛЕНИЕ МАТЕРИАЛОВ ===

async def show_add_material_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает приглашение для добавления материалов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_add_material_")[1])
    context.user_data['pch_adding_material'] = True

    await safe_edit_message_text(
        query,
        "📎 <b>Добавление материалов</b>\n\n"
        "Отправьте дополнительные материалы по делу:\n\n"
        "• Ссылки на доказательства\n"
        "• Показания свидетелей\n"
        "• Дополнительную информацию\n"
        "• Скриншоты (описанием)\n\n"
        "<i>Материалы будут добавлены к собранным данным.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    return PCH_ADD_MATERIAL


async def handle_add_material_or_custom_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обёртка для обработки текстового ввода в состоянии PCH_ADD_MATERIAL.
    Определяет, что делать: добавить материал или сгенерировать кастомный документ.
    """
    # Если есть pch_custom_doc_session_id — это запрос на кастомный документ
    if context.user_data.get('pch_custom_doc_session_id'):
        return await handle_custom_document_input(update, context)

    # Иначе — это добавление материала
    return await handle_material_input(update, context)


async def handle_material_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод дополнительных материалов."""
    material_text = update.message.text.strip()
    session_id = context.user_data.get('pch_session_id')

    if not session_id:
        await update.message.reply_text("❌ Ошибка сессии. Начните заново.")
        return ConversationHandler.END

    context.user_data['pch_adding_material'] = False

    manager = ProsecutionComplaintManager(session_id)

    # Добавляем материал к собранным данным
    collected = await manager.get_collected_data()
    materials = collected.get('additional_materials', [])
    materials.append({
        'content': material_text,
        'added_at': datetime.now().isoformat()
    })
    await manager.update_collected_data({'additional_materials': materials})

    await update.message.reply_text(
        "✅ <b>Материал добавлен!</b>\n\n"
        f"Всего добавлено материалов: {len(materials)}",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📎 Добавить ещё", callback_data=f"pch_add_material_{session_id}")],
            [InlineKeyboardButton("◀️ К этапу", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    return PCH_USER_REVIEW


# === КОНВЕРТАЦИЯ В УГОЛОВНОЕ ДЕЛО ===

async def handle_convert_to_ccw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Конвертирует жалобу в уголовное дело."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_convert_ccw_")[1])

    # Показываем подтверждение
    await safe_edit_message_text(
        query,
        "🔄 <b>Конвертация в уголовное дело</b>\n\n"
        "Все материалы жалобы будут переданы в систему создания уголовных дел (CCW).\n\n"
        "Продолжить?",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, конвертировать", callback_data=f"pch_confirm_ccw_{session_id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    return PCH_EXPORT


async def confirm_convert_to_ccw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждает и выполняет конвертацию."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_confirm_ccw_")[1])

    # Показываем индикатор
    await safe_edit_message_text(
        query,
        "⏳ <b>Создаю уголовное дело...</b>",
        parse_mode='HTML'
    )

    success, ccw_session_id, error = await convert_to_criminal_case(session_id, context)

    if not success:
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка конвертации</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_convert_ccw_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_EXPORT

    # Успешно создано
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    await safe_edit_message_text(
        query,
        f"✅ <b>УГОЛОВНОЕ ДЕЛО СОЗДАНО!</b>\n\n"
        f"📋 Жалоба: <b>{complaint_number}</b>\n"
        f"📂 Уголовное дело: <b>#{ccw_session_id}</b>\n\n"
        "Материалы жалобы перенесены в уголовное дело.\n"
        "Перейдите в раздел «Уголовные дела» для продолжения работы.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 К уголовным делам", callback_data="start_criminal_case")],
            [InlineKeyboardButton("📜 К истории жалоб", callback_data="pch_history")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
        ])
    )

    return PCH_NAVIGATE


# === ЗАВЕРШЕНИЕ И ОТМЕНА ===

async def complete_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершает сессию обработки жалобы."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_complete_")[1])
    manager = ProsecutionComplaintManager(session_id)

    await manager.close_session('completed')

    # Удаляем запланированные задачи
    remove_pch_scheduled_jobs(context, session_id)

    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    await safe_edit_message_text(
        query,
        f"✅ <b>Жалоба {complaint_number} завершена!</b>\n\n"
        "Все материалы сохранены. Вы можете просмотреть их в истории жалоб.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 История жалоб", callback_data="pch_history")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
        ])
    )

    return ConversationHandler.END


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущую операцию и возвращает в главное меню."""
    query = update.callback_query
    if query:
        await safe_answer_callback_query(query)

        # Проверяем, есть ли session_id в callback_data
        if 'pch_cancel_' in query.data:
            session_id = int(query.data.split("pch_cancel_")[1])
            # Не закрываем сессию, просто выходим
            remove_pch_scheduled_jobs(context, session_id)

    # Очищаем user_data
    keys_to_remove = [k for k in context.user_data.keys() if k.startswith('pch_')]
    for key in keys_to_remove:
        del context.user_data[key]

    if query:
        await _display_main_menu(update, context)

    return ConversationHandler.END
