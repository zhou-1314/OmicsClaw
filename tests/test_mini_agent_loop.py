"""Integration tests for the ADR 0032 mini-agent tactical loop.

Drives a real kernel (sandboxed when bubblewrap is present) with a scripted
fake LLM, so the parse -> lint -> execute -> feedback -> ReturnAnswer machinery
is exercised end to end without a provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.autonomous.budget import BudgetLedger, MiniAgentBudget, TerminationReason
from omicsclaw.autonomous.kernel_envelope import envelope_available
from omicsclaw.autonomous.kernel_session import CellResult, KernelSession, kernel_ipc_available
from omicsclaw.autonomous.mini_agent import _sync_skill_calls, run_mini_agent

SANDBOX = envelope_available()
IPC_AVAILABLE = kernel_ipc_available()


def TURN(purpose: str, code: str) -> str:
    return (
        f"**Purpose**: {purpose}\n"
        f"**Reasoning**: because\n"
        f"**Next Goal**: continue\n"
        f"**Code**:\n```python\n{code}\n```"
    )


class ScriptedLLM:
    """Returns canned turns in order; then idles with a benign step."""

    def __init__(self, turns: list[str]) -> None:
        self._turns = list(turns)
        self._i = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        self.prompts.append(prompt)
        if self._i < len(self._turns):
            turn = self._turns[self._i]
            self._i += 1
            return turn
        return TURN("idle", "pass")


@pytest.fixture()
def session(tmp_path: Path):
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    ks = KernelSession(workspace_root=tmp_path, sandbox=SANDBOX, startup_timeout=60)
    ks.start()
    try:
        yield ks
    finally:
        ks.shutdown()


def test_sync_skill_calls_mirrors_jsonl_into_ledger(tmp_path: Path):
    """The facade's per-call counter lives in the kernel, so the host mirrors
    skill_calls.jsonl into the ledger; this must be accurate and idempotent."""
    from omicsclaw.autonomous.skill_facade import SKILL_CALLS_LOG

    ledger = BudgetLedger(budget=MiniAgentBudget())
    _sync_skill_calls(ledger, tmp_path)  # no log yet
    assert ledger.skill_calls_used == 0

    log = tmp_path / SKILL_CALLS_LOG
    log.write_text('{"skill": "a"}\n{"skill": "b"}\n', encoding="utf-8")
    _sync_skill_calls(ledger, tmp_path)
    assert ledger.skill_calls_used == 2

    _sync_skill_calls(ledger, tmp_path)  # idempotent: no double-count
    assert ledger.skill_calls_used == 2

    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"skill": "c"}\n')
    _sync_skill_calls(ledger, tmp_path)  # a new call is reflected
    assert ledger.skill_calls_used == 3


def test_happy_path_returns_answer(session: KernelSession, tmp_path: Path):
    llm = ScriptedLLM(
        [
            TURN("compute", "result = 6 * 7\nprint('computed', result)"),
            # uses `result` from the prior step -> proves persistence; answer via file.
            TURN("finish", "ReturnAnswer('the answer is %d' % result)"),
        ]
    )
    outcome = run_mini_agent(
        session=session, llm=llm, goal="compute the answer", workspace_root=tmp_path
    )
    assert outcome.termination is TerminationReason.RETURNED_ANSWER
    assert outcome.answer == "the answer is 42"
    assert len(outcome.steps) == 2
    assert outcome.accepted_cells and len(outcome.accepted_cells) == 2


def test_format_error_is_retried(session: KernelSession, tmp_path: Path):
    llm = ScriptedLLM(
        [
            "this response has no required sections at all",
            TURN("finish", "ReturnAnswer('recovered')"),
        ]
    )
    outcome = run_mini_agent(
        session=session, llm=llm, goal="recover from a bad turn", workspace_root=tmp_path
    )
    assert outcome.answer == "recovered"
    assert outcome.steps[0].error.startswith("format:")
    assert outcome.termination is TerminationReason.RETURNED_ANSWER


def test_safety_lint_blocks_subprocess(session: KernelSession, tmp_path: Path):
    llm = ScriptedLLM(
        [
            TURN("escape", "import subprocess\nsubprocess.run(['ls'])"),
            TURN("finish", "ReturnAnswer('done')"),
        ]
    )
    outcome = run_mini_agent(
        session=session, llm=llm, goal="must not run subprocess", workspace_root=tmp_path
    )
    assert any("blocked" in s.error for s in outcome.steps)
    assert outcome.answer == "done"


def test_step_budget_terminates(session: KernelSession, tmp_path: Path):
    llm = ScriptedLLM([])  # always idles, never returns an answer
    outcome = run_mini_agent(
        session=session,
        llm=llm,
        goal="loop until budget",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=2),
    )
    assert outcome.termination is TerminationReason.STEP_BUDGET
    assert len(outcome.steps) == 2
    assert outcome.answer == ""


class FakeSession:
    def __init__(self, results: list[CellResult]) -> None:
        self._results = list(results)
        self.executed: list[str] = []

    def execute(self, code: str, *, timeout: float = 120.0) -> CellResult:
        self.executed.append(code)
        if self._results:
            return self._results.pop(0)
        return CellResult(ok=True)

    def introspect(self) -> dict[str, dict]:
        return {}


def test_return_answer_must_write_sentinel_file(tmp_path: Path):
    llm = ScriptedLLM([TURN("dead branch", "def later():\n    ReturnAnswer('not actually called')")])
    session = FakeSession(
        [
            CellResult(ok=True, stdout="[mini-agent kernel ready]\n"),
            CellResult(ok=True, stdout=""),
        ]
    )
    outcome = run_mini_agent(
        session=session,  # type: ignore[arg-type]
        llm=llm,
        goal="must not accept a never-called ReturnAnswer",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=1),
    )
    assert outcome.termination is TerminationReason.STEP_BUDGET
    assert outcome.answer == ""


def test_token_budget_counts_prompt_and_response_text(tmp_path: Path):
    llm = ScriptedLLM([TURN("idle", "pass")])
    session = FakeSession(
        [
            CellResult(ok=True, stdout="[mini-agent kernel ready]\n"),
            CellResult(ok=True, stdout=""),
        ]
    )
    outcome = run_mini_agent(
        session=session,  # type: ignore[arg-type]
        llm=llm,
        goal="consume token budget",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=5, max_total_tokens=1),
    )
    assert outcome.termination is TerminationReason.TOKEN_BUDGET
    assert outcome.ledger and outcome.ledger["tokens_used"] > 0


def test_cell_timeout_stops_loop_without_reusing_session(tmp_path: Path):
    llm = ScriptedLLM([TURN("hang", "while True:\n    pass"), TURN("next", "x = 1")])
    session = FakeSession(
        [
            CellResult(ok=True, stdout="[mini-agent kernel ready]\n"),
            CellResult(ok=False, timed_out=True, duration_seconds=5.0),
        ]
    )
    outcome = run_mini_agent(
        session=session,  # type: ignore[arg-type]
        llm=llm,
        goal="stop after timeout",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=5),
    )
    assert outcome.termination is TerminationReason.ENGINE_ERROR
    assert len(session.executed) == 2
