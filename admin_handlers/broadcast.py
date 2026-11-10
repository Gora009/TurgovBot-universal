"""Рассылки: подготовка, кнопки, запуск, фоновая отправка, отмена."""

import asyncio
import logging
import json
import time
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import Forbidden, BadRequest, RetryAfter

from database.manager import db_manager
from integrations.google_sheets import log_admin_action_to_sheet
from config import (
    BROADCAST_CHOOSE_AUDIENCE,
    BROADCAST_AWAITING_MESSAGE,
    BROADCAST_ATTACH_BUTTONS,
    BROADCAST_CONFIRM,
)

from .core import admin_panel


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 1: Выбор аудитории для рассылки."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('broadcast', None)  # Очищаем предыдущие данные рассылки

    # Считаем количество пользователей по категориям
    total = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE")
    subscribers = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND is_subscriber = TRUE")
    non_subs = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND (is_subscriber = FALSE OR is_subscriber IS NULL)")
    vips = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND is_vip = TRUE")
    testers = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND is_tester = TRUE")

    keyboard = [
        [InlineKeyboardButton(f"👥 Все пользователи ({total})", callback_data="bc_audience_all")],
        [InlineKeyboardButton(f"⭐ Подписчики ({subscribers})", callback_data="bc_audience_subscribers")],
        [InlineKeyboardButton(f"📭 Без подписки ({non_subs})", callback_data="bc_audience_non_subscribers")],
        [InlineKeyboardButton(f"👑 VIP ({vips})", callback_data="bc_audience_vip")],
        [InlineKeyboardButton(f"🧪 Тестеры ({testers})", callback_data="bc_audience_testers")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]

    await query.edit_message_text(
        "📢 <b>Рассылка — Шаг 1/4: Выбор аудитории</b>\n\n"
        "Выберите, кому отправить рассылку.\n"
        "<i>Заблокировавшие бота и забаненные автоматически исключены.</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return BROADCAST_CHOOSE_AUDIENCE


async def choose_broadcast_audience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2: Сохраняем аудиторию, просим отправить сообщение."""
    query = update.callback_query
    await query.answer()

    audience = query.data.replace("bc_audience_", "")
    context.user_data['broadcast'] = {'audience': audience}

    # Считаем точное количество
    base_filter = "is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE"
    if audience == "subscribers":
        base_filter += " AND is_subscriber = TRUE"
    elif audience == "non_subscribers":
        base_filter += " AND (is_subscriber = FALSE OR is_subscriber IS NULL)"
    elif audience == "vip":
        base_filter += " AND is_vip = TRUE"
    elif audience == "testers":
        base_filter += " AND is_tester = TRUE"

    count = await db_manager.fetchval(f"SELECT COUNT(*) FROM users WHERE {base_filter}")
    context.user_data['broadcast']['recipient_count'] = count
    context.user_data['broadcast']['db_filter'] = base_filter

    audience_names = {
        "all": "👥 Все пользователи",
        "subscribers": "⭐ Подписчики",
        "non_subscribers": "📭 Без подписки",
        "vip": "👑 VIP",
        "testers": "🧪 Тестеры"
    }

    await query.edit_message_text(
        f"📢 <b>Рассылка — Шаг 2/4: Сообщение</b>\n\n"
        f"Аудитория: <b>{audience_names.get(audience, audience)}</b> ({count} чел.)\n\n"
        f"Отправьте мне сообщение для рассылки.\n"
        f"Поддерживается <b>любой тип</b>:\n"
        f"• 📝 Текст (с HTML-разметкой)\n"
        f"• 🖼 Фото с подписью\n"
        f"• 🎥 Видео с подписью\n"
        f"• 📄 Документ\n"
        f"• 🎞 GIF-анимация\n"
        f"• 🎤 Голосовое сообщение\n"
        f"• 🎵 Аудио\n\n"
        f"<i>Сообщение будет переслано получателям в точном виде.</i>",
        parse_mode="HTML"
    )
    return BROADCAST_AWAITING_MESSAGE


async def receive_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: Получаем сообщение, показываем превью и опции."""
    message = update.message
    bc = context.user_data.get('broadcast', {})

    # Сохраняем координаты сообщения для copy_message
    bc['source_chat_id'] = message.chat_id
    bc['source_message_id'] = message.message_id

    # Определяем тип сообщения для отображения
    if message.photo:
        msg_type = "🖼 Фото"
    elif message.video:
        msg_type = "🎥 Видео"
    elif message.document:
        msg_type = "📄 Документ"
    elif message.animation:
        msg_type = "🎞 GIF"
    elif message.voice:
        msg_type = "🎤 Голосовое"
    elif message.audio:
        msg_type = "🎵 Аудио"
    elif message.sticker:
        msg_type = "😀 Стикер"
    else:
        msg_type = "📝 Текст"
    bc['msg_type'] = msg_type

    # Отправляем превью (копируем сообщение обратно админу)
    try:
        await message.reply_text("👁 <b>Превью рассылки:</b>", parse_mode="HTML")
        preview = await context.bot.copy_message(
            chat_id=message.chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id
        )
        bc['preview_message_id'] = preview.message_id
    except Exception as e:
        logging.error(f"[BROADCAST] Ошибка превью: {e}")
        await message.reply_text("❌ Не удалось создать превью. Попробуйте другое сообщение.")
        return BROADCAST_AWAITING_MESSAGE

    bc.setdefault('inline_keyboard', None)
    bc.setdefault('pin_message', False)
    context.user_data['broadcast'] = bc

    return await _show_broadcast_options(update, context)


async def _show_broadcast_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает меню опций рассылки."""
    bc = context.user_data.get('broadcast', {})

    pin_emoji = "✅" if bc.get('pin_message') else "⬜"
    buttons_status = "✅ Добавлены" if bc.get('inline_keyboard') else "⬜ Нет"

    audience_names = {
        "all": "👥 Все", "subscribers": "⭐ Подписчики",
        "non_subscribers": "📭 Без подписки", "vip": "👑 VIP"
    }

    text = (
        f"📢 <b>Рассылка — Шаг 3/4: Настройки</b>\n\n"
        f"📨 Тип: <b>{bc.get('msg_type', 'Текст')}</b>\n"
        f"👥 Аудитория: <b>{audience_names.get(bc.get('audience', 'all'), bc.get('audience'))}</b>\n"
        f"📊 Получателей: <b>{bc.get('recipient_count', 0)}</b>\n"
        f"🔘 Инлайн-кнопки: {buttons_status}\n"
        f"📌 Закрепить: {pin_emoji}\n"
    )

    keyboard = [
        [InlineKeyboardButton(f"🔘 {'Изменить' if bc.get('inline_keyboard') else 'Добавить'} кнопки", callback_data="bc_add_buttons")],
        [InlineKeyboardButton(f"📌 {'Убрать закрепление' if bc.get('pin_message') else 'Закрепить сообщение'}", callback_data="bc_toggle_pin")],
        [InlineKeyboardButton("✅ Запустить рассылку", callback_data="bc_confirm_send")],
        [InlineKeyboardButton("🔄 Изменить сообщение", callback_data="bc_change_message")],
        [InlineKeyboardButton("❌ Отмена", callback_data="bc_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Если вызвано из callback (toggle_pin, add_buttons) — редактируем
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="HTML")
            return BROADCAST_CONFIRM
        except BadRequest:
            pass  # "Message is not modified" или другое — отправляем новое

    # Если вызвано после получения media-сообщения — отправляем новое
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")
    return BROADCAST_CONFIRM


async def handle_broadcast_buttons_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Предлагает ввести инлайн-кнопки."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🔘 <b>Добавление инлайн-кнопок</b>\n\n"
        "Отправьте кнопки в формате (одна кнопка на строку):\n\n"
        "<code>Текст кнопки 1 - https://example.com\n"
        "Текст кнопки 2 - https://t.me/channel</code>\n\n"
        "Для двух кнопок в одном ряду разделяйте символом <code>|</code>:\n\n"
        "<code>Кнопка 1 - https://url1 | Кнопка 2 - https://url2</code>\n\n"
        "Отправьте <code>удалить</code> чтобы убрать кнопки.",
        parse_mode="HTML"
    )
    return BROADCAST_ATTACH_BUTTONS


async def receive_broadcast_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Парсит и сохраняет инлайн-кнопки."""
    text = update.message.text.strip()
    bc = context.user_data.get('broadcast', {})

    if text.lower() in ('удалить', 'убрать', 'нет', 'delete', 'remove'):
        bc['inline_keyboard'] = None
        context.user_data['broadcast'] = bc
        await update.message.reply_text("✅ Кнопки удалены.")
        return await _show_broadcast_options(update, context)

    try:
        keyboard = []
        for line in text.strip().split('\n'):
            row = []
            parts = line.split('|')
            for part in parts:
                part = part.strip()
                if ' - ' not in part:
                    raise ValueError(f"Неверный формат: '{part}'. Используйте 'Текст - URL'")
                btn_text, url = part.rsplit(' - ', 1)
                btn_text = btn_text.strip()
                url = url.strip()
                if not url.startswith(('http://', 'https://', 'tg://')):
                    raise ValueError(f"Неверный URL: '{url}'")
                row.append({"text": btn_text, "url": url})
            keyboard.append(row)

        # Проверяем, что кнопки валидны, пробуя собрать InlineKeyboardMarkup
        test_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=btn['text'], url=btn['url']) for btn in row]
            for row in keyboard
        ])

        bc['inline_keyboard'] = keyboard
        context.user_data['broadcast'] = bc

        btn_count = sum(len(row) for row in keyboard)
        await update.message.reply_text(f"✅ Добавлено кнопок: {btn_count}")

    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка формата:\n{e}\n\nПопробуйте ещё раз.")
        return BROADCAST_ATTACH_BUTTONS
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}\n\nПопробуйте ещё раз.")
        return BROADCAST_ATTACH_BUTTONS

    return await _show_broadcast_options(update, context)


async def toggle_broadcast_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключает опцию закрепления."""
    query = update.callback_query
    await query.answer()

    bc = context.user_data.get('broadcast', {})
    bc['pin_message'] = not bc.get('pin_message', False)
    context.user_data['broadcast'] = bc

    return await _show_broadcast_options(update, context)


async def change_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает к шагу выбора сообщения."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🔄 Отправьте новое сообщение для рассылки (любой тип).",
        parse_mode="HTML"
    )
    return BROADCAST_AWAITING_MESSAGE


async def confirm_and_start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 4: Подтверждаем и запускаем рассылку."""
    query = update.callback_query
    await query.answer()

    bc = context.user_data.get('broadcast', {})
    admin_id = query.from_user.id

    # Получаем список получателей
    db_filter = bc.get('db_filter', "is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE")
    user_ids = [r['user_id'] for r in await db_manager.fetch(f"SELECT user_id FROM users WHERE {db_filter}")]

    if not user_ids:
        await query.edit_message_text("❌ Нет подходящих получателей.")
        return await admin_panel(update, context)

    # Создаём запись в БД
    broadcast_id = await db_manager.fetchval(
        """INSERT INTO broadcasts 
           (admin_user_id, source_chat_id, source_message_id, audience_filter, 
            inline_keyboard_json, pin_message, total_recipients, status, started_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'running', NOW())
           RETURNING id""",
        admin_id,
        bc['source_chat_id'],
        bc['source_message_id'],
        bc.get('audience', 'all'),
        json.dumps(bc['inline_keyboard']) if bc.get('inline_keyboard') else None,
        bc.get('pin_message', False),
        len(user_ids)
    )

    # Формируем сообщение прогресса
    progress_msg = await query.edit_message_text(
        f"🚀 <b>Рассылка #{broadcast_id} запущена!</b>\n\n"
        f"📊 Прогресс: 0/{len(user_ids)} (0%)\n"
        f"<code>░░░░░░░░░░░░░░░░░░░░</code>\n\n"
        f"✅ Доставлено: 0\n"
        f"❌ Ошибок: 0\n"
        f"🚫 Заблокировали: 0",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ Остановить рассылку", callback_data=f"bc_cancel_running_{broadcast_id}")]
        ])
    )

    # Запускаем фоновую задачу
    context.job_queue.run_once(
        _broadcast_job,
        when=0.5,
        data={
            'broadcast_id': broadcast_id,
            'user_ids': user_ids,
            'source_chat_id': bc['source_chat_id'],
            'source_message_id': bc['source_message_id'],
            'inline_keyboard': bc.get('inline_keyboard'),
            'pin_message': bc.get('pin_message', False),
            'progress_message_id': progress_msg.message_id,
            'admin_chat_id': query.message.chat_id,
        },
        chat_id=query.message.chat_id,
        name=f"broadcast_{broadcast_id}"
    )

    await log_admin_action_to_sheet(admin_id, "BROADCAST_START",
                                    f"ID: {broadcast_id}, Audience: {bc.get('audience')}, Recipients: {len(user_ids)}")

    context.user_data.pop('broadcast', None)
    return ConversationHandler.END


async def _broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача рассылки с rate limiting и прогрессом в реальном времени."""
    job = context.job
    data = job.data
    bot = context.bot

    broadcast_id = data['broadcast_id']
    user_ids = data['user_ids']
    source_chat_id = data['source_chat_id']
    source_message_id = data['source_message_id']
    inline_keyboard = data.get('inline_keyboard')
    pin_message = data.get('pin_message', False)
    progress_message_id = data['progress_message_id']
    admin_chat_id = data['admin_chat_id']

    # Подготавливаем reply_markup если есть кнопки
    reply_markup = None
    if inline_keyboard:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=btn['text'], url=btn['url']) for btn in row]
            for row in inline_keyboard
        ])

    delivered = 0
    failed = 0
    blocked = 0
    total = len(user_ids)
    last_progress_update = 0
    cancel_key = f'cancel_broadcast_{broadcast_id}'

    for i, user_id in enumerate(user_ids):
        # Проверяем отмену
        if context.bot_data.get(cancel_key, False):
            logging.info(f"[BROADCAST #{broadcast_id}] Отменена администратором на {i}/{total}")
            break

        try:
            sent_msg = await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=reply_markup
            )
            delivered += 1

            # Закрепляем если нужно
            if pin_message:
                try:
                    await bot.pin_chat_message(
                        chat_id=user_id,
                        message_id=sent_msg.message_id,
                        disable_notification=True
                    )
                except Exception:
                    pass  # Закрепление может не работать в личных чатах старых версий

        except RetryAfter as e:
            # Telegram просит подождать
            wait_time = e.retry_after + 1
            logging.warning(f"[BROADCAST #{broadcast_id}] RetryAfter: ждём {wait_time}с")
            await asyncio.sleep(wait_time)
            # Повторяем попытку
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                    reply_markup=reply_markup
                )
                delivered += 1
            except Forbidden:
                blocked += 1
                await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id)
            except Exception:
                failed += 1

        except Forbidden:
            blocked += 1
            # Помечаем пользователя как заблокировавшего бота
            await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id)

        except BadRequest as e:
            failed += 1
            logging.warning(f"[BROADCAST #{broadcast_id}] BadRequest для {user_id}: {e}")

        except Exception as e:
            failed += 1
            logging.warning(f"[BROADCAST #{broadcast_id}] Ошибка для {user_id}: {e}")

        # Rate limiting: ~25 msg/sec
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1.1)
        else:
            await asyncio.sleep(0.04)

        # Обновляем прогресс каждые 3 секунды
        now = time.time()
        if now - last_progress_update >= 3:
            last_progress_update = now
            await _update_broadcast_progress(
                bot, admin_chat_id, progress_message_id, broadcast_id,
                i + 1, total, delivered, failed, blocked
            )

    # Определяем финальный статус
    was_cancelled = context.bot_data.pop(cancel_key, False)
    final_status = 'cancelled' if was_cancelled else 'completed'

    # Обновляем БД
    await db_manager.execute(
        """UPDATE broadcasts 
           SET delivered = $1, failed = $2, blocked_users = $3, status = $4, completed_at = NOW()
           WHERE id = $5""",
        delivered, failed, blocked, final_status, broadcast_id
    )

    # Финальный отчёт
    status_emoji = "⛔" if was_cancelled else "✅"
    status_text = "ОТМЕНЕНА" if was_cancelled else "ЗАВЕРШЕНА"

    progress_bar = _make_progress_bar(delivered + failed + blocked, total)
    report = (
        f"{status_emoji} <b>Рассылка #{broadcast_id} {status_text}</b>\n\n"
        f"📊 Прогресс: {delivered + failed + blocked}/{total}\n"
        f"{progress_bar}\n\n"
        f"✅ Доставлено: <b>{delivered}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n\n"
        f"⏱ Завершено: {datetime.now().strftime('%H:%M:%S')}"
    )

    try:
        await bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=progress_message_id,
            text=report,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"[BROADCAST #{broadcast_id}] Не удалось обновить финальный отчёт: {e}")
        try:
            await bot.send_message(chat_id=admin_chat_id, text=report, parse_mode="HTML")
        except Exception as e2:
            logging.warning(f"[BROADCAST #{broadcast_id}] Финальный отчёт не доставлен админу: {e2}")

    logging.info(f"[BROADCAST #{broadcast_id}] {status_text}: delivered={delivered}, failed={failed}, blocked={blocked}")


async def _update_broadcast_progress(bot, chat_id, message_id, broadcast_id, current, total, delivered, failed, blocked):
    """Обновляет сообщение прогресса рассылки."""
    try:
        progress_bar = _make_progress_bar(current, total)
        pct = int(current / total * 100) if total > 0 else 0

        text = (
            f"🚀 <b>Рассылка #{broadcast_id} в процессе...</b>\n\n"
            f"📊 Прогресс: {current}/{total} ({pct}%)\n"
            f"{progress_bar}\n\n"
            f"✅ Доставлено: {delivered}\n"
            f"❌ Ошибок: {failed}\n"
            f"🚫 Заблокировали: {blocked}"
        )

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⛔ Остановить", callback_data=f"bc_cancel_running_{broadcast_id}")]
            ])
        )
    except BadRequest:
        pass  # "Message is not modified" — прогресс не изменился
    except Exception as e:
        logging.debug(f"[BROADCAST] Ошибка обновления прогресса: {e}")


def _make_progress_bar(current: int, total: int, length: int = 20) -> str:
    """Создаёт визуальный прогресс-бар."""
    if total == 0:
        return "<code>" + "░" * length + "</code>"
    filled = int(length * current / total)
    bar = "█" * filled + "░" * (length - filled)
    return f"<code>{bar}</code>"


async def cancel_running_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Останавливает запущенную рассылку."""
    query = update.callback_query
    broadcast_id_str = query.data.replace("bc_cancel_running_", "")

    try:
        broadcast_id = int(broadcast_id_str)
    except ValueError:
        await query.answer("❌ Неверный ID рассылки", show_alert=True)
        return ConversationHandler.END

    context.bot_data[f'cancel_broadcast_{broadcast_id}'] = True
    await query.answer("⛔ Рассылка будет остановлена...", show_alert=True)
    logging.info(f"[BROADCAST #{broadcast_id}] Запрос на отмену от {query.from_user.id}")
    return ConversationHandler.END


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена рассылки до запуска."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('broadcast', None)
    await query.edit_message_text("❌ Рассылка отменена.")
    return await admin_panel(update, context)
