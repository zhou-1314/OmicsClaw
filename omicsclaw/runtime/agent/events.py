"""Typed event union emitted by ``dispatch()``.

Per ADR 0006 Q6. The nine event types correspond 1:1 to what
``llm_tool_loop`` already produces — its seven positional callbacks
plus its return value plus its exception path.

The ``pending_media`` side-channel that the ADR originally listed as a
tenth event was dropped during L2 because both live Surfaces consume it
out-of-band (Channel pops it at end-of-loop, Desktop drains it inside
each ``on_tool_result``) and a dispatcher-level emit forced a race
between the dispatcher's end-of-loop pop and Desktop's mid-loop pop.
Tools continue to mutate ``state.pending_media`` directly; Surfaces
continue to read it directly.

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
    """A tool finished. ``result`` is the raw return value.

    ``metadata`` carries the loop's per-result tags (timed_out, is_error,
    preflight_pending, preflight_payload, ...) that
    ``_build_tool_result_callback_metadata`` assembles before invoking the
    ``on_tool_result`` callback. Surfaces that do not need it can ignore
    the field.
    """

    tool: str
    result: Any
    metadata: dict[str, Any] | None = None


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
class Final:
    """The loop returned a user-facing message. Terminal."""

    text: str
    kind: Literal["normal", "preflight"] = "normal"


@dataclass(frozen=True)
class Error:
    """The loop raised. Terminal."""

    exception: BaseException


@dataclass(frozen=True)
class PathologyDetected:
    """The decision loop tripped a pathology threshold (ADR 0007).

    Emitted in-stream when ``loop_pathology.detect`` returns a new
    ``PathologySignal``. Non-terminal — the loop continues with a
    corrective tool-result injected into the next LLM call; this event
    exists so Surfaces can render a short notice that the agent has
    entered self-correction.
    """

    kind: Literal["pingpong", "repeated_failure"]
    tool_name: str | None
    iteration: int
    count: int
    reason: str


Event = Union[
    ProgressStart,
    ProgressUpdate,
    ToolCall,
    ToolResult,
    StreamContent,
    StreamReasoning,
    ContextCompacted,
    PathologyDetected,
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
    "PathologyDetected",
    "Final",
    "Error",
    "Event",
]
