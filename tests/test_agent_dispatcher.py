"""Unit tests for ``omicsclaw.runtime.agent.dispatcher.dispatch`` — L0 gate
of ADR 0006.

The tests substitute a controllable double for ``llm_tool_loop`` and
assert that every callback the loop fires shows up as the right typed
event, that the return value lands as ``Final``, that raised exceptions
land as ``Error``, and that the ``pending_media`` / preflight side-
channels are drained into events.
"""

from __future__ import annotations

import asyncio

import pytest

import omicsclaw.runtime.agent.state as _core
from omicsclaw.runtime.agent.dispatcher import dispatch
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import (
    ContextCompacted,
    Error,
    Final,
    PendingMedia,
    ProgressStart,
    ProgressUpdate,
    StreamContent,
    StreamReasoning,
    ToolCall,
    ToolResult,
)


def _patch_llm_tool_loop(monkeypatch, fake):
    """Replace the ``llm_tool_loop`` symbol the dispatcher imports lazily."""
    monkeypatch.setattr(
        "omicsclaw.runtime.agent.loop.llm_tool_loop",
        fake,
        raising=True,
    )


async def _collect(envelope: MessageEnvelope):
    return [event async for event in dispatch(envelope)]


@pytest.mark.asyncio
async def test_return_value_lands_as_final(monkeypatch):
    async def fake_loop(**_kwargs):
        return "hello"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert events == [Final(text="hello", kind="normal")]


@pytest.mark.asyncio
async def test_empty_return_yields_final_with_empty_text(monkeypatch):
    async def fake_loop(**_kwargs):
        return None

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert events == [Final(text="", kind="normal")]


@pytest.mark.asyncio
async def test_progress_callbacks_translate_to_events(monkeypatch):
    async def fake_loop(**kwargs):
        handle = await kwargs["progress_fn"]("starting")
        await kwargs["progress_update_fn"](handle, "halfway")
        await kwargs["progress_update_fn"](handle, "done")
        return "ok"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))

    assert isinstance(events[0], ProgressStart)
    pid = events[0].progress_id
    assert events[0].text == "starting"
    assert events[1] == ProgressUpdate(progress_id=pid, text="halfway")
    assert events[2] == ProgressUpdate(progress_id=pid, text="done")
    assert events[3] == Final(text="ok", kind="normal")


@pytest.mark.asyncio
async def test_progress_ids_are_unique_per_call(monkeypatch):
    async def fake_loop(**kwargs):
        a = await kwargs["progress_fn"]("a")
        b = await kwargs["progress_fn"]("b")
        return f"{a}-{b}"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    starts = [e for e in events if isinstance(e, ProgressStart)]
    assert len(starts) == 2
    assert starts[0].progress_id != starts[1].progress_id


@pytest.mark.asyncio
async def test_tool_callbacks_translate_to_events(monkeypatch):
    async def fake_loop(**kwargs):
        await kwargs["on_tool_call"]("run_skill", {"name": "preprocess"})
        await kwargs["on_tool_result"]("run_skill", {"status": "ok"})
        return "done"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert events[0] == ToolCall(tool="run_skill", arguments={"name": "preprocess"})
    assert events[1] == ToolResult(tool="run_skill", result={"status": "ok"})
    assert events[2] == Final(text="done", kind="normal")


@pytest.mark.asyncio
async def test_stream_callbacks_translate_to_events(monkeypatch):
    async def fake_loop(**kwargs):
        await kwargs["on_stream_content"]("hel")
        await kwargs["on_stream_content"]("lo")
        await kwargs["on_stream_reasoning"]("...")
        return "hello"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert events[0] == StreamContent(chunk="hel")
    assert events[1] == StreamContent(chunk="lo")
    assert events[2] == StreamReasoning(chunk="...")
    assert events[3] == Final(text="hello", kind="normal")


@pytest.mark.asyncio
async def test_context_compacted_translates_to_event(monkeypatch):
    async def fake_loop(**kwargs):
        await kwargs["on_context_compacted"]({"before": 100, "after": 30})
        return "ok"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert events[0] == ContextCompacted(payload={"before": 100, "after": 30})
    assert events[1] == Final(text="ok", kind="normal")


@pytest.mark.asyncio
async def test_exception_lands_as_error(monkeypatch):
    boom = RuntimeError("LLM unreachable")

    async def fake_loop(**_kwargs):
        raise boom

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert events[0].exception is boom


@pytest.mark.asyncio
async def test_pending_media_drained_into_event(monkeypatch):
    async def fake_loop(**_kwargs):
        _core.pending_media["c1"] = [{"path": "/tmp/x.png", "filename": "x.png"}]
        return "done"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
    assert isinstance(events[0], PendingMedia)
    assert events[0].items == [{"path": "/tmp/x.png", "filename": "x.png"}]
    assert events[1] == Final(text="done", kind="normal")
    assert "c1" not in _core.pending_media


@pytest.mark.asyncio
async def test_pending_media_str_key_fallback(monkeypatch):
    async def fake_loop(**_kwargs):
        _core.pending_media["42"] = [{"path": "/tmp/y.png", "filename": "y.png"}]
        return "done"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    events = await _collect(MessageEnvelope(chat_id=42, content="hi"))
    media = [e for e in events if isinstance(e, PendingMedia)]
    assert len(media) == 1
    assert media[0].items[0]["filename"] == "y.png"
    assert "42" not in _core.pending_media


@pytest.mark.asyncio
async def test_final_kind_preflight_when_pending_preflight_set(monkeypatch):
    async def fake_loop(**_kwargs):
        _core.pending_preflight_requests["c1"] = {"need": "input"}
        return "Please answer X"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    try:
        events = await _collect(MessageEnvelope(chat_id="c1", content="hi"))
        finals = [e for e in events if isinstance(e, Final)]
        assert finals == [Final(text="Please answer X", kind="preflight")]
    finally:
        _core.pending_preflight_requests.pop("c1", None)


@pytest.mark.asyncio
async def test_envelope_fields_passed_through(monkeypatch):
    captured: dict = {}

    async def fake_loop(**kwargs):
        captured.update(kwargs)
        return "ok"

    _patch_llm_tool_loop(monkeypatch, fake_loop)
    envelope = MessageEnvelope(
        chat_id="c1",
        content="hi",
        user_id="u",
        platform="cli",
        workspace="/tmp/ws",
        output_style="markdown",
        model_override="claude-opus-4-7",
        max_tokens_override=4096,
        mode="ask",
    )
    await _collect(envelope)
    assert captured["chat_id"] == "c1"
    assert captured["user_content"] == "hi"
    assert captured["user_id"] == "u"
    assert captured["platform"] == "cli"
    assert captured["workspace"] == "/tmp/ws"
    assert captured["output_style"] == "markdown"
    assert captured["model_override"] == "claude-opus-4-7"
    assert captured["max_tokens_override"] == 4096
    assert captured["mode"] == "ask"


@pytest.mark.asyncio
async def test_early_break_cancels_loop_task(monkeypatch):
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def fake_loop(**kwargs):
        await kwargs["progress_fn"]("starting")
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "should not reach"

    _patch_llm_tool_loop(monkeypatch, fake_loop)

    async for event in dispatch(MessageEnvelope(chat_id="c1", content="hi")):
        if isinstance(event, ProgressStart):
            await started.wait()
            break

    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    assert cancelled.is_set()
