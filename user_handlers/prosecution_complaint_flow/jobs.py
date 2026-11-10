"""Фоновые задачи: предупреждение об истечении и автозакрытие сессии."""

import logging
from datetime import timedelta
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import PCH_SESSION_TIMEOUT_HOURS, PCH_WARNING_BEFORE_HOURS

from .session import ProsecutionComplaintManager


# === ПЛАНИРОВЩИК АВТОЗАКРЫТИЯ ===

def schedule_pch_session_closure(context: ContextTypes.DEFAULT_TYPE, session_id: int, user_id: int) -> None:
    """Планирует автозакрытие сессии и уведомление."""
    # Планируем уведомление за 24 часа до закрытия
    warning_delay = timedelta(hours=PCH_SESSION_TIMEOUT_HOURS - PCH_WARNING_BEFORE_HOURS)
    context.job_queue.run_once(
        pch_expiry_warning_job,
        when=warning_delay,
        data={'session_id': session_id, 'user_id': user_id},
        name=f"pch_warning_{session_id}"
    )

    # Планируем автозакрытие
    closure_delay = timedelta(hours=PCH_SESSION_TIMEOUT_HOURS)
    context.job_queue.run_once(
        pch_auto_close_job,
        when=closure_delay,
        data={'session_id': session_id, 'user_id': user_id},
        name=f"pch_close_{session_id}"
    )

    logging.info(
        f"[PCH] Запланировано: уведомление через {warning_delay}, "
        f"закрытие через {closure_delay} для сессии #{session_id}"
    )


def remove_pch_scheduled_jobs(context: ContextTypes.DEFAULT_TYPE, session_id: int) -> None:
    """Удаляет запланированные задачи для сессии."""
    for job_name in [f"pch_warning_{session_id}", f"pch_close_{session_id}"]:
        jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()


async def pch_expiry_warning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет уведомление о скором закрытии сессии."""
    job = context.job
    session_id = job.data['session_id']
    user_id = job.data['user_id']

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    # Проверяем, активна ли ещё сессия
    if not session or session.get('status') != 'active':
        return

    # Проверяем, не отправляли ли уже уведомление
    if session.get('expiry_warning_sent'):
        return

    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ <b>Внимание!</b>\n\n"
                 f"Сессия жалобы <b>{complaint_number}</b> будет автоматически закрыта "
                 f"через {PCH_WARNING_BEFORE_HOURS} часов.\n\n"
                 "Завершите работу с жалобой или она будет сохранена в текущем состоянии.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "▶️ Продолжить работу",
                    callback_data=f"pch_continue_{session_id}"
                )]
            ])
        )

        # Отмечаем, что уведомление отправлено
        await manager.update_session(expiry_warning_sent=True)
        logging.info(f"[PCH] Отправлено уведомление о закрытии сессии #{session_id}")

    except Exception as e:
        logging.warning(f"[PCH] Не удалось отправить уведомление пользователю {user_id}: {e}")


async def pch_auto_close_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автоматически закрывает сессию по истечении времени."""
    job = context.job
    session_id = job.data['session_id']
    user_id = job.data['user_id']

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    # Проверяем, активна ли ещё сессия
    if not session or session.get('status') != 'active':
        return

    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    # Закрываем сессию
    await manager.close_session('expired')

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ <b>Сессия закрыта</b>\n\n"
                 f"Жалоба <b>{complaint_number}</b> была автоматически закрыта "
                 f"по истечении {PCH_SESSION_TIMEOUT_HOURS} часов.\n\n"
                 "Вы можете просмотреть сохранённые материалы в истории жалоб.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 История жалоб", callback_data="pch_history")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        logging.info(f"[PCH] Сессия #{session_id} автоматически закрыта")

    except Exception as e:
        logging.warning(f"[PCH] Не удалось уведомить пользователя {user_id} о закрытии: {e}")
