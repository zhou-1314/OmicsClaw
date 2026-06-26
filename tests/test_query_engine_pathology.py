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


def _build_execution_tool_runtime():
    """A runtime exposing ``omicsclaw`` — an EXECUTION_TOOLS member — so a
    recovery tool call clears the phantom-completion predicate."""

    async def executor(args):
        return "skill ran: spatial-preprocess complete"

    return ToolRegistry(
        [
            ToolSpec(
                name="omicsclaw",
                description="Run an omics skill",
                parameters={"type": "object", "properties": {}},
                read_only=False,
                concurrency_safe=False,
            )
        ]
    ).build_runtime({"omicsclaw": executor})


def _omicsclaw_call_response():
    return _FakeResponse(
        _FakeMessage(
            content="",
            tool_calls=[_FakeToolCall("call-O", "omicsclaw", '{"query": "preprocess"}')],
        )
    )


def _run(llm, runtime, config, signals, *, chat_id, tmp_path):
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    result = asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id=chat_id,
                session_id="s",
                system_prompt="SYSTEM",
                user_message_content="对这个数据执行预处理分析",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=config,
            callbacks=QueryEngineCallbacks(
                on_pathology_signal=lambda s: signals.append(s)
            ),
        )
    )
    return result, transcript_store


def _corrections(transcript_store, chat_id) -> list[str]:
    return [
        m["content"]
        for m in transcript_store.get_history(chat_id)
        if m["role"] == "user" and "Loop detector:" in m.get("content", "")
    ]


# ── Phantom completion (ADR 0027) ────────────────────────────────────


def test_phantom_completion_nudges_once_then_recovers(tmp_path):
    """Guard on: the model narrates a claim with no tool call, gets nudged
    once, then actually calls the execution tool and finishes."""
    runtime = _build_execution_tool_runtime()
    llm = _FakeLLM(
        [
            _final_response("I will proceed with the preprocessing pipeline now."),
            _omicsclaw_call_response(),
            _final_response("done"),
        ]
    )
    signals: list = []
    result, transcript_store = _run(
        llm,
        runtime,
        QueryEngineConfig(
            model="fake",
            llm_error_types=(_FakeAPIError,),
            phantom_completion_guard=True,
        ),
        signals,
        chat_id="chat-phantom-recover",
        tmp_path=tmp_path,
    )

    assert result == "done"
    assert len(signals) == 1
    assert signals[0].kind == "phantom_completion"
    assert signals[0].tool_name is None

    corrections = _corrections(transcript_store, "chat-phantom-recover")
    assert len(corrections) == 1
    assert "did not call any tool" in corrections[0]

    # The nudge reached the model: the second LLM call saw the correction.
    second_call_contents = [
        m.get("content", "")
        for m in llm.calls[1]["messages"]
        if isinstance(m, dict)
    ]
    assert any("Loop detector:" in c for c in second_call_contents if isinstance(c, str))


def test_phantom_completion_nudges_at_most_once(tmp_path):
    """If the model keeps narrating, only one nudge is injected and its
    final narration is returned (no infinite loop)."""
    runtime = _build_execution_tool_runtime()
    llm = _FakeLLM(
        [
            _final_response("I will run the analysis."),
            _final_response("I will run the analysis again, generating QC."),
        ]
    )
    signals: list = []
    result, transcript_store = _run(
        llm,
        runtime,
        QueryEngineConfig(
            model="fake",
            llm_error_types=(_FakeAPIError,),
            phantom_completion_guard=True,
        ),
        signals,
        chat_id="chat-phantom-stubborn",
        tmp_path=tmp_path,
    )

    assert result == "I will run the analysis again, generating QC."
    assert len(signals) == 1
    assert len(_corrections(transcript_store, "chat-phantom-stubborn")) == 1


def test_phantom_completion_guard_off_leaves_cloud_untouched(tmp_path):
    """Guard off (default / cloud providers): a claiming narration is
    returned immediately, no nudge, no signal."""
    runtime = _build_execution_tool_runtime()
    narration = "I will proceed with the preprocessing pipeline now."
    llm = _FakeLLM([_final_response(narration)])
    signals: list = []
    result, transcript_store = _run(
        llm,
        runtime,
        QueryEngineConfig(model="fake", llm_error_types=(_FakeAPIError,)),
        signals,
        chat_id="chat-phantom-cloud",
        tmp_path=tmp_path,
    )

    assert result == narration
    assert signals == []
    assert _corrections(transcript_store, "chat-phantom-cloud") == []


def test_phantom_completion_silent_after_genuine_execution(tmp_path):
    """Guard on, but an execution tool actually ran: the results summary is
    legitimate, not a phantom — no nudge."""
    runtime = _build_execution_tool_runtime()
    llm = _FakeLLM(
        [
            _omicsclaw_call_response(),
            _final_response("Here are the results of the analysis report: QC passed."),
        ]
    )
    signals: list = []
    result, transcript_store = _run(
        llm,
        runtime,
        QueryEngineConfig(
            model="fake",
            llm_error_types=(_FakeAPIError,),
            phantom_completion_guard=True,
        ),
        signals,
        chat_id="chat-phantom-genuine",
        tmp_path=tmp_path,
    )

    assert "QC passed" in result
    assert signals == []
    assert _corrections(transcript_store, "chat-phantom-genuine") == []


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


# ── Repeated read — same file via different tools/args ───────────────


def _build_read_tool_runtime():
    """A runtime exposing two read-only tools (file_read + grep_files) so the
    repeated-read detector can key on ``ToolCallRecord.target``."""

    async def reader(args):
        return "file contents"

    return ToolRegistry(
        [
            ToolSpec(
                name="file_read",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                },
                read_only=True,
                concurrency_safe=True,
            ),
            ToolSpec(
                name="grep_files",
                description="Grep files",
                parameters={
                    "type": "object",
                    "properties": {
                        "root": {"type": "string"},
                        "glob": {"type": "string"},
                        "pattern": {"type": "string"},
                    },
                },
                read_only=True,
                concurrency_safe=True,
            ),
        ]
    ).build_runtime({"file_read": reader, "grep_files": reader})


def _read_call(call_id: str, name: str, args_json: str):
    return _FakeResponse(
        _FakeMessage(content="", tool_calls=[_FakeToolCall(call_id, name, args_json)])
    )


def test_repeated_read_fires_and_injects_correction(tmp_path):
    """Same report opened via file_read, grep_files, then a line-range
    file_read — three different (name, args_digest) keys — trips the
    repeated-read detector and injects exactly one correction naming the file."""
    runtime = _build_read_tool_runtime()
    llm = _FakeLLM(
        [
            _read_call("c1", "file_read", '{"path": "/x/report.json"}'),
            _read_call(
                "c2",
                "grep_files",
                '{"root": "/x", "glob": "report.json", "pattern": "p"}',
            ),
            _read_call(
                "c3",
                "file_read",
                '{"path": "/x/report.json", "start_line": 1, "end_line": 5}',
            ),
            _final_response("done"),
        ]
    )
    signals: list = []
    result, transcript_store = _run(
        llm,
        runtime,
        QueryEngineConfig(model="fake", llm_error_types=(_FakeAPIError,)),
        signals,
        chat_id="chat-read",
        tmp_path=tmp_path,
    )

    assert result == "done"
    assert len(signals) == 1
    assert signals[0].kind == "repeated_read"
    assert signals[0].target == "/x/report.json"

    corrections = _corrections(transcript_store, "chat-read")
    assert len(corrections) == 1
    assert "read the same resource" in corrections[0]
    assert "/x/report.json" in corrections[0]
