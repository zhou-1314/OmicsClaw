"""The ``python`` tool's own seams, with a fake runner.

The tool is a leaf: what it owns is argument validation, the approval
gate, the session id it reads from the tool context, and the streaming
hop from the runner's worker thread to the progress channel. The kernel
itself belongs to ``tests/kernel``; nothing here starts a process.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from omicsclaw.tools.base import ApprovalMode, RiskLevel
from omicsclaw.tools.builtin.python_kernel import (
    DEFAULT_TIMEOUT,
    ENGINE_TIMEOUT_MARGIN,
    PYTHON_SCHEMA,
    PythonKernelTool,
)
from omicsclaw.tools.context import (
    ApprovalDecision,
    ApprovalRequest,
    use_tool_context,
)


class _RecordingRunner:
    """The KernelRunner double: one method, called with what the tool sent."""

    def __init__(self, answer: str = "ran") -> None:
        self.answer = answer
        self.calls: list[dict] = []
        self.outputs: list[str] = []

    async def run_python(self, code, *, description, session_id, on_output=None,
                         timeout_s=None) -> str:
        self.calls.append(
            {
                "code": code,
                "description": description,
                "session_id": session_id,
                "timeout_s": timeout_s,
            }
        )
        if on_output is not None:
            on_output("streamed line\n")
            self.outputs.append("streamed line\n")
        return self.answer


def _auto_approve(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=True, reason="")


def test_the_tool_passes_code_session_and_stream_to_the_runner():
    runner = _RecordingRunner()
    tool = PythonKernelTool(runner=runner)

    async def scenario() -> str:
        with use_tool_context(approval=_auto_approve, values={"session_id": "tool-s1"}):
            return await tool.execute(json.dumps({"code": "x = 1"}))

    assert asyncio.run(scenario()) == "ran"
    assert runner.calls == [
        {
            "code": "x = 1",
            "description": "",
            "session_id": "tool-s1",
            "timeout_s": None,
        }
    ]
    assert runner.outputs == ["streamed line\n"]


def test_the_default_session_when_the_context_has_none():
    runner = _RecordingRunner()
    tool = PythonKernelTool(runner=runner)

    async def scenario() -> str:
        with use_tool_context(approval=_auto_approve):
            return await tool.execute(json.dumps({"code": "pass", "description": "d"}))

    asyncio.run(scenario())
    assert runner.calls[0]["session_id"] == "default"
    assert runner.calls[0]["description"] == "d"


def test_a_denied_approval_never_reaches_the_runner():
    runner = _RecordingRunner()
    tool = PythonKernelTool(runner=runner)

    def deny(request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=False, reason="not today")

    async def scenario():
        with use_tool_context(approval=deny):
            await tool.execute(json.dumps({"code": "rm = 'anything'"}))

    with pytest.raises(Exception) as raised:
        asyncio.run(scenario())
    assert runner.calls == []
    assert "not today" in str(raised.value)


def test_a_missing_runner_refuses_at_execution_time():
    tool = PythonKernelTool()

    async def scenario():
        with use_tool_context(approval=_auto_approve):
            await tool.execute(json.dumps({"code": "pass"}))

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(scenario())
    assert "kernel runner" in str(raised.value)


def test_argument_validation_refuses_shape_and_whitespace():
    tool = PythonKernelTool(runner=_RecordingRunner())

    async def refuse(arguments: dict) -> str:
        with use_tool_context(approval=_auto_approve):
            return await tool.execute(json.dumps(arguments))

    with pytest.raises(Exception):
        asyncio.run(refuse({"description": "no code"}))
    with pytest.raises(Exception):
        asyncio.run(refuse({"code": "   "}))
    with pytest.raises(Exception):
        asyncio.run(refuse({"code": "pass", "mystery": True}))


def test_the_policy_is_the_bash_category():
    policy = PythonKernelTool(runner=None).policy
    assert policy.risk_level is RiskLevel.HIGH
    assert policy.approval_mode is ApprovalMode.ASK
    assert policy.prompts_for_itself
    assert not policy.concurrency_safe


def test_the_engine_budget_arithmetic_bash_set():
    """45 + 15 <= the engine's 60: the coupling ``bash.py`` documents,
    re-asserted for this tool because it borrowed the numbers."""
    assert DEFAULT_TIMEOUT + ENGINE_TIMEOUT_MARGIN <= 60.0


def test_the_schema_declares_only_the_three_arguments():
    assert set(PYTHON_SCHEMA["properties"]) == {"code", "description", "timeout_secs"}
    assert PYTHON_SCHEMA["required"] == ["code"]
    assert PYTHON_SCHEMA["additionalProperties"] is False
