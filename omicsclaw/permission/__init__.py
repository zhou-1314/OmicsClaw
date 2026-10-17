"""Control whether tool calls receive permission to execute.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from .danger import (
    COMMAND_ARGUMENT,
    DEFAULT_DANGER_PATTERNS,
    DangerPattern,
    DangerPatterns,
)
from .gate import (
    DecisionSource,
    GatedTool,
    PermissionDenied,
    PermissionGate,
    Resolution,
    gate_tools,
)
from .modes import PermissionMode
from .rules import (
    CONFIG_KEY,
    PermissionConfigError,
    Rule,
    Rules,
    RuleStore,
    Verdict,
    literal_pattern,
    load_rules,
    principal_argument,
    principal_key,
    save_rules,
)

__all__ = [
    "COMMAND_ARGUMENT",
    "CONFIG_KEY",
    "DEFAULT_DANGER_PATTERNS",
    "DangerPattern",
    "DangerPatterns",
    "DecisionSource",
    "GatedTool",
    "PermissionConfigError",
    "PermissionDenied",
    "PermissionGate",
    "PermissionMode",
    "Resolution",
    "Rule",
    "RuleStore",
    "Rules",
    "Verdict",
    "gate_tools",
    "literal_pattern",
    "load_rules",
    "principal_argument",
    "principal_key",
    "save_rules",
]
