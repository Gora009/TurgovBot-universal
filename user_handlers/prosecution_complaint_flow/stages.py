"""Навигация по этапам: принять, уточнить, перегенерировать, показать, вперёд/назад, финал."""

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import PCH_PHASE_A, PCH_USER_REVIEW, PCH_EXPORT, PCH_STAGES_ORDER, PCH_STAGES_DISPLAY
from common.utils import sanitize_telegram_html, safe_answer_callback_query

from .pipeline import (
    get_available_documents_for_stage,
    regenerate_stage,
    run_clarification_phase,
    run_finalization_phase,
    run_planning_phase,
)
from .session import ProsecutionComplaintManager, safe_edit_message_text
from .ui import build_stage_keyboard, format_stage_message


# === ОБРАБОТЧИКИ ФАЗ ===

async def handle_accept_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимает текущий этап и переходит к следующему."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_accept_")[1])
    manager = ProsecutionComplaintManager(session_id)

    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Помечаем этап как финальный
    await manager.mark_stage_final(stage_type)

    # Переходим к следующему этапу
    next_stage_index = current_stage_index + 1

    if next_stage_index >= len(PCH_STAGES_ORDER):
        # Все этапы завершены - показываем финальный экран
        return await show_completion_screen(update, context, session_id)

    # Обновляем индекс
    await manager.update_session(current_stage_index=next_stage_index)
    context.user_data['pch_current_stage_index'] = next_stage_index

    # Показываем индикатор загрузки
    await safe_edit_message_text(
        query,
        f"⏳ <b>Генерирую этап {next_stage_index + 1} из {len(PCH_STAGES_ORDER)}...</b>\n\n"
        f"{PCH_STAGES_DISPLAY.get(PCH_STAGES_ORDER[next_stage_index], 'Обработка...')}",
        parse_mode='HTML'
    )

    # Запускаем соответствующую фазу
    stage_runners = {
        1: run_planning_phase,
        2: run_clarification_phase,
        3: run_finalization_phase
    }

    runner = stage_runners.get(next_stage_index)
    if runner:
        success, content, error = await runner(session_id)

        if not success:
            await safe_edit_message_text(
                query,
                f"❌ <b>Ошибка генерации этапа</b>\n\n{error}",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_accept_{session_id}")],
                    [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
                ])
            )
            return PCH_USER_REVIEW

        # Показываем результат
        return await show_current_stage(update, context, session_id=session_id)

    return PCH_USER_REVIEW


async def handle_refine_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает уточнения для текущего этапа."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_refine_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_name = PCH_STAGES_DISPLAY.get(PCH_STAGES_ORDER[current_stage_index], 'этапа')

    await safe_edit_message_text(
        query,
        f"✏️ <b>Уточнение {stage_name}</b>\n\n"
        "Введите ваши комментарии, уточнения или дополнительную информацию.\n\n"
        "<i>AI учтёт ваш ввод при перегенерации этапа.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    context.user_data['pch_awaiting_refinement'] = True
    return PCH_PHASE_A  # Используем состояние для ввода текста


async def handle_refinement_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод уточнений и перегенерирует этап."""
    user_feedback = update.message.text.strip()
    session_id = context.user_data.get('pch_session_id')
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    context.user_data['pch_awaiting_refinement'] = False

    # Показываем индикатор
    progress_msg = await update.message.reply_text(
        "⏳ <b>Обрабатываю уточнения...</b>",
        parse_mode='HTML'
    )

    # Перегенерируем этап
    success, content, error = await regenerate_stage(
        session_id=session_id,
        stage_type=stage_type,
        user_feedback=user_feedback
    )

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_refine_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Удаляем сообщение прогресса
    try:
        await context.bot.delete_message(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id
        )
    except Exception:
        pass

    # Показываем обновлённый этап
    return await show_current_stage_new_message(update, context, session_id=session_id)


async def handle_regenerate_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Полностью перегенерирует текущий этап."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_regenerate_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Показываем индикатор
    await safe_edit_message_text(
        query,
        f"⏳ <b>Перегенерирую {PCH_STAGES_DISPLAY.get(stage_type, 'этап')}...</b>",
        parse_mode='HTML'
    )

    # Перегенерируем
    success, content, error = await regenerate_stage(
        session_id=session_id,
        stage_type=stage_type
    )

    if not success:
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_regenerate_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Показываем результат
    return await show_current_stage(update, context, session_id=session_id)


async def show_current_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: int = None
) -> int:
    """Показывает текущий этап (редактирует существующее сообщение)."""
    query = update.callback_query

    if session_id is None:
        session_id = context.user_data.get('pch_session_id')
        if query and 'pch_show_stage_' in query.data:
            session_id = int(query.data.split("pch_show_stage_")[1])

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    current_stage_index = context.user_data.get('pch_current_stage_index', session.get('current_stage_index', 0))
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Получаем контент этапа
    stage_data = await manager.get_stage_with_notes(stage_type)
    content = stage_data.get('content', 'Контент не найден') if stage_data else 'Этап ещё не сгенерирован'
    ai_notes = stage_data.get('ai_review_notes') if stage_data else None

    # Форматируем сообщение
    message_text = format_stage_message(
        complaint_number=complaint_number,
        stage_index=current_stage_index,
        stage_content=sanitize_telegram_html(content[:3500]),
        ai_review=ai_notes
    )

    available_docs = get_available_documents_for_stage(current_stage_index)
    keyboard = build_stage_keyboard(
        current_stage_index=current_stage_index,
        session_id=session_id,
        show_accept=True,
        show_doc_button=bool(available_docs),
        available_docs=available_docs
    )

    if query:
        await safe_edit_message_text(
            query,
            message_text,
            parse_mode='HTML',
            reply_markup=keyboard
        )

    return PCH_USER_REVIEW


async def show_current_stage_new_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: int = None
) -> int:
    """Показывает текущий этап (отправляет новое сообщение)."""
    if session_id is None:
        session_id = context.user_data.get('pch_session_id')

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    current_stage_index = context.user_data.get('pch_current_stage_index', session.get('current_stage_index', 0))
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Получаем контент этапа
    stage_data = await manager.get_stage_with_notes(stage_type)
    content = stage_data.get('content', 'Контент не найден') if stage_data else 'Этап ещё не сгенерирован'
    ai_notes = stage_data.get('ai_review_notes') if stage_data else None

    # Форматируем сообщение
    message_text = format_stage_message(
        complaint_number=complaint_number,
        stage_index=current_stage_index,
        stage_content=sanitize_telegram_html(content[:3500]),
        ai_review=ai_notes
    )

    available_docs = get_available_documents_for_stage(current_stage_index)
    keyboard = build_stage_keyboard(
        current_stage_index=current_stage_index,
        session_id=session_id,
        show_accept=True,
        show_doc_button=bool(available_docs),
        available_docs=available_docs
    )

    await update.message.reply_text(
        message_text,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    return PCH_USER_REVIEW


async def _show_completion_screen_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обёртка для show_completion_screen, извлекает session_id из callback_data."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    session_id = int(query.data.split("pch_completion_")[1])
    return await show_completion_screen(update, context, session_id)


async def show_completion_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: int
) -> int:
    """Показывает экран завершения обработки жалобы."""
    query = update.callback_query
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    generated_docs = await manager.get_generated_documents()

    text = (
        f"✅ <b>ОБРАБОТКА ЖАЛОБЫ ЗАВЕРШЕНА</b>\n\n"
        f"📋 Номер: <b>{complaint_number}</b>\n"
        f"📄 Сгенерировано документов: <b>{len(generated_docs)}</b>\n\n"
    )

    if generated_docs:
        text += "<b>Созданные документы:</b>\n"
        for i, doc in enumerate(generated_docs, 1):
            text += f"  {i}. {doc.get('type_display', doc.get('type'))}\n"
        text += "\n"

    text += (
        "<b>Доступные действия:</b>\n"
        "• Просмотреть сгенерированные документы\n"
        "• Конвертировать в уголовное дело\n"
        "• Завершить работу с жалобой"
    )

    keyboard = [
        [InlineKeyboardButton(
            "📄 Просмотреть документы",
            callback_data=f"pch_view_docs_{session_id}"
        )],
        [InlineKeyboardButton(
            "🔄 Конвертировать в уголовное дело",
            callback_data=f"pch_convert_ccw_{session_id}"
        )],
        [InlineKeyboardButton(
            "✅ Завершить",
            callback_data=f"pch_complete_{session_id}"
        )],
        [InlineKeyboardButton("◀️ К этапам", callback_data=f"pch_show_stage_{session_id}")]
    ]

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_EXPORT


# Конец Части 3


# === ЧАСТЬ 4: НАВИГАЦИЯ, ИСТОРИЯ, ПЛАНИРОВЩИК И CONVERSATION HANDLER ===

# === НАВИГАЦИЯ МЕЖДУ ЭТАПАМИ ===

async def handle_prev_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переходит к предыдущему этапу."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_prev_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)

    if current_stage_index > 0:
        context.user_data['pch_current_stage_index'] = current_stage_index - 1

    return await show_current_stage(update, context, session_id=session_id)


async def handle_next_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переходит к следующему этапу (только просмотр, без генерации)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_next_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)

    if current_stage_index < len(PCH_STAGES_ORDER) - 1:
        # Проверяем, есть ли контент для следующего этапа
        manager = ProsecutionComplaintManager(session_id)
        next_stage_type = PCH_STAGES_ORDER[current_stage_index + 1]
        next_content = await manager.get_latest_stage_content(next_stage_type)

        if next_content:
            context.user_data['pch_current_stage_index'] = current_stage_index + 1
        else:
            # Этап ещё не сгенерирован - показываем подсказку
            await query.answer(
                "Этот этап ещё не сгенерирован. Нажмите «Принять этап» для генерации.",
                show_alert=True
            )
            return PCH_USER_REVIEW

    return await show_current_stage(update, context, session_id=session_id)
