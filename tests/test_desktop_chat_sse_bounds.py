from __future__ import annotations

import json
import sys
import asyncio
from types import SimpleNamespace

import pytest


def _decode_frame(frame: str) -> dict[str, object]:
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    outer = json.loads(frame[6:])
    data = outer["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    return {"type": outer["type"], "data": data}


def _fake_desktop_core(llm_tool_loop):
    return SimpleNamespace(
        init=lambda **kwargs: None,
        llm_tool_loop=llm_tool_loop,
        LLM_PROVIDER_NAME="env",
        OMICSCLAW_MODEL="gpt-test",
        OUTPUT_DIR="/tmp/omicsclaw-test-output",
        _skill_registry=lambda: SimpleNamespace(skills={}),
        get_tool_executors=lambda: {"demo": object()},
        _accumulate_usage=lambda response_usage: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        _get_token_price=lambda model: (0.0, 0.0),
    )


async def _response_chunks(response) -> list[str]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
    return chunks


def _decode_stream(chunks: list[str]) -> list[dict[str, object]]:
    return [
        _decode_frame(line + "\n\n")
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]


def test_oversized_tool_result_has_bounded_visible_projection() -> None:
    from omicsclaw.surfaces.desktop._chat_sse import (
        CHAT_SSE_MAX_FRAME_BYTES,
        render_chat_sse_frame,
    )

    content = "界" * (CHAT_SSE_MAX_FRAME_BYTES // 2)
    frame = render_chat_sse_frame(
        "tool_result",
        {
            "tool_use_id": "call_demo_1234",
            "tool_name": "demo",
            "content": content,
            "media": [{"type": "image", "localPath": "/tmp/plot.png"}],
        },
    )

    assert len(frame.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES
    event = _decode_frame(frame)
    assert event["type"] == "tool_result"
    data = event["data"]
    assert isinstance(data, dict)
    assert data["tool_use_id"] == "call_demo_1234"
    assert data["tool_name"] == "demo"
    assert data["content_truncated"] is True
    assert data["content_size_bytes"] == len(content.encode("utf-8"))
    assert data["media_omitted_count"] == 1
    assert content not in str(data["content"])


def test_tool_result_omits_oversized_media_without_truncating_content() -> None:
    from omicsclaw.surfaces.desktop._chat_sse import (
        CHAT_SSE_MAX_FRAME_BYTES,
        render_chat_sse_frame,
    )

    content = "analysis finished"
    frame = render_chat_sse_frame(
        "tool_result",
        {
            "tool_use_id": "call_demo_5678",
            "tool_name": "demo",
            "content": content,
            "media": [
                {
                    "type": "output_summary",
                    "runDir": "x" * CHAT_SSE_MAX_FRAME_BYTES,
                }
            ],
        },
    )

    assert len(frame.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES
    event = _decode_frame(frame)
    assert event["type"] == "tool_result"
    data = event["data"]
    assert isinstance(data, dict)
    assert data["tool_use_id"] == "call_demo_5678"
    assert data["tool_name"] == "demo"
    assert data["content"] == content
    assert data["content_truncated"] is False
    assert data["content_size_bytes"] == len(content.encode("utf-8"))
    assert data["media_omitted_count"] == 1
    assert "media" not in data


def test_oversized_nonterminal_event_is_an_explicit_omission() -> None:
    from omicsclaw.surfaces.desktop._chat_sse import (
        CHAT_SSE_MAX_FRAME_BYTES,
        render_chat_sse_frame,
    )

    frame = render_chat_sse_frame(
        "thinking",
        "x" * (CHAT_SSE_MAX_FRAME_BYTES + 1),
    )

    assert len(frame.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES
    event = _decode_frame(frame)
    assert event == {
        "type": "event_omitted",
        "data": {
            "omitted_event_type": "thinking",
            "reason": "frame_too_large",
            "data_size_bytes": CHAT_SSE_MAX_FRAME_BYTES + 1,
        },
    }


def test_oversized_error_remains_a_terminal_error_event() -> None:
    from omicsclaw.surfaces.desktop._chat_sse import (
        CHAT_SSE_MAX_FRAME_BYTES,
        render_chat_sse_frame,
    )

    frame = render_chat_sse_frame(
        "error",
        "failure" * CHAT_SSE_MAX_FRAME_BYTES,
    )

    assert len(frame.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES
    event = _decode_frame(frame)
    assert event["type"] == "error"
    assert "omitted" in str(event["data"]).lower()


def test_frame_is_valid_utf8_when_tool_returns_a_lone_surrogate() -> None:
    from omicsclaw.surfaces.desktop._chat_sse import render_chat_sse_frame

    frame = render_chat_sse_frame("text", "bad-surrogate:\ud800")

    wire = frame.encode("utf-8")
    assert wire.decode("utf-8") == frame
    event = _decode_frame(frame)
    assert event["type"] == "text"


@pytest.mark.asyncio
async def test_chat_stream_projects_large_tool_result_then_reaches_done(
    monkeypatch,
) -> None:
    pytest.importorskip("fastapi")

    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop._chat_sse import CHAT_SSE_MAX_FRAME_BYTES

    content = "界" * (CHAT_SSE_MAX_FRAME_BYTES // 2)

    async def fake_llm_tool_loop(**kwargs):
        await kwargs["on_tool_call"]("demo", {"mode": "large"})
        await kwargs["on_tool_result"]("demo", content)
        await kwargs["on_stream_content"]("finished")
        return "finished"

    fake_core = _fake_desktop_core(fake_llm_tool_loop)
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setitem(sys.modules, "omicsclaw.runtime.agent.state", fake_core)
    monkeypatch.setattr(server, "_mcp_load_fn", None, raising=False)

    response = await server.chat_stream(
        server.ChatRequest(
            session_id="bounded-tool-result",
            content="run demo",
            permission_profile="full_access",
        )
    )
    chunks = await _response_chunks(response)

    assert all(
        len(chunk.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES for chunk in chunks
    )
    events = _decode_stream(chunks)
    tool_result = next(event for event in events if event["type"] == "tool_result")
    data = tool_result["data"]
    assert isinstance(data, dict)
    assert data["tool_name"] == "demo"
    assert str(data["tool_use_id"]).startswith("call_demo_")
    assert data["content_truncated"] is True
    assert data["content_size_bytes"] == len(content.encode("utf-8"))
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_chat_stream_queue_backpressures_and_still_delivers_done(
    monkeypatch,
) -> None:
    pytest.importorskip("fastapi")

    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop._chat_sse import CHAT_SSE_QUEUE_MAX_ITEMS
    from omicsclaw.runtime.agent.dispatcher import DISPATCH_EVENT_QUEUE_MAX_ITEMS

    produced = 0
    queue_filled = asyncio.Event()
    total = CHAT_SSE_QUEUE_MAX_ITEMS + 5

    async def fake_llm_tool_loop(**kwargs):
        nonlocal produced
        for index in range(total):
            await kwargs["on_stream_reasoning"](f"reasoning-{index}")
            produced += 1
            if produced == CHAT_SSE_QUEUE_MAX_ITEMS:
                queue_filled.set()
        return ""

    fake_core = _fake_desktop_core(fake_llm_tool_loop)
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setitem(sys.modules, "omicsclaw.runtime.agent.state", fake_core)
    monkeypatch.setattr(server, "_mcp_load_fn", None, raising=False)

    response = await server.chat_stream(
        server.ChatRequest(
            session_id="bounded-backpressure",
            content="think",
            permission_profile="full_access",
        )
    )
    iterator = response.body_iterator
    try:
        first = await anext(iterator)
        await asyncio.wait_for(queue_filled.wait(), timeout=1)
        await asyncio.sleep(0)

        # Eight rendered frames, one event in the dispatch handoff, and the
        # event currently blocked on the Surface queue are the complete bound.
        assert produced <= (
            CHAT_SSE_QUEUE_MAX_ITEMS + DISPATCH_EVENT_QUEUE_MAX_ITEMS + 1
        )
        assert produced < total

        chunks = [first.decode("utf-8") if isinstance(first, bytes) else str(first)]
        async for chunk in iterator:
            chunks.append(
                chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            )
    finally:
        await iterator.aclose()
    events = _decode_stream(chunks)
    assert sum(event["type"] == "thinking" for event in events) == total
    assert produced == total
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_chat_disconnect_wakes_blocked_producer_without_task_leak(
    monkeypatch,
) -> None:
    pytest.importorskip("fastapi")

    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop._chat_sse import CHAT_SSE_QUEUE_MAX_ITEMS

    producer_closed = asyncio.Event()
    queue_filled = asyncio.Event()
    produced = 0

    async def fake_llm_tool_loop(**kwargs):
        nonlocal produced
        try:
            for index in range(CHAT_SSE_QUEUE_MAX_ITEMS * 4):
                await kwargs["on_stream_reasoning"](f"reasoning-{index}")
                produced += 1
                if produced >= CHAT_SSE_QUEUE_MAX_ITEMS:
                    queue_filled.set()
            return ""
        finally:
            producer_closed.set()

    fake_core = _fake_desktop_core(fake_llm_tool_loop)
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setitem(sys.modules, "omicsclaw.runtime.agent.state", fake_core)
    monkeypatch.setattr(server, "_mcp_load_fn", None, raising=False)

    response = await server.chat_stream(
        server.ChatRequest(
            session_id="bounded-disconnect",
            content="think",
            permission_profile="full_access",
        )
    )
    iterator = response.body_iterator
    await anext(iterator)
    await asyncio.wait_for(queue_filled.wait(), timeout=1)

    await iterator.aclose()
    await asyncio.wait_for(producer_closed.wait(), timeout=1)
    await asyncio.sleep(0)

    assert "bounded-disconnect" not in server._active_sessions


@pytest.mark.asyncio
async def test_chat_stream_bounds_skill_log_line_and_then_delivers_done(
    monkeypatch,
) -> None:
    pytest.importorskip("fastapi")

    from omicsclaw.skill.log_stream import skill_log_emitter_var
    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop._chat_sse import CHAT_SSE_MAX_FRAME_BYTES
    from omicsclaw.surfaces.desktop._skill_log_bridge import (
        SKILL_LOG_MAX_LINE_BYTES,
    )

    original = "界" * SKILL_LOG_MAX_LINE_BYTES

    async def fake_llm_tool_loop(**kwargs):
        emitter = skill_log_emitter_var.get()
        assert emitter is not None
        run_id = emitter.begin_skill("demo")
        emitter.emit(run_id, "stderr", original)
        return "finished"

    fake_core = _fake_desktop_core(fake_llm_tool_loop)
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setitem(sys.modules, "omicsclaw.runtime.agent.state", fake_core)
    monkeypatch.setattr(server, "_mcp_load_fn", None, raising=False)

    response = await server.chat_stream(
        server.ChatRequest(
            session_id="bounded-skill-log",
            content="run demo",
            permission_profile="full_access",
        )
    )
    chunks = await _response_chunks(response)

    assert all(
        len(chunk.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES for chunk in chunks
    )
    events = _decode_stream(chunks)
    tool_log = next(event for event in events if event["type"] == "tool_log")
    data = tool_log["data"]
    assert isinstance(data, dict)
    line = data["lines"][0]
    assert line["text_truncated"] is True
    assert line["text_size_bytes"] == len(original.encode("utf-8"))
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_chat_stream_oversized_failure_remains_error_then_done(
    monkeypatch,
) -> None:
    pytest.importorskip("fastapi")

    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop._chat_sse import CHAT_SSE_MAX_FRAME_BYTES

    async def fake_llm_tool_loop(**kwargs):
        raise RuntimeError("x" * (CHAT_SSE_MAX_FRAME_BYTES + 1))

    fake_core = _fake_desktop_core(fake_llm_tool_loop)
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setattr(server, "_desktop_control_runtime", None, raising=False)
    monkeypatch.setitem(sys.modules, "omicsclaw.runtime.agent.state", fake_core)
    monkeypatch.setattr(server, "_mcp_load_fn", None, raising=False)
    monkeypatch.setattr(server.logger, "exception", lambda *args, **kwargs: None)

    response = await server.chat_stream(
        server.ChatRequest(
            session_id="bounded-error",
            content="fail",
            permission_profile="full_access",
        )
    )
    chunks = await _response_chunks(response)

    assert all(
        len(chunk.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES for chunk in chunks
    )
    events = _decode_stream(chunks)
    assert events[-2]["type"] == "error"
    assert "omitted" in str(events[-2]["data"]).lower()
    assert events[-1]["type"] == "done"
