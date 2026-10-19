"""Expose Desktop HTTP routes.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {'CHAT_SSE_MAX_FRAME_BYTES': ('_chat_sse', 'CHAT_SSE_MAX_FRAME_BYTES'),
 'CHAT_SSE_QUEUE_MAX_ITEMS': ('_chat_sse', 'CHAT_SSE_QUEUE_MAX_ITEMS'),
 'render_chat_sse_frame': ('_chat_sse', 'render_chat_sse_frame'),
 'utf8_size': ('_chat_sse', 'utf8_size'),
 'mcp_servers': ('catalog', 'mcp_servers'),
 'skill_catalog': ('catalog', 'skill_catalog'),
 'skill_detail': ('catalog', 'skill_detail'),
 'doctor_report': ('doctor', 'doctor_report'),
 'effective_model': ('doctor', 'effective_model'),
 'FILES_SERVE_MAX_BYTES': ('files', 'FILES_SERVE_MAX_BYTES'),
 'FILES_TREE_MAX_NODES': ('files', 'FILES_TREE_MAX_NODES'),
 'file_tree': ('files', 'file_tree'),
 'resolve_in_workspace': ('files', 'resolve_in_workspace'),
 'serve_target': ('files', 'serve_target'),
 'DesktopInteractions': ('interactions', 'DesktopInteractions'),
 'PendingApproval': ('interactions', 'PendingApproval'),
 'PendingQuestion': ('interactions', 'PendingQuestion'),
 'abort_chat': ('interactions', 'abort_chat'),
 'answer_permission': ('interactions', 'answer_permission'),
 'answer_question': ('interactions', 'answer_question'),
 'change_permission_profile': ('interactions', 'change_permission_profile'),
 'DotenvSettings': ('providers', 'DotenvSettings'),
 'SettingsFile': ('providers', 'SettingsFile'),
 'provider_listing': ('providers', 'provider_listing'),
 'provider_settings_updates': ('providers', 'provider_settings_updates'),
 'save_provider': ('providers', 'save_provider'),
 'BACKEND_PROCESS_EPOCH': ('server', 'BACKEND_PROCESS_EPOCH'),
 'ChatStream': ('server', 'ChatStream'),
 'change_workspace': ('server', 'change_workspace'),
 'create_desktop_app': ('server', 'create_desktop_app'),
 'health_payload': ('server', 'health_payload'),
 'is_json_media_type': ('server', 'is_json_media_type'),
 'open_chat_stream': ('server', 'open_chat_stream'),
 'workspace_payload': ('server', 'workspace_payload'),
 'generate_title': ('title', 'generate_title'),
 'KEEPALIVE_INTERVAL_S': ('turn_observation', 'KEEPALIVE_INTERVAL_S'),
 'DesktopChatSSEBody': ('turn_observation', 'DesktopChatSSEBody'),
 'desktop_chat_frame': ('turn_observation', 'desktop_chat_frame'),
 'desktop_terminal_frames': ('turn_observation', 'desktop_terminal_frames'),
 'DEFAULT_MAX_JSON_NESTING': ('turn_submission', 'DEFAULT_MAX_JSON_NESTING'),
 'DEFAULT_MAX_REQUEST_BYTES': ('turn_submission', 'DEFAULT_MAX_REQUEST_BYTES'),
 'ChatStreamRequest': ('turn_submission', 'ChatStreamRequest'),
 'DesktopIngressError': ('turn_submission', 'DesktopIngressError'),
 'decode_chat_stream_request': ('turn_submission', 'decode_chat_stream_request'),
 'parse_chat_stream_document': ('turn_submission', 'parse_chat_stream_document'),
 'DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION': ('wire_contract',
                                           'DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION'),
 'DESKTOP_CHAT_REQUEST_SCHEMA_VERSION': ('wire_contract', 'DESKTOP_CHAT_REQUEST_SCHEMA_VERSION'),
 'DESKTOP_CHAT_SSE_SCHEMA_VERSION': ('wire_contract', 'DESKTOP_CHAT_SSE_SCHEMA_VERSION'),
 'SERVED_PATHS': ('wire_contract', 'SERVED_PATHS'),
 'desktop_chat_contract': ('wire_contract', 'desktop_chat_contract')}

def __getattr__(name: str):
    """Resolve public adapters only when a caller asks for them."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module, attribute = target
    value = getattr(import_module("." + module, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    "BACKEND_PROCESS_EPOCH",
    "CHAT_SSE_MAX_FRAME_BYTES",
    "CHAT_SSE_QUEUE_MAX_ITEMS",
    "DEFAULT_MAX_JSON_NESTING",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION",
    "DESKTOP_CHAT_REQUEST_SCHEMA_VERSION",
    "DESKTOP_CHAT_SSE_SCHEMA_VERSION",
    "FILES_SERVE_MAX_BYTES",
    "FILES_TREE_MAX_NODES",
    "KEEPALIVE_INTERVAL_S",
    "SERVED_PATHS",
    "ChatStream",
    "ChatStreamRequest",
    "DesktopChatSSEBody",
    "DesktopIngressError",
    "DesktopInteractions",
    "DotenvSettings",
    "PendingApproval",
    "PendingQuestion",
    "SettingsFile",
    "abort_chat",
    "answer_permission",
    "answer_question",
    "change_permission_profile",
    "change_workspace",
    "create_desktop_app",
    "decode_chat_stream_request",
    "desktop_chat_contract",
    "desktop_chat_frame",
    "desktop_terminal_frames",
    "doctor_report",
    "effective_model",
    "file_tree",
    "generate_title",
    "health_payload",
    "is_json_media_type",
    "mcp_servers",
    "open_chat_stream",
    "parse_chat_stream_document",
    "provider_listing",
    "provider_settings_updates",
    "render_chat_sse_frame",
    "resolve_in_workspace",
    "save_provider",
    "serve_target",
    "skill_catalog",
    "skill_detail",
    "utf8_size",
    "workspace_payload",
]
