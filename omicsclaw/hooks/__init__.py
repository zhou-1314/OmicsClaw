"""Expose tool-call hooks and the JSONL audit hook.

Composition wraps mounted tools with hook_tools, then applies the permission
gate. Hook behavior belongs in this layer; the engine only dispatches tools.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

from .audit import (
    SESSION_ID_KEY,
    AuditHook,
    AuditOutcome,
    AuditRecord,
    AuditSink,
    JsonlAuditSink,
    arguments_digest,
    outcome_of,
)
from .base import (
    Hook,
    HookAction,
    HookCall,
    HookDecision,
    HookDenied,
    ToolHook,
    allow,
    deny,
)
from .chain import HookedTool, hook_tools

__all__ = [
    "AuditHook",
    "AuditOutcome",
    "AuditRecord",
    "AuditSink",
    "Hook",
    "HookAction",
    "HookCall",
    "HookDecision",
    "HookDenied",
    "HookedTool",
    "JsonlAuditSink",
    "SESSION_ID_KEY",
    "ToolHook",
    "allow",
    "arguments_digest",
    "deny",
    "hook_tools",
    "outcome_of",
]
