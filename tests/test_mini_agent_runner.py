"""End-to-end tests for the ADR 0032 mini-agent runner + dispatch wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omicsclaw.autonomous.budget import MiniAgentBudget, TerminationReason
from omicsclaw.autonomous.contracts import AutonomousRunRequest, AutonomousRunStatus
from omicsclaw.autonomous.kernel_session import kernel_ipc_available
from omicsclaw.autonomous.mini_agent_runner import (
    _budget_from_request,
    _status_for,
    run_mini_agent_request,
)

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


def test_run_dir_has_no_kernel_machinery_or_empty_dirs(tmp_path: Path):
    """Regression for the workspace-clutter bug: a finished run must not ship
    kernel HOME machinery (.cache/.config/.ipython) or empty placeholder dirs
    into the user-facing output (RC1 + RC2 + RC3)."""
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    request = AutonomousRunRequest(goal="compute the answer", output_root=str(tmp_path))
    result = run_mini_agent_request(
        request, llm_client=ScriptedLLM(_answer_turns()), require_sandbox=False, budget=BUDGET
    )
    assert result.ok is True
    ws = Path(result.workspace_root)
    # (Q2) the replay re-run happens in throwaway scratch, never in the deliverable.
    assert not (ws / "replay").exists(), "replay re-run leaked into the run workspace"
    # (RC1) kernel HOME machinery never lands in the user-facing run dir.
    for junk in (".cache", ".config", ".ipython"):
        assert not (ws / junk).exists(), f"kernel machinery {junk} leaked into the run dir"
    # (RC2/RC3) no empty placeholder or leftover directory anywhere in the run.
    empties = sorted(
        str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_dir() and not any(p.iterdir())
    )
    assert empties == [], f"empty dirs left behind: {empties}"


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


def test_budget_from_request_wires_one_shot_era_fields():
    """ADR 0032: request.timeout_seconds and max_repair_attempts are no longer
    inert — they map onto wall_clock and consecutive-failure tolerance."""
    # Defaults preserve the prior effective behaviour (3600s wall, 3 failures).
    default = _budget_from_request(AutonomousRunRequest(goal="g", output_root="x"))
    assert default.wall_clock_seconds == 3600
    assert default.max_consecutive_failures == 3  # default 2 repairs + 1 attempt

    tuned = _budget_from_request(
        AutonomousRunRequest(
            goal="g", output_root="x", timeout_seconds=900, max_repair_attempts=1
        )
    )
    assert tuned.wall_clock_seconds == 900
    assert tuned.max_consecutive_failures == 2  # 1 repair + 1 attempt


def test_budget_from_request_metadata_override_still_wins():
    request = AutonomousRunRequest(
        goal="g",
        output_root="x",
        timeout_seconds=900,
        metadata={"mini_agent_budget": {"wall_clock_seconds": 120, "max_steps": 5}},
    )
    budget = _budget_from_request(request)
    assert budget.wall_clock_seconds == 120  # explicit override beats timeout_seconds
    assert budget.max_steps == 5
    assert budget.max_consecutive_failures == 3  # untouched by the override


def test_status_for_distinguishes_timeout_from_failure():
    assert _status_for(True, TerminationReason.RETURNED_ANSWER) is AutonomousRunStatus.SUCCEEDED
    assert _status_for(False, TerminationReason.WALL_CLOCK) is AutonomousRunStatus.TIMED_OUT
    assert _status_for(False, TerminationReason.STEP_BUDGET) is AutonomousRunStatus.FAILED
    assert _status_for(False, TerminationReason.MODEL_INCAPABLE) is AutonomousRunStatus.FAILED


def test_capability_probe_is_cached_on_production_path(tmp_path: Path, monkeypatch):
    """A capable model is probed once; later runs with the same identity reuse the
    positive verdict. Refusals are never cached, and injected clients never are."""
    import asyncio

    from omicsclaw.autonomous import code_loop
    from omicsclaw.autonomous.capability import GateDecision
    from omicsclaw.autonomous.contracts import AutonomousRunResult

    monkeypatch.setenv("OMICSCLAW_MINI_AGENT_PROBE", "1")
    code_loop._CAPABLE_MODEL_CACHE.clear()
    calls = {"probe": 0, "run": 0}

    def fake_gate(client, **kwargs):
        calls["probe"] += 1
        return GateDecision(action="run", probe=None)

    async def fake_run(request, *, llm_client=None):
        calls["run"] += 1
        return AutonomousRunResult(
            run_id="r", workspace_root="w", status=AutonomousRunStatus.SUCCEEDED
        )

    # No real provider: the gate is faked and the client is an inert stand-in.
    monkeypatch.setattr(code_loop, "ProviderChatClient", lambda **kw: object())
    monkeypatch.setattr("omicsclaw.autonomous.capability.mini_agent_gate", fake_gate)
    monkeypatch.setattr(
        "omicsclaw.autonomous.mini_agent_runner.run_mini_agent_request_async", fake_run
    )

    request = AutonomousRunRequest(
        goal="g", output_root=str(tmp_path), provider_override="p1", model_override="m1"
    )
    asyncio.run(code_loop.run_autonomous_code_loop_async(request))  # llm_client=None -> production
    asyncio.run(code_loop.run_autonomous_code_loop_async(request))

    assert calls["probe"] == 1  # second run short-circuited on the cached verdict
    assert calls["run"] == 2
    code_loop._CAPABLE_MODEL_CACHE.clear()


def test_capability_refusal_is_not_cached(tmp_path: Path, monkeypatch):
    import asyncio

    from omicsclaw.autonomous import code_loop
    from omicsclaw.autonomous.capability import GateDecision

    monkeypatch.setenv("OMICSCLAW_MINI_AGENT_PROBE", "1")
    code_loop._CAPABLE_MODEL_CACHE.clear()
    calls = {"probe": 0}

    def fake_gate(client, **kwargs):
        calls["probe"] += 1
        return GateDecision(action="refuse", probe=None)

    monkeypatch.setattr(code_loop, "ProviderChatClient", lambda **kw: object())
    monkeypatch.setattr("omicsclaw.autonomous.capability.mini_agent_gate", fake_gate)

    request = AutonomousRunRequest(
        goal="g", output_root=str(tmp_path), provider_override="p1", model_override="m1"
    )
    asyncio.run(code_loop.run_autonomous_code_loop_async(request))
    asyncio.run(code_loop.run_autonomous_code_loop_async(request))

    assert calls["probe"] == 2  # a refusal must re-probe, never poison the cache
    assert ("p1", "m1") not in code_loop._CAPABLE_MODEL_CACHE
    code_loop._CAPABLE_MODEL_CACHE.clear()


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


def test_autonomous_loop_does_not_advertise_dead_approval_hook():
    # F: request_tool_approval was threaded into the autonomous loop but silently
    # dropped (code_loop never forwarded it; the mini-agent has no mid-run
    # approval). The misleading dead param (+ the equally-dead runtime_context)
    # must not exist — gating lives at the outer agent loop (ADR 0008 L2) + the
    # kernel envelope / strict-sandbox tier.
    import inspect

    from omicsclaw.autonomous import code_loop

    for fn in (code_loop.run_autonomous_code_loop_async, code_loop.run_autonomous_code_loop):
        params = inspect.signature(fn).parameters
        assert "request_tool_approval" not in params, fn.__name__
        assert "runtime_context" not in params, fn.__name__
