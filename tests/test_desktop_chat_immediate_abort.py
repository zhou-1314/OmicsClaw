from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_abort_after_first_sse_frame_still_terminates_body_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task cancelled before its first coroutine turn must still signal DONE."""

    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    fake_core = SimpleNamespace(
        init=lambda **_kwargs: None,
        LLM_PROVIDER_NAME="test",
        OMICSCLAW_MODEL="test-model",
        OUTPUT_DIR=Path.cwd() / "output",
        _skill_registry=lambda: SimpleNamespace(skills={}),
        get_tool_executors=lambda: {},
        _accumulate_usage=lambda _usage: {},
        _get_token_price=lambda _model: (0.0, 0.0),
    )
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    monkeypatch.setattr(server, "_mcp_load_fn", None, raising=False)

    session_id = "immediate-abort-before-task-start"
    source_request_id = "d" * 32
    response = await server.chat_stream(
        server.ChatRequest(
            session_id=session_id,
            source_request_id=source_request_id,
            content="hello",
        )
    )
    iterator = response.body_iterator

    first = await anext(iterator)
    assert '"type": "status"' in str(first)

    aborted = await server.chat_abort(
        server.AbortRequest(
            session_id=session_id,
            source_request_id=source_request_id,
        )
    )
    assert aborted["status"] == "aborted"

    # mode_changed was already prepared ahead of the queue; the following
    # queue frame must be done rather than an endless keep-alive sequence.
    second = await asyncio.wait_for(anext(iterator), timeout=1)
    third = await asyncio.wait_for(anext(iterator), timeout=1)
    assert '"type": "mode_changed"' in str(second)
    assert '"type": "done"' in str(third)

    await iterator.aclose()
    assert session_id not in server._active_sessions
