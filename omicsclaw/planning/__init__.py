"""Expose persistent plan state, plan tools and per-call plan injection.

Plans live outside conversation history so compaction need not remove the
task state. Composition supplies the archive and binds an injector to the
current session.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from .archive import (
    FilePlanArchive,
    PlanArchive,
    PlanArchiveError,
    dump_items,
    load_items,
    safe_name,
)
from .book import ArchiveErrorSink, PlanBook
from .guidance import PLANNING_GUIDANCE, PLANNING_SECTION_HEADING
from .injector import (
    DEFAULT_GATE_TURNS,
    PLANNING_GATE_TEXT,
    PROGRESS_TOOL_NAMES,
    PlanInjector,
)
from .plan import PlanItem, PlanStatus, PlanStore, PlanWriteSink
from .render import DOCUMENT_TITLE, INJECTION_HEADER, format_plan, render_document
from .rules import MAX_DIRECT_COMPLETIONS, PlanRefused, apply, merge, validate
from .tool import (
    PLAN_WRITE_SCHEMA,
    PLAN_WRITE_TOOL_NAME,
    SESSION_VALUE_KEY,
    plan_write_tool,
)

__all__ = [
    "ArchiveErrorSink",
    "DEFAULT_GATE_TURNS",
    "DOCUMENT_TITLE",
    "FilePlanArchive",
    "INJECTION_HEADER",
    "MAX_DIRECT_COMPLETIONS",
    "PLANNING_GATE_TEXT",
    "PLANNING_GUIDANCE",
    "PLANNING_SECTION_HEADING",
    "PLAN_WRITE_SCHEMA",
    "PLAN_WRITE_TOOL_NAME",
    "PROGRESS_TOOL_NAMES",
    "PlanArchive",
    "PlanArchiveError",
    "PlanBook",
    "PlanInjector",
    "PlanItem",
    "PlanRefused",
    "PlanStatus",
    "PlanStore",
    "PlanWriteSink",
    "SESSION_VALUE_KEY",
    "apply",
    "dump_items",
    "format_plan",
    "load_items",
    "merge",
    "plan_write_tool",
    "render_document",
    "safe_name",
    "validate",
]
