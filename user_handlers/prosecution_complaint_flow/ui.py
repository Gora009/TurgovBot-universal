"""Клавиатуры и тексты этапов: прогресс-бар, сообщение этапа, статус сессии."""

from typing import Dict, List
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from config import PCH_STAGES_ORDER, PCH_STAGES_DISPLAY


# === UI ФУНКЦИИ ===

def build_progress_bar(current: int, total: int) -> str:
    """Создаёт текстовый прогресс-бар."""
    filled = current
    empty = total - current
    return '■' * filled + '□' * empty


def build_stage_keyboard(
    current_stage_index: int,
    session_id: int,
    show_accept: bool = True,
    show_doc_button: bool = True,
    available_docs: List[str] = None
) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру карусельного UI для этапов.
    """
    keyboard = []

    # Ряд навигации между этапами
    nav_row = []
    if current_stage_index > 0:
        nav_row.append(InlineKeyboardButton(
            "◀️ Пред.", callback_data=f"pch_prev_{session_id}"
        ))

    stage_type = PCH_STAGES_ORDER[current_stage_index]
    stage_name = PCH_STAGES_DISPLAY.get(stage_type, f"Этап {current_stage_index + 1}")
    nav_row.append(InlineKeyboardButton(
        f"{current_stage_index + 1}/{len(PCH_STAGES_ORDER)}",
        callback_data="pch_noop"
    ))

    if current_stage_index < len(PCH_STAGES_ORDER) - 1:
        nav_row.append(InlineKeyboardButton(
            "След. ▶️", callback_data=f"pch_next_{session_id}"
        ))

    keyboard.append(nav_row)

    # Ряд действий
    if show_accept:
        keyboard.append([
            InlineKeyboardButton("✅ Принять этап", callback_data=f"pch_accept_{session_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton("✏️ Уточнить", callback_data=f"pch_refine_{session_id}"),
            InlineKeyboardButton("🔄 Пересоздать", callback_data=f"pch_regenerate_{session_id}"),
        ])

    # Кнопка генерации документа (если доступны документы на этом этапе)
    if show_doc_button and available_docs:
        keyboard.append([
            InlineKeyboardButton(
                "📄 Создать документ", callback_data=f"pch_gen_doc_{session_id}"
            )
        ])

    # Кнопка добавления материалов
    keyboard.append([
        InlineKeyboardButton(
            "📎 Добавить материал", callback_data=f"pch_add_material_{session_id}"
        )
    ])

    # На последнем этапе - кнопки завершения
    if current_stage_index == len(PCH_STAGES_ORDER) - 1:
        keyboard.append([
            InlineKeyboardButton(
                "🔄 Конвертировать в уголовное дело",
                callback_data=f"pch_convert_ccw_{session_id}"
            )
        ])

    # Кнопки отмены/выхода
    keyboard.append([
        InlineKeyboardButton(
            "📜 История жалоб", callback_data="pch_history"
        ),
        InlineKeyboardButton(
            "❌ Отменить", callback_data=f"pch_cancel_{session_id}"
        ),
    ])

    return InlineKeyboardMarkup(keyboard)


def format_stage_message(
    complaint_number: str,
    stage_index: int,
    stage_content: str,
    ai_review: str = None
) -> str:
    """Форматирует сообщение для отображения этапа."""
    stage_type = PCH_STAGES_ORDER[stage_index]
    stage_name = PCH_STAGES_DISPLAY.get(stage_type, stage_type)
    progress = build_progress_bar(stage_index + 1, len(PCH_STAGES_ORDER))

    message = f"""══════════════════════════════════════════
📋 <b>ЖАЛОБА {complaint_number}</b>
[{progress}] Этап {stage_index + 1} из {len(PCH_STAGES_ORDER)} — {stage_name}
══════════════════════════════════════════

{stage_content}

──────────────────────────────────────────"""

    if ai_review:
        message += f"\n🤖 <b>AI-примечание:</b> {ai_review}\n──────────────────────────────────────────"

    return message


def format_session_status(session: Dict) -> str:
    """Форматирует статус сессии для отображения в списке."""
    status = session.get('status', 'unknown')
    stage_index = session.get('current_stage_index', 0)

    status_emoji = {
        'active': '⏳',
        'completed': '✅',
        'converted_to_case': '🔄',
        'expired': '❌'
    }.get(status, '❓')

    stage_name = ''
    if status == 'active' and stage_index < len(PCH_STAGES_ORDER):
        stage_type = PCH_STAGES_ORDER[stage_index]
        stage_name = f"\n   Этап: {PCH_STAGES_DISPLAY.get(stage_type, stage_type)}"

    linked_case = ''
    if status == 'converted_to_case' and session.get('linked_criminal_case_id'):
        linked_case = f"\n   → Уголовное дело #{session['linked_criminal_case_id']}"

    return f"{status_emoji} {status.replace('_', ' ').title()}{stage_name}{linked_case}"
