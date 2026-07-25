"""The engine must hand the unknown-tool reporter the names it could dispatch.

Diagnosis 2026-07-25: a guessed tool name (``run_shell``) came back as a bare
"Unknown tool: run_shell", so the model burned its next tool iteration on
``tool_search``. The suggestion machinery lives in the orchestrator, but it is
inert unless the engine tells it which tools actually exist — this pins that
wiring at the engine's own seam.
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
    _FakeLLM,
    _FakeMessage,
    _FakeResponse,
    _FakeToolCall,
)


def _runtime():
    async def executor(args):
        return "ok"

    return ToolRegistry(
        [
            ToolSpec(
                name="run_shell_command",
                description="Run a shell command",
                parameters={"type": "object", "properties": {}},
                read_only=False,
                concurrency_safe=False,
            )
        ]
    ).build_runtime({"run_shell_command": executor})


def test_engine_suggests_the_real_tool_after_a_guessed_name(tmp_path):
    llm = _FakeLLM(
        [
            _FakeResponse(
                _FakeMessage(
                    content="",
                    tool_calls=[_FakeToolCall("call-1", "run_shell", "{}")],
                )
            ),
            _FakeResponse(_FakeMessage(content="recovered", tool_calls=None)),
        ]
    )
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="c1",
                session_id="s",
                system_prompt="SYSTEM",
                user_message_content="run the script",
            ),
            tool_runtime=_runtime(),
            transcript_store=transcript_store,
            tool_result_store=ToolResultStore(storage_dir=tmp_path / "tr"),
            config=QueryEngineConfig(model="fake"),
            callbacks=QueryEngineCallbacks(),
        )
    )

    tool_messages = [
        m["content"] for m in transcript_store.get_history("c1") if m["role"] == "tool"
    ]
    assert tool_messages, "the unknown tool call should still produce a tool message"
    assert "run_shell_command" in tool_messages[0], (
        "the engine must pass the dispatchable tool names through, so the model "
        f"can recover in the same turn; got: {tool_messages[0]!r}"
    )
