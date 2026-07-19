from __future__ import annotations

import json

import pytest

from omicsclaw.control.event_hub import TurnEventFrame
from omicsclaw.runtime.agent.events import (
    ContextCompacted,
    Error,
    Final,
    PathologyDetected,
    ProgressStart,
    ProgressUpdate,
    StreamContent,
    StreamReasoning,
    ToolCall,
    ToolResult,
)
from omicsclaw.surfaces.desktop.turn_observation import (
    desktop_turn_event_frame_v1,
    desktop_turn_event_v1,
    render_sse_v1,
)


@pytest.mark.parametrize(
    ("event", "expected_name"),
    [
        (ProgressStart("p", "start"), "progress_start"),
        (ProgressUpdate("p", "update"), "progress_update"),
        (ToolCall("inspect", {"path": "x"}), "tool_call"),
        (ToolResult("inspect", {"ok": True}), "tool_result"),
        (StreamContent("content"), "stream_content"),
        (StreamReasoning("reasoning"), "stream_reasoning"),
        (ContextCompacted({"before": 3}), "context_compacted"),
        (
            PathologyDetected(
                kind="pingpong",
                tool_name="inspect",
                iteration=4,
                count=3,
                reason="loop",
            ),
            "pathology_detected",
        ),
        (Final("done"), "final"),
        (Error(RuntimeError("secret provider detail")), "error"),
    ],
)
def test_desktop_turn_event_codec_is_explicit_and_typed(event, expected_name):
    event_name, payload = desktop_turn_event_v1(event)

    assert event_name == expected_name
    assert payload["type"] == type(event).__name__
    assert "secret provider detail" not in str(payload)


def test_desktop_turn_event_frame_v1_carries_correlation_and_time():
    frame = TurnEventFrame(
        turn_id="a" * 32,
        sequence=7,
        emitted_at_ms=1234,
        event=StreamContent("hello"),
    )

    event_name, payload = desktop_turn_event_frame_v1(frame)

    assert event_name == "stream_content"
    assert payload.model_dump() == {
        "schema_version": 1,
        "turn_id": "a" * 32,
        "sequence": 7,
        "emitted_at_ms": 1234,
        "terminal": False,
        "event": {"type": "StreamContent", "chunk": "hello"},
    }


def test_desktop_turn_event_codec_fails_closed_for_unknown_event():
    with pytest.raises(TypeError, match="unsupported Turn Event type"):
        desktop_turn_event_v1(object())  # type: ignore[arg-type]


def test_desktop_turn_event_wire_normalizes_nonfinite_numbers_and_redacts_secrets():
    event_name, payload = desktop_turn_event_v1(
        ToolResult(
            "score",
            {
                "values": [float("nan"), float("inf"), float("-inf")],
                "openai_api_key": "must-not-leak",
                "auth_token": "must-not-leak",
                "proxy_authorization": "must-not-leak",
                "private_key": "must-not-leak",
                "input_tokens": 42,
            },
            metadata={"authorization": "Bearer must-not-leak"},
        )
    )

    rendered = render_sse_v1(event_name, payload)
    decoded = json.loads(rendered.split("data: ", 1)[1])

    assert decoded["result"]["values"] == ["NaN", "Infinity", "-Infinity"]
    assert decoded["result"]["openai_api_key"] == "[redacted]"
    assert decoded["result"]["auth_token"] == "[redacted]"
    assert decoded["result"]["proxy_authorization"] == "[redacted]"
    assert decoded["result"]["private_key"] == "[redacted]"
    assert decoded["result"]["input_tokens"] == 42
    assert decoded["metadata"]["authorization"] == "[redacted]"
    assert "must-not-leak" not in rendered


def test_desktop_turn_event_wire_rejects_arbitrary_objects_without_stringifying():
    class UnsafeValue:
        def __str__(self) -> str:  # pragma: no cover - must never be invoked
            raise AssertionError("arbitrary __str__ reached the wire")

    with pytest.raises(TypeError, match="unsupported Turn Event wire value"):
        desktop_turn_event_v1(ToolResult("unsafe", UnsafeValue()))


def test_desktop_turn_event_wire_rejects_circular_values():
    circular: list[object] = []
    circular.append(circular)

    with pytest.raises(TypeError, match="circular Turn Event value"):
        desktop_turn_event_v1(ToolResult("circular", circular))


def test_desktop_turn_event_wire_escapes_lone_surrogates_to_valid_utf8_json():
    event_name, payload = desktop_turn_event_v1(StreamContent("bad-\ud800-value"))

    rendered = render_sse_v1(event_name, payload)

    rendered.encode("utf-8")
    decoded = json.loads(rendered.split("data: ", 1)[1])
    assert decoded["chunk"] == "bad-\ud800-value"
