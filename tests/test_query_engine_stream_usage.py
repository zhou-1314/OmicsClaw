"""Stream usage accounting tests.

Regression cover for the "abnormally high tokens" bug: some providers emit
``chunk.usage`` in every streaming chunk (cumulative), not just in the final
one. The query engine must not accumulate cumulative values — it should record
usage exactly ONCE per LLM call using the last emitted snapshot.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from omicsclaw.runtime.query_engine import (
    QueryEngineCallbacks,
    QueryEngineConfig,
    QueryEngineContext,
    run_query_engine,
)
from omicsclaw.runtime.tool_registry import ToolRegistry
from omicsclaw.runtime.tool_result_store import ToolResultStore
from omicsclaw.runtime.transcript_store import TranscriptStore, sanitize_tool_history


class _FakeStreamChunk:
    def __init__(self, *, delta=None, usage=None):
        self.usage = usage
        if delta is None:
            self.choices = []
        else:
            self.choices = [SimpleNamespace(delta=delta)]


class _FakeStreamResponse:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeLLM:
    def __init__(self, events):
        self._responses = list(events)
        self.calls = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeUsage:
    def __init__(self, *, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


def _run_stream(chunks, accumulate_usage, tmp_path):
    llm = _FakeLLM(events=[_FakeStreamResponse(chunks)])
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    runtime = ToolRegistry([]).build_runtime({})
    return asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="chat-usage",
                session_id="session-usage",
                system_prompt="SYSTEM",
                user_message_content="hello",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=QueryEngineConfig(model="fake-model"),
            callbacks=QueryEngineCallbacks(
                on_stream_content=lambda _chunk: None,
                accumulate_usage=accumulate_usage,
            ),
        )
    )


def test_streamed_usage_is_recorded_once_with_final_values(tmp_path):
    """Cumulative per-chunk usage (Anthropic / LiteLLM style) must not inflate totals."""

    recorded: list[tuple[int, int]] = []

    def accumulate(usage):
        recorded.append((usage.prompt_tokens, usage.completion_tokens))
        return {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }

    chunks = [
        _FakeStreamChunk(
            delta=SimpleNamespace(content="Hel", tool_calls=None),
            usage=_FakeUsage(prompt_tokens=100, completion_tokens=1),
        ),
        _FakeStreamChunk(
            delta=SimpleNamespace(content="lo ", tool_calls=None),
            usage=_FakeUsage(prompt_tokens=100, completion_tokens=3),
        ),
        _FakeStreamChunk(
            delta=SimpleNamespace(content="world", tool_calls=None),
            usage=_FakeUsage(prompt_tokens=100, completion_tokens=5),
        ),
        # Final "finalization" chunk (no delta, usage present — OpenAI spec style).
        _FakeStreamChunk(usage=_FakeUsage(prompt_tokens=100, completion_tokens=5)),
    ]

    result = _run_stream(chunks, accumulate, tmp_path)
    assert result == "Hello world"

    # Must record usage exactly once — with the final cumulative value, not summed.
    assert len(recorded) == 1, f"expected 1 usage callback, got {len(recorded)}: {recorded}"
    assert recorded[0] == (100, 5)


def test_streamed_usage_last_value_wins_when_only_partial_chunks_have_usage(tmp_path):
    """If only some chunks expose usage, the LAST one still wins (OpenAI native case)."""

    recorded: list[tuple[int, int]] = []

    def accumulate(usage):
        recorded.append((usage.prompt_tokens, usage.completion_tokens))

    chunks = [
        _FakeStreamChunk(delta=SimpleNamespace(content="foo", tool_calls=None)),
        _FakeStreamChunk(delta=SimpleNamespace(content="bar", tool_calls=None)),
        _FakeStreamChunk(usage=_FakeUsage(prompt_tokens=42, completion_tokens=7)),
    ]

    _run_stream(chunks, accumulate, tmp_path)

    assert recorded == [(42, 7)]


def test_streamed_usage_not_recorded_when_absent(tmp_path):
    """If no chunk carries usage, we must not invent one."""

    recorded: list = []

    def accumulate(usage):
        recorded.append(usage)

    chunks = [
        _FakeStreamChunk(delta=SimpleNamespace(content="foo", tool_calls=None)),
        _FakeStreamChunk(delta=SimpleNamespace(content="bar", tool_calls=None)),
    ]

    _run_stream(chunks, accumulate, tmp_path)

    assert recorded == []
