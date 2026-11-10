"""Разбор жалоб в прокуратуру (Prosecution Complaint Handler).

Раньше всё было в одном prosecution_complaint_flow.py (~3200 строк). Разбито так:

    session       менеджер сессии в БД, шаблоны форм, номер жалобы
    ui            клавиатуры и тексты этапов
    pipeline      AI-конвейер: законы, парсинг жалобы, фазы анализа, документы, перевод в УД
    intake        старт: проект, сервер, ссылка, анкета, запуск анализа
    stages        навигация по этапам
    documents     история, документы, доп. материалы, завершение и отмена
    jobs          предупреждение об истечении и автозакрытие сессии
    conversation  сборка ConversationHandler
"""

from .conversation import get_pch_conv_handler, pch_conv_handler

__all__ = ['get_pch_conv_handler', 'pch_conv_handler']
