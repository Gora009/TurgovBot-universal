"""Сборка ConversationHandler разбора жалоб в прокуратуру."""

from telegram.ext import ConversationHandler, CallbackQueryHandler, MessageHandler, filters

from config import (
    PCH_CHOOSING_PROJECT,
    PCH_CHOOSING_SERVER,
    PCH_AWAITING_URL,
    PCH_AWAITING_FORM,
    PCH_PHASE_A,
    PCH_USER_REVIEW,
    PCH_NAVIGATE,
    PCH_ADD_MATERIAL,
    PCH_EXPORT,
)
from ..core import unknown_text_fallback

from .documents import (
    complete_session,
    confirm_convert_to_ccw,
    handle_add_material_or_custom_doc,
    handle_cancel,
    handle_continue_session,
    handle_convert_to_ccw,
    handle_generate_document,
    show_add_material_prompt,
    show_custom_document_prompt,
    show_document_selection,
    show_history,
    show_single_document,
    view_generated_documents,
    view_session_details,
)
from .intake import (
    create_new_complaint,
    handle_form_input,
    handle_url_input,
    replace_form_template,
    select_pch_project,
    select_pch_server,
    skip_form_input,
    start_prosecution_complaint,
)
from .stages import (
    _show_completion_screen_handler,
    handle_accept_stage,
    handle_next_stage,
    handle_prev_stage,
    handle_refine_stage,
    handle_refinement_input,
    handle_regenerate_stage,
    show_current_stage,
)


# === CONVERSATION HANDLER ===

def get_pch_conv_handler() -> ConversationHandler:
    """Создаёт ConversationHandler для Prosecution Complaint Handler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
            CallbackQueryHandler(start_prosecution_complaint, pattern=r'^tool_pro_review_complaint$'),
            CallbackQueryHandler(show_history, pattern=r'^tool_pro_complaint_history$'),
            CallbackQueryHandler(handle_continue_session, pattern=r'^pch_continue_\d+$'),
        ],
        states={
            PCH_CHOOSING_PROJECT: [
                CallbackQueryHandler(select_pch_project, pattern=r'^pch_project_'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_CHOOSING_SERVER: [
                CallbackQueryHandler(select_pch_server, pattern=r'^pch_server_'),
                CallbackQueryHandler(select_pch_project, pattern=r'^pch_project_'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_AWAITING_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_input),
                CallbackQueryHandler(replace_form_template, pattern=r'^pch_replace_form$'),
                CallbackQueryHandler(select_pch_project, pattern=r'^pch_project_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_AWAITING_FORM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_form_input),
                CallbackQueryHandler(skip_form_input, pattern=r'^pch_skip_form$'),
                CallbackQueryHandler(select_pch_server, pattern=r'^pch_server_'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_PHASE_A: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refinement_input),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_USER_REVIEW: [
                CallbackQueryHandler(handle_accept_stage, pattern=r'^pch_accept_\d+$'),
                CallbackQueryHandler(handle_refine_stage, pattern=r'^pch_refine_\d+$'),
                CallbackQueryHandler(handle_regenerate_stage, pattern=r'^pch_regenerate_\d+$'),
                CallbackQueryHandler(handle_prev_stage, pattern=r'^pch_prev_\d+$'),
                CallbackQueryHandler(handle_next_stage, pattern=r'^pch_next_\d+$'),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(show_document_selection, pattern=r'^pch_gen_doc_\d+$'),
                CallbackQueryHandler(show_custom_document_prompt, pattern=r'^pch_custom_doc_\d+$'),
                CallbackQueryHandler(handle_generate_document, pattern=r'^pch_doc_[a-z_]+_\d+$'),
                CallbackQueryHandler(show_add_material_prompt, pattern=r'^pch_add_material_\d+$'),
                CallbackQueryHandler(handle_convert_to_ccw, pattern=r'^pch_convert_ccw_\d+$'),
                CallbackQueryHandler(view_generated_documents, pattern=r'^pch_view_docs_\d+$'),
                CallbackQueryHandler(_show_completion_screen_handler, pattern=r'^pch_completion_\d+$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_ADD_MATERIAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_material_or_custom_doc),
                CallbackQueryHandler(show_document_selection, pattern=r'^pch_gen_doc_\d+$'),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_NAVIGATE: [
                CallbackQueryHandler(create_new_complaint, pattern=r'^pch_create_new$'),
                CallbackQueryHandler(show_history, pattern=r'^pch_history$'),
                CallbackQueryHandler(handle_continue_session, pattern=r'^pch_continue_\d+$'),
                CallbackQueryHandler(view_session_details, pattern=r'^pch_view_session_\d+$'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_EXPORT: [
                CallbackQueryHandler(view_generated_documents, pattern=r'^pch_view_docs_\d+$'),
                CallbackQueryHandler(show_single_document, pattern=r'^pch_show_doc_\d+_\d+$'),
                CallbackQueryHandler(handle_convert_to_ccw, pattern=r'^pch_convert_ccw_\d+$'),
                CallbackQueryHandler(confirm_convert_to_ccw, pattern=r'^pch_confirm_ccw_\d+$'),
                CallbackQueryHandler(complete_session, pattern=r'^pch_complete_\d+$'),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(show_history, pattern=r'^pch_history$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
            CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
        ],
        name="prosecution_complaint_handler",
        persistent=False,
        allow_reentry=True,
    )


# Экспортируем handler
pch_conv_handler = get_pch_conv_handler()
