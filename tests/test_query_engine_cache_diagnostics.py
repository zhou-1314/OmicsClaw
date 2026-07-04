"""Multi-turn integration tests for prompt-prefix cache diagnostics (ADR 0024).

Drives ``run_query_engine`` across several turns of one chat with a stub LLM
that reports DeepSeek-shaped cache tokens, and asserts the per-turn miss-reason
sequence. This doubles as the Phase 4 regression oracle: a stable prefix must
yield ``cold-start`` then ``none``; a per-turn tool churn must surface as
``tool-list-changed``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.runtime.agent.cache_diagnostics import (
    CACHE_DIAGNOSTICS,
    REASON_COLD_START,
    REASON_HISTORY_SHIFTED,
    REASON_NONE,
    REASON_SYSTEM_CHANGED,
    REASON_TOOL_LIST_CHANGED,
    compute_segment_hash,
)
from omicsclaw.runtime.agent.query_engine import (
    QueryEngineCallbacks,
    QueryEngineConfig,
    QueryEngineContext,
    run_query_engine,
)
from omicsclaw.runtime.context.compaction import ContextCompactionConfig
from omicsclaw.runtime.storage.tool_result import ToolResultStore
from omicsclaw.runtime.storage.transcript import TranscriptStore, sanitize_tool_history
from omicsclaw.runtime.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Minimal stubs (the LLM never calls a tool — one LLM call per turn)
# --------------------------------------------------------------------------- #


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Resp:
    def __init__(self, content, usage):
        self.choices = [SimpleNamespace(message=_Msg(content))]
        self.usage = usage


class _LLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _PromptTooLongThenCapture:
    """Raise a prompt-too-long error on the first call, then succeed — capturing
    the system content actually sent on the successful (post-compaction) retry."""

    def __init__(self, usage):
        self.chat = self
        self.completions = self
        self.calls = 0
        self.sent_system = None
        self._usage = usage

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("maximum context length exceeded")
        self.sent_system = kwargs["messages"][0]["content"]
        return _Resp("ok", self._usage)


def _deepseek_usage(hit, miss):
    return SimpleNamespace(
        prompt_tokens=hit + miss,
        completion_tokens=12,
        total_tokens=hit + miss + 12,
        prompt_cache_hit_tokens=hit,
        prompt_cache_miss_tokens=miss,
    )


TOOLS_A = (
    {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    {"type": "function", "function": {"name": "beta", "parameters": {}}},
)
TOOLS_B = (
    {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    {"type": "function", "function": {"name": "beta", "parameters": {}}},
    {"type": "function", "function": {"name": "gamma", "parameters": {}}},
)


def _run_session(turns, *, chat_id, tmp_path):
    """Run a list of (system_prompt, request_tools, usage) turns; return diagnostics."""
    CACHE_DIAGNOSTICS.reset(chat_id)
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    runtime = ToolRegistry([]).build_runtime({})
    captured = []

    async def on_diag(diag):
        captured.append(diag)

    for index, (system_prompt, request_tools, usage) in enumerate(turns):
        llm = _LLM([_Resp(f"reply-{index}", usage)])
        asyncio.run(
            run_query_engine(
                llm=llm,
                context=QueryEngineContext(
                    chat_id=chat_id,
                    session_id="session-x",
                    system_prompt=system_prompt,
                    user_message_content=f"turn-{index}",
                    request_tools=request_tools,
                ),
                tool_runtime=runtime,
                transcript_store=transcript_store,
                tool_result_store=result_store,
                config=QueryEngineConfig(model="fake-model"),
                callbacks=QueryEngineCallbacks(on_cache_diagnostics=on_diag),
            )
        )
    return captured


def test_stable_prefix_yields_cold_start_then_hits(tmp_path):
    turns = [
        ("SYS", TOOLS_A, _deepseek_usage(0, 1000)),
        ("SYS", TOOLS_A, _deepseek_usage(900, 100)),
        ("SYS", TOOLS_A, _deepseek_usage(950, 50)),
    ]
    diags = _run_session(turns, chat_id="chat-stable", tmp_path=tmp_path)

    assert [d.miss_reason for d in diags] == [
        REASON_COLD_START,
        REASON_NONE,
        REASON_NONE,
    ]
    assert diags[1].hit_ratio == pytest.approx(0.9)
    assert diags[2].hit_ratio == pytest.approx(0.95)
    # Same prefix every turn → identical hashes.
    assert diags[0].tool_hash == diags[1].tool_hash == diags[2].tool_hash
    assert diags[0].system_hash == diags[1].system_hash == diags[2].system_hash
    # Cumulative session hit ratio rolls up.
    assert CACHE_DIAGNOSTICS.session_hit_ratio("chat-stable") == pytest.approx(
        1850 / 3000
    )


def test_tool_list_change_is_attributed(tmp_path):
    turns = [
        ("SYS", TOOLS_A, _deepseek_usage(0, 1000)),
        ("SYS", TOOLS_A, _deepseek_usage(900, 100)),
        ("SYS", TOOLS_B, _deepseek_usage(0, 1000)),  # tool set grew this turn
    ]
    diags = _run_session(turns, chat_id="chat-toolchange", tmp_path=tmp_path)

    assert [d.miss_reason for d in diags] == [
        REASON_COLD_START,
        REASON_NONE,
        REASON_TOOL_LIST_CHANGED,
    ]
    assert diags[1].tool_hash != diags[2].tool_hash


def test_system_change_is_attributed(tmp_path):
    turns = [
        ("SYS", TOOLS_A, _deepseek_usage(0, 1000)),
        ("SYS", TOOLS_A, _deepseek_usage(900, 100)),
        ("SYS-REWARMED", TOOLS_A, _deepseek_usage(0, 1000)),  # e.g. memory write
    ]
    diags = _run_session(turns, chat_id="chat-syschange", tmp_path=tmp_path)

    assert [d.miss_reason for d in diags] == [
        REASON_COLD_START,
        REASON_NONE,
        REASON_SYSTEM_CHANGED,
    ]
    assert diags[1].system_hash != diags[2].system_hash
    assert diags[1].tool_hash == diags[2].tool_hash


def test_history_shifted_when_prefix_stable_but_zero_hit(tmp_path):
    turns = [
        ("SYS", TOOLS_A, _deepseek_usage(0, 1000)),
        ("SYS", TOOLS_A, _deepseek_usage(900, 100)),
        ("SYS", TOOLS_A, _deepseek_usage(0, 1000)),  # stable prefix, yet zero hit
    ]
    diags = _run_session(turns, chat_id="chat-histshift", tmp_path=tmp_path)

    assert [d.miss_reason for d in diags] == [
        REASON_COLD_START,
        REASON_NONE,
        REASON_HISTORY_SHIFTED,
    ]


def test_history_is_append_only_across_turns(tmp_path):
    """ADR 0024 Phase 3 — across turns (no collapse), each turn's sent messages
    are a prefix of the next turn's: history only grows, never slides/rewrites.

    ``max_history=4`` is deliberately tiny: under the old per-turn sliding
    window this would drop the oldest messages and break the prefix relation;
    append-only history keeps it intact.
    """
    from omicsclaw.runtime.storage.transcript import TranscriptStore as _TS

    chat_id = "chat-appendonly"
    CACHE_DIAGNOSTICS.reset(chat_id)
    transcript_store = _TS(max_history=4, sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tr")
    runtime = ToolRegistry([]).build_runtime({})

    sent_per_turn = []
    for i in range(8):
        llm = _LLM([_Resp(f"reply-{i}", _deepseek_usage(200, 20))])
        asyncio.run(
            run_query_engine(
                llm=llm,
                context=QueryEngineContext(
                    chat_id=chat_id,
                    session_id="s",
                    system_prompt="SYS",
                    user_message_content=f"turn-{i}",
                    request_tools=TOOLS_A,
                ),
                tool_runtime=runtime,
                transcript_store=transcript_store,
                tool_result_store=result_store,
                config=QueryEngineConfig(model="fake-model"),
            )
        )
        sent_per_turn.append(llm.calls[0]["messages"])

    # Each turn's sent messages must be an exact prefix of the next turn's.
    for earlier, later in zip(sent_per_turn, sent_per_turn[1:]):
        assert later[: len(earlier)] == earlier, (
            "history is not append-only — a message was dropped or rewritten "
            "between turns (the per-turn slide is back)."
        )
        assert len(later) > len(earlier)


def test_regression_floor_stable_prefix_holds_high_hit_ratio(tmp_path):
    """Phase 4 oracle: with a byte-stable prefix, turns 2+ never regress.

    A future change that re-introduces per-turn tool/system variation would
    flip a turn's reason away from ``none`` and trip this assertion.
    """
    warm = _deepseek_usage(950, 50)
    turns = [("SYS", TOOLS_A, _deepseek_usage(0, 1000))] + [
        ("SYS", TOOLS_A, warm) for _ in range(9)
    ]
    diags = _run_session(turns, chat_id="chat-floor", tmp_path=tmp_path)

    assert diags[0].miss_reason == REASON_COLD_START
    for d in diags[1:]:
        assert d.miss_reason == REASON_NONE
        assert d.hit_ratio >= 0.85


def test_reactive_compaction_emits_hash_of_sent_system_prompt(tmp_path):
    """ADR 0024 (review finding 1) — on a reactive-compaction turn the helper
    rebuilds the system prompt mid-call; the emitted system_hash must match the
    bytes ACTUALLY sent (post-compaction), not the stale pre-compaction value,
    so a collapse re-warm is attributed correctly."""
    chat_id = "chat-reactive-diag"
    CACHE_DIAGNOSTICS.reset(chat_id)
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    for i in range(12):
        transcript_store.append_user_message(chat_id, f"user {i} " + "x" * 800)
        transcript_store.append_assistant_message(chat_id, content=f"assistant {i}")
    result_store = ToolResultStore(storage_dir=tmp_path / "tr")
    runtime = ToolRegistry([]).build_runtime({})
    llm = _PromptTooLongThenCapture(_deepseek_usage(0, 5000))
    captured = []

    asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id=chat_id,
                session_id=None,
                system_prompt="SYS",
                user_message_content="hi",
                request_tools=TOOLS_A,
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=QueryEngineConfig(
                model="fake-model",
                context_compaction=ContextCompactionConfig(
                    max_prompt_tokens=1_000_000,
                    reactive_preserve_messages=4,
                    reactive_preserve_tokens=500,
                ),
            ),
            callbacks=QueryEngineCallbacks(on_cache_diagnostics=lambda d: captured.append(d)),
        )
    )

    assert llm.calls == 2  # 413 on the first attempt, success on the retry
    assert llm.sent_system is not None
    assert "reactive compact context" in llm.sent_system.lower()  # post-compaction
    assert len(captured) == 1
    diag = captured[0]
    # The fix: hash the bytes actually sent (post-compaction), not the stale "SYS".
    assert diag.system_hash == compute_segment_hash(llm.sent_system)
    assert diag.system_hash != compute_segment_hash("SYS")


def test_cache_diagnostics_released_on_transcript_eviction(tmp_path):
    """ADR 0024 (review finding 7) — when a chat's transcript is LRU-evicted,
    its cache-diagnostics state is released too (no unbounded growth)."""
    CACHE_DIAGNOSTICS.clear()
    transcript_store = TranscriptStore(max_conversations=1, sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tr")
    runtime = ToolRegistry([]).build_runtime({})

    def _run(chat_id):
        asyncio.run(
            run_query_engine(
                llm=_LLM([_Resp("ok", _deepseek_usage(10, 1))]),
                context=QueryEngineContext(
                    chat_id=chat_id,
                    session_id=None,
                    system_prompt="SYS",
                    user_message_content="hi",
                    request_tools=TOOLS_A,
                ),
                tool_runtime=runtime,
                transcript_store=transcript_store,
                tool_result_store=result_store,
                config=QueryEngineConfig(model="fake-model"),
            )
        )

    _run("chat-A")
    assert CACHE_DIAGNOSTICS.session_hit_ratio("chat-A") > 0  # recorded
    _run("chat-B")
    _run("chat-C")  # starting chat-C evicts the LRU (chat-A) from the transcript store

    assert "chat-A" not in transcript_store.messages_by_chat  # evicted
    assert CACHE_DIAGNOSTICS.session_hit_ratio("chat-A") == 0.0  # diagnostics released
    assert CACHE_DIAGNOSTICS.session_hit_ratio("chat-C") > 0  # live chat retained
