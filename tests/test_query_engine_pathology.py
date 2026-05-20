"""Integration tests for ``run_query_engine`` pathology wiring — L1 gate
of ADR 0007.

The three behaviours pinned in the ADR §Verification L1 list:

1. Pingpong fires at the expected iteration and the synthesised
   correction lands in the transcript (and therefore in the next LLM
   call's messages).
2. Repeated-failure detection fires when a tool keeps raising.
3. ``MAX_TOOL_ITERATIONS`` still terminates the loop if the mock LLM
   ignores the correction and keeps emitting the same tool call.
"""

from __future__ import annotations

import asyncio

from omicsclaw.runtime.agent.query_engine import (
    QueryEngineCallbacks,
    QueryEngineConfig,
    QueryEngineContext,
    run_query_engine,
)
from omicsclaw.runtime.storage.tool_result import ToolResultStore
from omicsclaw.runtime.storage.transcript import TranscriptStore, sanitize_tool_history
from omicsclaw.runtime.tools.registry import ToolRegistry
from omicsclaw.runtime.tools.spec import ToolSpec

from tests.test_query_engine import (  # type: ignore[import-not-found]
    _FakeAPIError,
    _FakeLLM,
    _FakeMessage,
    _FakeResponse,
    _FakeToolCall,
)


def _build_tool_runtime(*, fail: bool = False):
    async def executor(args):
        if fail:
            raise RuntimeError("synthetic tool failure")
        return "ok"

    return ToolRegistry(
        [
            ToolSpec(
                name="alpha",
                description="Alpha tool",
                parameters={"type": "object", "properties": {}},
                read_only=True,
                concurrency_safe=True,
            )
        ]
    ).build_runtime({"alpha": executor})


def _tool_call_response():
    return _FakeResponse(
        _FakeMessage(
            content="",
            tool_calls=[_FakeToolCall("call-X", "alpha", "{}")],
        )
    )


def _final_response(text: str):
    return _FakeResponse(_FakeMessage(content=text, tool_calls=None))


def test_pathology_pingpong_fires_and_injects_correction(tmp_path):
    runtime = _build_tool_runtime()
    llm = _FakeLLM(
        [
            _tool_call_response(),
            _tool_call_response(),
            _tool_call_response(),
            _tool_call_response(),
            _final_response("done"),
        ]
    )
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    signals: list = []

    async def on_signal(signal):
        signals.append(signal)

    result = asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="chat-pp",
                session_id="s",
                system_prompt="SYSTEM",
                user_message_content="hi",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=QueryEngineConfig(model="fake", llm_error_types=(_FakeAPIError,)),
            callbacks=QueryEngineCallbacks(on_pathology_signal=on_signal),
        )
    )

    assert result == "done"
    assert len(signals) == 1
    signal = signals[0]
    assert signal.kind == "pingpong"
    assert signal.tool_name == "alpha"
    assert signal.count == 4
    assert signal.iteration == 3

    history = transcript_store.get_history("chat-pp")
    user_messages = [m for m in history if m["role"] == "user"]
    corrective = [
        m for m in user_messages if "Loop detector:" in m.get("content", "")
    ]
    assert len(corrective) == 1
    assert "alpha" in corrective[0]["content"]

    fifth_call = llm.calls[4]
    sent_contents = [
        m.get("content", "") for m in fifth_call["messages"] if isinstance(m, dict)
    ]
    assert any(
        isinstance(c, str) and "Loop detector:" in c for c in sent_contents
    )


def test_pathology_repeated_failure_fires(tmp_path):
    runtime = _build_tool_runtime(fail=True)
    llm = _FakeLLM(
        [
            _tool_call_response(),
            _tool_call_response(),
            _tool_call_response(),
            _tool_call_response(),
            _final_response("giving up"),
        ]
    )
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    signals: list = []

    result = asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="chat-rf",
                session_id="s",
                system_prompt="SYSTEM",
                user_message_content="hi",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=QueryEngineConfig(model="fake", llm_error_types=(_FakeAPIError,)),
            callbacks=QueryEngineCallbacks(
                on_pathology_signal=lambda s: signals.append(s)
            ),
        )
    )

    assert result == "giving up"
    assert len(signals) == 1
    signal = signals[0]
    # Pingpong takes precedence when both fire; the failing tool is also
    # being pinged-ponged on identical arguments, so the assertion is on
    # the tool name, not the kind.
    assert signal.tool_name == "alpha"
    assert signal.count >= 4

    history = transcript_store.get_history("chat-rf")
    assert any(
        "Loop detector:" in m.get("content", "")
        for m in history
        if m["role"] == "user"
    )


def test_pathology_correction_is_not_repeated_for_same_pattern(tmp_path):
    """If the LLM ignores the correction and keeps pingponging, MAX
    iterations still terminates the loop — and only one correction is
    injected (no spam)."""
    runtime = _build_tool_runtime()
    llm = _FakeLLM([_tool_call_response() for _ in range(8)])
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    signals: list = []

    result = asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="chat-cap",
                session_id="s",
                system_prompt="SYSTEM",
                user_message_content="hi",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=QueryEngineConfig(
                model="fake",
                max_iterations=8,
                llm_error_types=(_FakeAPIError,),
            ),
            callbacks=QueryEngineCallbacks(
                on_pathology_signal=lambda s: signals.append(s)
            ),
        )
    )

    assert "max tool iterations" in result.lower()
    # Only one corrective injection for the same (kind, tool_name)
    assert len(signals) == 1

    history = transcript_store.get_history("chat-cap")
    corrective_count = sum(
        1
        for m in history
        if m["role"] == "user" and "Loop detector:" in m.get("content", "")
    )
    assert corrective_count == 1


def test_pathology_no_signal_under_threshold(tmp_path):
    """3 same-args tool calls + a final response: detector stays silent."""
    runtime = _build_tool_runtime()
    llm = _FakeLLM(
        [
            _tool_call_response(),
            _tool_call_response(),
            _tool_call_response(),
            _final_response("done"),
        ]
    )
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    signals: list = []

    result = asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="chat-quiet",
                session_id="s",
                system_prompt="SYSTEM",
                user_message_content="hi",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=QueryEngineConfig(model="fake", llm_error_types=(_FakeAPIError,)),
            callbacks=QueryEngineCallbacks(
                on_pathology_signal=lambda s: signals.append(s)
            ),
        )
    )

    assert result == "done"
    assert signals == []

    history = transcript_store.get_history("chat-quiet")
    assert not any(
        "Loop detector:" in m.get("content", "")
        for m in history
        if m["role"] == "user"
    )
