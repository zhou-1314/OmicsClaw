"""Per-request loop state for the agent decision/tool loop.

Per ADR 0007. ``LoopState`` carries iteration counters, bounded tool-call
and error histories, and accumulated pathology signals for one
invocation of ``run_query_engine``. The state is constructed inside the
function call and discarded when the call returns — no module-level
mutable state is introduced.

The bounded deques cap memory under long sessions. The unbounded
``signals`` list is safe because pathology signals are rare by
construction (they fire only when a threshold is crossed) and they are
useful for postmortem telemetry.

``ToolCallRecord.args_digest`` stores a SHA-1 hex of the JSON-canonical
argument dict rather than the raw arguments themselves. This bounds
state size under tools that accept large payloads (file contents, MCP
binary args) while preserving the granularity needed to distinguish
``grep(pattern="A")`` from ``grep(pattern="B")``.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal


_TOOL_CALL_HISTORY_MAXLEN = 20
_ERROR_HISTORY_MAXLEN = 10


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool invocation observed during the loop."""

    name: str
    args_digest: str
    iteration: int
    succeeded: bool


@dataclass(frozen=True, slots=True)
class ToolErrorRecord:
    """One tool failure observed during the loop."""

    tool_name: str
    iteration: int
    error_class: str
    message_head: str


@dataclass(frozen=True, slots=True)
class PathologySignal:
    """A detector found an unhealthy pattern in the loop history."""

    kind: Literal["pingpong", "repeated_failure"]
    tool_name: str | None
    iteration: int
    count: int
    reason: str


@dataclass(slots=True)
class LoopState:
    """Mutable carrier of loop progress for one ``run_query_engine`` call."""

    iteration: int = 0
    tool_calls: deque[ToolCallRecord] = field(
        default_factory=lambda: deque(maxlen=_TOOL_CALL_HISTORY_MAXLEN)
    )
    errors: deque[ToolErrorRecord] = field(
        default_factory=lambda: deque(maxlen=_ERROR_HISTORY_MAXLEN)
    )
    signals: list[PathologySignal] = field(default_factory=list)


def compute_args_digest(arguments: Any) -> str:
    """Return a stable SHA-1 hex digest of tool arguments.

    Uses ``json.dumps(sort_keys=True, default=str)`` so that nested
    dicts with reordered keys produce the same digest, and so that
    non-JSON-serialisable values (paths, datetimes) degrade to their
    ``str()`` repr rather than raising.

    A non-dict, non-list ``arguments`` is digested via its ``repr``. An
    empty dict / ``None`` produces a stable, well-defined digest — the
    detector wants every call to land in *some* bucket.
    """
    try:
        canonical = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(arguments)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ToolCallRecord",
    "ToolErrorRecord",
    "PathologySignal",
    "LoopState",
    "compute_args_digest",
]
