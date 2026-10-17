"""Provide session persistence and long-term recall.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from .archive import ArchivedMessage, SqliteCompactionArchive
from .compaction_log import JsonlCompactionLog, LoggedCompaction, record_from_dict
from .database import SCHEMA, Database
from .extractor import (
    EXTRACTION_SYSTEM_PROMPT,
    ExtractionResult,
    MemoryExtractor,
    parse_facts,
    render_conversation,
)
from .longterm import Category, MemoryEntry, normalize, signature
from .notebook import (
    ExecutionConflict,
    ExecutionRecord,
    NotebookCell,
    NotebookStore,
    RevisionConflict,
)
from .offload import FileOffloadStore, safe_name
from .precis import (
    PRECIS_MAX_BYTES,
    PRECIS_MAX_ENTRIES,
    Precis,
    render,
    truncate_utf8,
)
from .record import StoredSession
from .scratch import SCRATCH_KIND_TRIED_SOLUTIONS, SessionScratch
from .sessions import SqliteSessionStore
from .store import STALE_AFTER_DAYS, STALE_MAX_IMPORTANCE, LongTermStore

__all__ = [
    "ArchivedMessage",
    "EXTRACTION_SYSTEM_PROMPT",
    "PRECIS_MAX_BYTES",
    "PRECIS_MAX_ENTRIES",
    "SCHEMA",
    "SCRATCH_KIND_TRIED_SOLUTIONS",
    "STALE_AFTER_DAYS",
    "STALE_MAX_IMPORTANCE",
    "Category",
    "Database",
    "ExtractionResult",
    "FileOffloadStore",
    "JsonlCompactionLog",
    "LoggedCompaction",
    "LongTermStore",
    "NotebookCell",
    "ExecutionConflict",
    "ExecutionRecord",
    "NotebookStore",
    "RevisionConflict",
    "MemoryEntry",
    "MemoryExtractor",
    "Precis",
    "SessionScratch",
    "SqliteCompactionArchive",
    "SqliteSessionStore",
    "StoredSession",
    "normalize",
    "parse_facts",
    "record_from_dict",
    "render",
    "render_conversation",
    "safe_name",
    "signature",
    "truncate_utf8",
    "DurableTurnEvent",
    "DurableTurnEventLog",
    "EventLogConflict",
    "StreamOwnerConflict",
    "cells_to_ipynb",
    "export_ipynb",
    "import_ipynb",
    "write_ipynb",
]

from .notebook_io import cells_to_ipynb, export_ipynb, import_ipynb, write_ipynb
from .turn_events import (
    DurableTurnEvent,
    DurableTurnEventLog,
    EventLogConflict,
    StreamOwnerConflict,
)
