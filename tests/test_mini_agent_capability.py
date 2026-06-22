"""Tests for the ADR 0032 §8 model-capability gate.

Covers the pre-flight behavioural probe, the run/refuse/degrade gate decision,
and the in-loop warm-up backstop — none of which need a real kernel.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.autonomous.budget import MiniAgentBudget, TerminationReason
from omicsclaw.autonomous.capability import (
    CapabilityVerdict,
    mini_agent_gate,
    probe_model_capability,
)
from omicsclaw.autonomous.kernel_session import CellResult
from omicsclaw.autonomous.mini_agent import run_mini_agent


def TURN(purpose: str, code: str) -> str:
    return (
        f"**Purpose**: {purpose}\n**Reasoning**: r\n**Next Goal**: g\n"
        f"**Code**:\n```python\n{code}\n```"
    )


class CapableLLM:
    def complete(self, prompt, *, temperature=0.0):
        return TURN("probe", "print(2 + 2)")


class GarbageLLM:
    def complete(self, prompt, *, temperature=0.0):
        return "I will not follow any particular structure here."


class EmptyLLM:
    def complete(self, prompt, *, temperature=0.0):
        return ""


class RecoverLLM:
    """Fails the first probe attempt, then complies."""

    def __init__(self):
        self.n = 0

    def complete(self, prompt, *, temperature=0.0):
        self.n += 1
        return "nope" if self.n == 1 else TURN("probe", "print(1)")


class FakeSession:
    """Minimal kernel stand-in: init/cells 'succeed' but introspect is empty."""

    def execute(self, code: str, *, timeout: float = 120.0) -> CellResult:
        return CellResult(ok=True, stdout="[mini-agent kernel ready]")

    def introspect(self) -> dict:
        return {}


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def test_probe_capable_model():
    probe = probe_model_capability(CapableLLM())
    assert probe.verdict is CapabilityVerdict.CAPABLE
    assert probe.attempts_used == 1


def test_probe_incapable_model():
    probe = probe_model_capability(GarbageLLM(), attempts=2)
    assert probe.verdict is CapabilityVerdict.INCAPABLE
    assert probe.attempts_used == 2
    assert probe.last_excerpt


def test_probe_recovers_on_second_attempt():
    probe = probe_model_capability(RecoverLLM(), attempts=2)
    assert probe.verdict is CapabilityVerdict.CAPABLE
    assert probe.attempts_used == 2


def test_probe_handles_empty_response():
    assert probe_model_capability(EmptyLLM(), attempts=2).verdict is CapabilityVerdict.INCAPABLE


# --------------------------------------------------------------------------- #
# gate decision
# --------------------------------------------------------------------------- #


def test_gate_runs_when_probe_disabled():
    decision = mini_agent_gate(GarbageLLM(), probe_enabled=False)
    assert decision.action == "run"
    assert decision.probe is None


def test_gate_runs_when_capable():
    assert mini_agent_gate(CapableLLM(), probe_enabled=True).action == "run"


def test_gate_refuses_incapable_model():
    # One engine: an incapable model is refused (no legacy engine to degrade to).
    decision = mini_agent_gate(GarbageLLM(), probe_enabled=True)
    assert decision.action == "refuse"
    assert "could not follow" in decision.diagnostic.lower()


# --------------------------------------------------------------------------- #
# in-loop warm-up backstop
# --------------------------------------------------------------------------- #


def test_warmup_backstop_aborts_on_malformed_turns(tmp_path: Path):
    outcome = run_mini_agent(
        session=FakeSession(),  # type: ignore[arg-type]
        llm=GarbageLLM(),
        goal="model that cannot follow the contract",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=8),
    )
    assert outcome.termination is TerminationReason.MODEL_INCAPABLE
    # Stops at the warm-up window (3), not after burning all 8 steps.
    assert len(outcome.steps) == 3
    assert outcome.answer == ""


def test_warmup_not_triggered_when_model_produces_valid_turns(tmp_path: Path):
    # Valid turns that simply never call ReturnAnswer -> NOT incapable; the loop
    # should exhaust the step budget instead.
    class IdleButValidLLM:
        def complete(self, prompt, *, temperature=0.0):
            return TURN("idle", "pass")

    outcome = run_mini_agent(
        session=FakeSession(),  # type: ignore[arg-type]
        llm=IdleButValidLLM(),
        goal="valid but never finishes",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=4),
    )
    assert outcome.termination is TerminationReason.STEP_BUDGET
