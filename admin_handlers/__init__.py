"""Админ-панель Telegram-бота.

Раньше всё жило в одном admin_handlers.py (~6000 строк). Разбито по разделам панели:

    core             вход в панель, главное меню, ввод списков пользователей
    users            баны, HWID, универсальные ключи, ВИП, тестеры, техработы, админы
    user_search      поиск пользователей, карточка, дела
    servers          серверы, обновление законов, прецеденты
    limits           лимиты пользователей
    reports          репорты
    broadcast        рассылки
    monitoring       мониторинг Vertex AI, очистка Google Drive
    api_keys         REST API и ключи
    promo            промо-акция
    loophole         анализатор дыр в законах
    tester_apps      заявки в тестировщики
    vip_apps         заявки на ВИП
    service_account  смена сервис-аккаунта Vertex/Gemini
    conversation     сборка admin_conv_handler

Здесь реэкспортируется то, что используют main.py и другие модули.
"""

from .broadcast import cancel_running_broadcast
from .conversation import admin_conv_handler
from .core import admin_panel, handle_forwarded_message
from .servers import bypass_kb_protection_handler
from .tester_apps import invite_testers_command
from .vip_apps import (
    accept_vip_application,
    handle_vip_revision_reply,
    reject_vip_application,
    request_vip_info,
    set_vip_channel_command,
    vip_rev_reply_filter,
)

__all__ = [
    'accept_vip_application',
    'admin_conv_handler',
    'admin_panel',
    'bypass_kb_protection_handler',
    'cancel_running_broadcast',
    'handle_forwarded_message',
    'handle_vip_revision_reply',
    'invite_testers_command',
    'reject_vip_application',
    'request_vip_info',
    'set_vip_channel_command',
    'vip_rev_reply_filter',
]
