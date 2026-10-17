"""Expose prompt assembly, token budgeting and history compaction.

Callers supply sections, history and model limits. The engine receives an
assembled conversation; deployment files and session persistence live above
this layer.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

from .budget import (
    PRESSURE_ORDER,
    BudgetReport,
    ContextBudget,
    Pressure,
    at_least,
    measure,
)
from .compaction import (
    CompactionArchiver,
    CompactionPlan,
    CompactionRecord,
    CompactionState,
    DEFAULT_MIN_TAIL,
    MemoryExtractor,
    apply_compaction,
    build_summary_prompt,
    collect_references,
    compact,
    plan_compaction,
)
from .nudge import DEFAULT_MEMORY_NUDGE_TURNS, MEMORY_NUDGE_TEXT, MemoryNudge
from .offload import (
    MAX_REFERENCES,
    OFFLOAD_MARKER,
    REFERENCES_HEADING,
    OffloadEntry,
    OffloadOutcome,
    OffloadStore,
    Offloader,
    is_offloaded,
    offload_key,
    offload_messages,
    parse_placeholder,
    parse_references,
    render_placeholder,
    render_references,
)
from .progressive import ProgressiveCompactor, should_write_back
from .prompt import AssembledPrompt, PromptAssembler, assemble
from .sections import (
    RenderedSection,
    Section,
    SectionSource,
    static,
    text_from_file,
)
from .summary import (
    COMPACTION_MARKER,
    FIRST_TEMPLATE,
    INCREMENTAL_TEMPLATE,
    OFFLOAD_RULE,
    SUMMARY_SYSTEM_PROMPT,
    Anchors,
    Summarizer,
    build_compaction_message,
    is_summary_message,
    parse_anchors_and_summary,
)
from .tokens import (
    TokenCounter,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    estimate_tool_tokens,
    format_token_count,
)
from .volatile import VolatileBlock
from .transcript import (
    MISSING_TOOL_RESULT,
    drop_unanswered_calls,
    emergency_fit,
    fit_to_budget,
    render_for_summary,
    repair_tool_pairs,
    split_head_tail,
)

__all__ = [
    "Anchors",
    "AssembledPrompt",
    "BudgetReport",
    "COMPACTION_MARKER",
    "CompactionArchiver",
    "CompactionPlan",
    "CompactionRecord",
    "CompactionState",
    "ContextBudget",
    "DEFAULT_MEMORY_NUDGE_TURNS",
    "DEFAULT_MIN_TAIL",
    "FIRST_TEMPLATE",
    "INCREMENTAL_TEMPLATE",
    "MAX_REFERENCES",
    "MEMORY_NUDGE_TEXT",
    "MISSING_TOOL_RESULT",
    "MemoryExtractor",
    "MemoryNudge",
    "OFFLOAD_MARKER",
    "OFFLOAD_RULE",
    "OffloadEntry",
    "OffloadOutcome",
    "OffloadStore",
    "Offloader",
    "PRESSURE_ORDER",
    "Pressure",
    "ProgressiveCompactor",
    "PromptAssembler",
    "REFERENCES_HEADING",
    "RenderedSection",
    "SUMMARY_SYSTEM_PROMPT",
    "Section",
    "SectionSource",
    "Summarizer",
    "TokenCounter",
    "VolatileBlock",
    "apply_compaction",
    "assemble",
    "at_least",
    "build_compaction_message",
    "build_summary_prompt",
    "collect_references",
    "compact",
    "drop_unanswered_calls",
    "emergency_fit",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "estimate_tool_tokens",
    "fit_to_budget",
    "format_token_count",
    "is_offloaded",
    "is_summary_message",
    "measure",
    "offload_key",
    "offload_messages",
    "parse_anchors_and_summary",
    "parse_placeholder",
    "parse_references",
    "plan_compaction",
    "render_for_summary",
    "render_placeholder",
    "render_references",
    "repair_tool_pairs",
    "should_write_back",
    "split_head_tail",
    "static",
    "text_from_file",
]
