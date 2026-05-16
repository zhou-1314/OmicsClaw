"""Typed event union emitted by ``dispatch()``.

Per ADR 0006 Q6. The ten event types correspond 1:1 to what
``llm_tool_loop`` already produces — its seven positional callbacks plus
its return value, its exception path, and the ``pending_media`` side-
channel that tools mutate during execution.

Surfaces iterate ``dispatch(envelope)`` and render the subset of events
relevant to their output channel. The dispatcher guarantees that exactly
one ``Final`` or ``Error`` event terminates the stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union


@dataclass(frozen=True)
class ProgressStart:
    """A progress message was opened. ``progress_id`` correlates updates."""

    progress_id: str
    text: str


@dataclass(frozen=True)
class ProgressUpdate:
    """Edit a previously opened progress message."""

    progress_id: str
    text: str


@dataclass(frozen=True)
class ToolCall:
    """A tool is about to execute."""

    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """A tool finished. ``result`` is the raw return value."""

    tool: str
    result: Any


@dataclass(frozen=True)
class StreamContent:
    """A token of the final assistant text."""

    chunk: str


@dataclass(frozen=True)
class StreamReasoning:
    """A token of model reasoning / thinking."""

    chunk: str


@dataclass(frozen=True)
class ContextCompacted:
    """The conversation history was compacted mid-loop."""

    payload: dict[str, Any]


@dataclass(frozen=True)
class PendingMedia:
    """Media that tools queued for delivery after the loop returns."""

    items: list[dict[str, Any]]


@dataclass(frozen=True)
class Final:
    """The loop returned a user-facing message. Terminal."""

    text: str
    kind: Literal["normal", "preflight"] = "normal"


@dataclass(frozen=True)
class Error:
    """The loop raised. Terminal."""

    exception: BaseException


Event = Union[
    ProgressStart,
    ProgressUpdate,
    ToolCall,
    ToolResult,
    StreamContent,
    StreamReasoning,
    ContextCompacted,
    PendingMedia,
    Final,
    Error,
]


__all__ = [
    "ProgressStart",
    "ProgressUpdate",
    "ToolCall",
    "ToolResult",
    "StreamContent",
    "StreamReasoning",
    "ContextCompacted",
    "PendingMedia",
    "Final",
    "Error",
    "Event",
]
