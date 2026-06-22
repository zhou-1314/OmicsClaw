"""End-to-end tests for the ADR 0032 mini-agent runner + dispatch wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omicsclaw.autonomous.budget import MiniAgentBudget
from omicsclaw.autonomous.contracts import AutonomousRunRequest
from omicsclaw.autonomous.kernel_session import kernel_ipc_available
from omicsclaw.autonomous.mini_agent_runner import run_mini_agent_request

BUDGET = MiniAgentBudget(wall_clock_seconds=150)
IPC_AVAILABLE = kernel_ipc_available()


def TURN(purpose: str, code: str) -> str:
    return (
        f"**Purpose**: {purpose}\n**Reasoning**: because\n**Next Goal**: next\n"
        f"**Code**:\n```python\n{code}\n```"
    )


class ScriptedLLM:
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0

    def complete(self, prompt, *, temperature=0.0):
        if self._i < len(self._turns):
            t = self._turns[self._i]
            self._i += 1
            return t
        return TURN("idle", "pass")


def _answer_turns():
    return [
        TURN("compute", "val = 21 * 2\nprint('val', val)"),
        TURN("finish", "ReturnAnswer('answer=%d' % val)"),
    ]


def test_runner_end_to_end_with_replay_and_manifest(tmp_path: Path):
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    request = AutonomousRunRequest(goal="compute the answer", output_root=str(tmp_path))
    result = run_mini_agent_request(
        request, llm_client=ScriptedLLM(_answer_turns()), require_sandbox=False, budget=BUDGET
    )

    assert result.ok is True
    assert result.metadata["engine"] == "mini_agent"
    assert result.metadata["replay_ok"] is True
    assert result.metadata["answer"] == "answer=42"
    # provenance + output-shape parity artifacts exist.
    assert Path(result.manifest_path).exists()
    assert Path(result.completion_report_path).exists()
    ws = Path(result.workspace_root)
    assert (ws / "result_summary.md").exists()
    assert (ws / "analysis.py").exists()  # the replay artifact


def test_runner_fails_when_no_answer(tmp_path: Path):
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    request = AutonomousRunRequest(goal="never finishes", output_root=str(tmp_path))
    result = run_mini_agent_request(
        request,
        llm_client=ScriptedLLM([]),  # always idles
        require_sandbox=False,
        budget=MiniAgentBudget(max_steps=2, wall_clock_seconds=120),
    )
    assert result.ok is False
    assert "without an answer" in result.error


def test_fail_closed_without_envelope(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "omicsclaw.autonomous.mini_agent_runner.envelope_available", lambda: False
    )
    request = AutonomousRunRequest(goal="x", output_root=str(tmp_path))
    result = run_mini_agent_request(
        request, llm_client=ScriptedLLM([]), require_sandbox=True
    )
    assert result.ok is False
    assert "bubblewrap" in result.error.lower()
    assert result.metadata.get("fail_closed") is True


def test_dispatch_always_routes_to_mini_agent(tmp_path: Path, monkeypatch):
    # Single engine (ADR 0032): no flag to set — the dispatch must reach the
    # mini-agent unconditionally. The old OMICSCLAW_AUTONOMOUS_MINI_AGENT gate
    # was removed with the consolidation, so deliberately set nothing here.
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    monkeypatch.setenv("OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX", "0")
    monkeypatch.setenv("OMICSCLAW_MINI_AGENT_PROBE", "0")  # routing test, not the gate
    from omicsclaw.autonomous.code_loop import run_autonomous_code_loop_async

    request = AutonomousRunRequest(goal="compute", output_root=str(tmp_path))
    result = asyncio.run(
        run_autonomous_code_loop_async(request, llm_client=ScriptedLLM(_answer_turns()))
    )
    assert result.metadata.get("engine") == "mini_agent"
    assert result.ok is True


def test_dispatch_refuses_incapable_model(tmp_path: Path, monkeypatch):
    """Probe on + incapable model -> clean FAILED refusal, no kernel started."""
    monkeypatch.setenv("OMICSCLAW_MINI_AGENT_PROBE", "1")
    from omicsclaw.autonomous.code_loop import run_autonomous_code_loop_async

    class GarbageLLM:
        def complete(self, prompt, *, temperature=0.0):
            return "I cannot follow any particular format, sorry."

    request = AutonomousRunRequest(goal="x", output_root=str(tmp_path))
    result = asyncio.run(run_autonomous_code_loop_async(request, llm_client=GarbageLLM()))
    assert result.ok is False
    assert result.metadata.get("engine") == "mini_agent"
    assert result.metadata.get("refused") is True
    assert "could not follow" in result.error.lower()


def test_runner_uses_process_guard_without_bwrap(tmp_path: Path, monkeypatch):
    """Tiered isolation: no bwrap -> in-kernel guard tier, run still succeeds."""
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    monkeypatch.setattr(
        "omicsclaw.autonomous.mini_agent_runner.envelope_available", lambda: False
    )
    request = AutonomousRunRequest(goal="compute the answer", output_root=str(tmp_path))
    result = run_mini_agent_request(
        request, llm_client=ScriptedLLM(_answer_turns()), require_sandbox=False, budget=BUDGET
    )
    assert result.ok is True
    assert result.metadata["isolation"] == "process_guard"
    assert result.metadata["sandbox"] is False
