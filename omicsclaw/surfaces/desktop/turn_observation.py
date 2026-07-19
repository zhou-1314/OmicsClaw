"""Stable V1 Desktop Adapter for authoritative Turn observation.

This Module owns only HTTP/SSE projection.  Durable truth and cursor recovery
remain behind ``ControlRuntime``; internal Event class names never leak into
the SSE ``event:`` field by reflection.
"""

from __future__ import annotations

from collections import deque
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from omicsclaw.control import ControlTurnObservation, TurnObservationSnapshot
from omicsclaw.control.event_hub import EventObserverDetached, TurnEventFrame
from omicsclaw.runtime.agent.events import (
    ContextCompacted,
    Error,
    Event,
    Final,
    PathologyDetected,
    ProgressStart,
    ProgressUpdate,
    StreamContent,
    StreamReasoning,
    ToolCall,
    ToolResult,
)


_REDACTED = "[redacted]"
_SENSITIVE_WIRE_KEYS = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretaccesskey",
        "secretkey",
        "setcookie",
        "token",
    }
)


def _wire_json_value(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    active_containers: set[int] | None = None,
) -> Any:
    """Project internal values to deterministic, credential-safe JSON.

    Non-finite built-in floats use explicit strings because JSON has no NaN or
    Infinity values. Arbitrary objects and non-string mapping keys fail closed;
    their ``str`` methods are never invoked on the observation wire.
    """

    if depth > 32:
        raise TypeError(f"Turn Event value exceeds maximum depth at {path}")
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value

    if isinstance(value, (dict, list, tuple)):
        containers = active_containers if active_containers is not None else set()
        identity = id(value)
        if identity in containers:
            raise TypeError(f"circular Turn Event value at {path}")
        containers.add(identity)
        try:
            if isinstance(value, dict):
                projected: dict[str, Any] = {}
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise TypeError(
                            f"Turn Event mapping key must be a string at {path}"
                        )
                    normalized_key = "".join(ch for ch in key.lower() if ch.isalnum())
                    projected[key] = (
                        _REDACTED
                        if any(
                            normalized_key == family or normalized_key.endswith(family)
                            for family in _SENSITIVE_WIRE_KEYS
                        )
                        else _wire_json_value(
                            child,
                            path=f"{path}.{key}",
                            depth=depth + 1,
                            active_containers=containers,
                        )
                    )
                return projected
            return [
                _wire_json_value(
                    child,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    active_containers=containers,
                )
                for index, child in enumerate(value)
            ]
        finally:
            containers.remove(identity)

    raise TypeError(f"unsupported Turn Event wire value at {path}")


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DesktopTranscriptRefV1(_StrictWireModel):
    entry_id: str
    content_sha256: str


class DesktopTurnReceiptV1(_StrictWireModel):
    """Read projection; ``project_id`` is owned by the Conversation."""

    schema_version: Literal[1]
    turn_id: str
    conversation_id: str
    project_id: str | None
    turn_kind: Literal["agent", "control_command"]
    status: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "interrupted",
    ]
    retry_of_turn_id: str | None
    terminal_code: str | None
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    revision: int
    transcript_ref: DesktopTranscriptRefV1 | None
    interaction_snapshot: dict[str, Any] | None


class DesktopTurnSnapshotEventV1(_StrictWireModel):
    schema_version: Literal[1]
    receipt: DesktopTurnReceiptV1
    interaction_snapshot: dict[str, Any] | None


class DesktopRetainedRangeV1(_StrictWireModel):
    oldest_sequence: int | None
    latest_sequence: int | None


class DesktopTurnGapEventV1(_StrictWireModel):
    schema_version: Literal[1]
    reason: Literal["cursor_evicted", "cursor_ahead", "buffer_unavailable"]
    requested_after_sequence: int
    retained_range: DesktopRetainedRangeV1
    receipt: DesktopTurnReceiptV1
    interaction_snapshot: dict[str, Any] | None


class DesktopTurnEventFrameV1(_StrictWireModel):
    schema_version: Literal[1]
    turn_id: str
    sequence: int
    emitted_at_ms: int
    terminal: bool
    event: dict[str, Any]


class DesktopTurnCancelResultV1(_StrictWireModel):
    schema_version: Literal[1]
    turn_id: str
    changed: bool
    code: Literal[
        "canceled_waiting",
        "cancel_requested",
        "cancel_already_requested",
        "already_terminal",
    ]
    receipt: DesktopTurnReceiptV1


def desktop_turn_receipt_v1(
    snapshot: TurnObservationSnapshot,
) -> DesktopTurnReceiptV1:
    receipt = snapshot.receipt
    ref = snapshot.transcript_ref
    interaction = (
        dict(snapshot.interaction_snapshot)
        if snapshot.interaction_snapshot is not None
        else None
    )
    return DesktopTurnReceiptV1(
        schema_version=1,
        turn_id=receipt.turn_id,
        conversation_id=receipt.conversation_id,
        project_id=snapshot.project_id,
        turn_kind=receipt.turn_kind,
        status=receipt.status,
        retry_of_turn_id=receipt.retry_of_turn_id,
        terminal_code=receipt.terminal_code,
        created_at_ms=receipt.created_at_ms,
        started_at_ms=receipt.started_at_ms,
        finished_at_ms=receipt.finished_at_ms,
        revision=receipt.revision,
        transcript_ref=(
            DesktopTranscriptRefV1(
                entry_id=ref.entry_id,
                content_sha256=ref.content_sha256,
            )
            if ref is not None
            else None
        ),
        interaction_snapshot=interaction,
    )


def desktop_turn_event_v1(event: Event) -> tuple[str, dict[str, Any]]:
    """Map the closed internal Event union to stable typed wire names."""

    if isinstance(event, ProgressStart):
        return "progress_start", {
            "type": "ProgressStart",
            "progress_id": event.progress_id,
            "text": event.text,
        }
    if isinstance(event, ProgressUpdate):
        return "progress_update", {
            "type": "ProgressUpdate",
            "progress_id": event.progress_id,
            "text": event.text,
        }
    if isinstance(event, ToolCall):
        return "tool_call", {
            "type": "ToolCall",
            "tool": event.tool,
            "arguments": _wire_json_value(event.arguments),
        }
    if isinstance(event, ToolResult):
        return "tool_result", {
            "type": "ToolResult",
            "tool": event.tool,
            "result": _wire_json_value(event.result),
            "metadata": _wire_json_value(event.metadata),
        }
    if isinstance(event, StreamContent):
        return "stream_content", {"type": "StreamContent", "chunk": event.chunk}
    if isinstance(event, StreamReasoning):
        return "stream_reasoning", {
            "type": "StreamReasoning",
            "chunk": event.chunk,
        }
    if isinstance(event, ContextCompacted):
        return "context_compacted", {
            "type": "ContextCompacted",
            "payload": _wire_json_value(event.payload),
        }
    if isinstance(event, PathologyDetected):
        return "pathology_detected", {
            "type": "PathologyDetected",
            "kind": event.kind,
            "tool_name": event.tool_name,
            "iteration": event.iteration,
            "count": event.count,
            "reason": event.reason,
        }
    if isinstance(event, Final):
        return "final", {
            "type": "Final",
            "text": event.text,
            "kind": event.kind,
            "transcript_entry_id": event.transcript_entry_id,
            "transcript_content_sha256": event.transcript_content_sha256,
        }
    if isinstance(event, Error):
        # Exception text and provider detail are intentionally not wire facts.
        return "error", {"type": "Error"}
    raise TypeError(f"unsupported Turn Event type: {type(event).__name__}")


def desktop_turn_event_frame_v1(
    frame: TurnEventFrame,
) -> tuple[str, DesktopTurnEventFrameV1]:
    event_name, event_payload = desktop_turn_event_v1(frame.event)
    return event_name, DesktopTurnEventFrameV1(
        schema_version=1,
        turn_id=frame.turn_id,
        sequence=frame.sequence,
        emitted_at_ms=frame.emitted_at_ms,
        terminal=frame.terminal,
        event=event_payload,
    )


def render_sse_v1(
    event_name: str,
    payload: BaseModel | dict[str, Any],
    *,
    event_id: int | None = None,
) -> str:
    model_body = (
        payload.model_dump(mode="python") if isinstance(payload, BaseModel) else payload
    )
    body = _wire_json_value(model_body)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return (
        f"{prefix}event: {event_name}\n"
        f"data: {json.dumps(body, ensure_ascii=True, allow_nan=False)}\n\n"
    )


class DesktopTurnSSEBody:
    """Close-safe SSE iterator over one already-open Control observation."""

    def __init__(
        self,
        observation: ControlTurnObservation,
        *,
        requested_after_sequence: int,
    ) -> None:
        self._observation = observation
        self._closed = False
        receipt = desktop_turn_receipt_v1(observation.snapshot)
        interaction = (
            dict(observation.snapshot.interaction_snapshot)
            if observation.snapshot.interaction_snapshot is not None
            else None
        )
        self._pending: deque[str] = deque(
            (
                render_sse_v1(
                    "snapshot",
                    DesktopTurnSnapshotEventV1(
                        schema_version=1,
                        receipt=receipt,
                        interaction_snapshot=interaction,
                    ),
                ),
            )
        )
        gap = observation.gap
        if gap is not None:
            self._pending.append(
                render_sse_v1(
                    "gap",
                    DesktopTurnGapEventV1(
                        schema_version=1,
                        reason=gap.reason,
                        requested_after_sequence=gap.requested_after_sequence,
                        retained_range=DesktopRetainedRangeV1(
                            oldest_sequence=gap.oldest_available_sequence,
                            latest_sequence=gap.latest_sequence,
                        ),
                        receipt=receipt,
                        interaction_snapshot=interaction,
                    ),
                )
            )
        elif observation.unavailable_reason is not None:
            self._pending.append(
                render_sse_v1(
                    "gap",
                    DesktopTurnGapEventV1(
                        schema_version=1,
                        reason="buffer_unavailable",
                        requested_after_sequence=requested_after_sequence,
                        retained_range=DesktopRetainedRangeV1(
                            oldest_sequence=None,
                            latest_sequence=None,
                        ),
                        receipt=receipt,
                        interaction_snapshot=interaction,
                    ),
                )
            )

    def __aiter__(self) -> "DesktopTurnSSEBody":
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        if self._pending:
            return self._pending.popleft()
        try:
            frame = await anext(self._observation)
            event_name, payload = desktop_turn_event_frame_v1(frame)
            return render_sse_v1(
                event_name,
                payload,
                event_id=frame.sequence,
            )
        except (StopAsyncIteration, EventObserverDetached):
            await self.aclose()
            raise StopAsyncIteration from None
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._observation.aclose()


__all__ = [
    "DesktopRetainedRangeV1",
    "DesktopTranscriptRefV1",
    "DesktopTurnCancelResultV1",
    "DesktopTurnEventFrameV1",
    "DesktopTurnGapEventV1",
    "DesktopTurnReceiptV1",
    "DesktopTurnSSEBody",
    "DesktopTurnSnapshotEventV1",
    "desktop_turn_event_frame_v1",
    "desktop_turn_event_v1",
    "desktop_turn_receipt_v1",
    "render_sse_v1",
]
