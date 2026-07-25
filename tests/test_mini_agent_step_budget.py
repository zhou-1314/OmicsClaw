"""Step-budget semantics for the ADR 0032 mini-agent loop.

Diagnosis 2026-07-25: ``max_steps`` was a *silent, undisclosed guillotine on
total LLM turns*. Three independent decisions compounded into runs that died
mid-analysis with an empty answer while N successful cells sat on disk:

1. it metered the wrong thing — malformed / lint-blocked / empty-response turns
   were charged to the same budget as real analysis steps;
2. it was invisible to the planner — neither the system prompt nor the per-step
   feedback ever told the model a budget existed, so it could not pace or land;
3. it was terminal rather than a landing signal — the loop just broke with
   ``answer=""``.

These tests pin the repaired contract: the budget meters *executed* steps, the
reject lane is metered separately, the model is told its remaining budget and
is forced to land on the final step, and exhaustion reports the partial work.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.autonomous import run_layout
from omicsclaw.autonomous.budget import BudgetLedger, MiniAgentBudget, TerminationReason
from omicsclaw.autonomous.kernel_session import CellResult
from omicsclaw.autonomous.mini_agent import run_mini_agent


def TURN(purpose: str, code: str) -> str:
    return (
        f"**Purpose**: {purpose}\n"
        f"**Reasoning**: because\n"
        f"**Next Goal**: continue\n"
        f"**Code**:\n```python\n{code}\n```"
    )


class AnswerAwareSession:
    """Fake kernel that honours ``ReturnAnswer`` by writing the sentinel file."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.executed: list[str] = []

    def execute(self, code: str, *, timeout: float = 120.0, cancel_event=None) -> CellResult:
        self.executed.append(code)
        if "ReturnAnswer(" in code and not code.lstrip().startswith("def "):
            path = self.workspace / run_layout.relpath("answer")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("final summary", encoding="utf-8")
        return CellResult(ok=True)

    def introspect(self) -> dict[str, dict]:
        return {}


class ScriptedLLM:
    """Replays canned turns, then idles with a benign valid turn."""

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


class LandsWhenToldLLM:
    """A model that keeps working until the loop tells it this is the last step.

    Stands in for a competent model that *would* have landed gracefully if the
    budget had ever been disclosed to it.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        self.prompts.append(prompt)
        if "LAST STEP" in prompt:
            return TURN("land", "ReturnAnswer('partial but real summary')")
        return TURN("work", "res = 1")


def _drive(llm, workspace: Path, **budget_kwargs):
    return run_mini_agent(
        session=AnswerAwareSession(workspace),  # type: ignore[arg-type]
        llm=llm,
        goal="run a standard analysis",
        workspace_root=workspace,
        budget=MiniAgentBudget(**budget_kwargs),
    )


# --------------------------------------------------------------------------- #
# 1. the budget meters executed steps, not raw LLM turns
# --------------------------------------------------------------------------- #


def test_rejected_turns_do_not_consume_the_analysis_step_budget(tmp_path: Path):
    """A malformed turn and a lint-blocked turn are model/infra noise, not
    analysis progress; charging them to ``max_steps`` silently shrank the real
    budget (here: 3 -> 1 usable step) and pushed real work off the end."""
    llm = ScriptedLLM(
        [
            "prose instead of the contract",  # format reject, never executed
            TURN("escape", "import subprocess\nsubprocess.run(['ls'])"),  # lint reject
            TURN("work", "x = 1"),
            TURN("finish", "ReturnAnswer('done')"),
        ]
    )
    outcome = _drive(llm, tmp_path, max_steps=3)

    assert outcome.termination is TerminationReason.RETURNED_ANSWER
    assert outcome.answer == "final summary"
    # Two executed cells; the two rejected turns were metered elsewhere.
    assert outcome.ledger["steps_used"] == 2
    assert outcome.ledger["rejected_turns"] == 2


def test_reject_lane_is_still_bounded(tmp_path: Path):
    """Not charging rejects to ``max_steps`` must not make them free: an
    alternating reject/accept model never trips ``max_consecutive_failures``,
    so the reject lane needs its own cap."""

    class AlternatingLLM:
        def __init__(self) -> None:
            self._n = 0

        def complete(self, prompt, *, temperature=0.0):
            self._n += 1
            return "garbage" if self._n % 2 else TURN("ok", "x = 1")

    outcome = run_mini_agent(
        session=AnswerAwareSession(tmp_path),  # type: ignore[arg-type]
        llm=AlternatingLLM(),
        goal="never finishes, never fails twice in a row",
        workspace_root=tmp_path,
        budget=MiniAgentBudget(max_steps=100, max_rejected_turns=4),
    )
    assert outcome.termination is TerminationReason.REJECTED_TURN_BUDGET
    assert outcome.ledger["rejected_turns"] == 4


# --------------------------------------------------------------------------- #
# 2. the budget is disclosed, and the final step is a landing signal
# --------------------------------------------------------------------------- #


def test_system_prompt_discloses_the_step_budget(tmp_path: Path):
    llm = ScriptedLLM([TURN("finish", "ReturnAnswer('done')")])
    _drive(llm, tmp_path, max_steps=6)
    assert "6" in llm.prompts[0]
    assert "step" in llm.prompts[0].lower()


def test_feedback_reports_remaining_steps(tmp_path: Path):
    llm = ScriptedLLM([TURN("work", "x = 1"), TURN("finish", "ReturnAnswer('done')")])
    _drive(llm, tmp_path, max_steps=5)
    # The second prompt must tell the model how much budget is left.
    assert "steps remaining" in llm.prompts[1].lower()


def test_final_step_forces_a_landing_instead_of_a_guillotine(tmp_path: Path):
    """The headline fix: a model that would run forever now gets an explicit
    LAST STEP directive and returns a real (if partial) answer, instead of the
    loop breaking silently with answer=''."""
    llm = LandsWhenToldLLM()
    outcome = _drive(llm, tmp_path, max_steps=4)

    assert outcome.termination is TerminationReason.RETURNED_ANSWER
    assert outcome.answer == "final summary"
    assert any("LAST STEP" in p for p in llm.prompts)


# --------------------------------------------------------------------------- #
# 3. the capability backstop must not misfire on the cheap path
# --------------------------------------------------------------------------- #


def test_one_malformed_turn_on_the_cheap_path_is_not_model_incapable(tmp_path: Path):
    """ADR 0032 §7 allows ``max_steps=1``/``2`` for trivial residual work. The
    warm-up backstop used to clamp to ``max_steps``, so a single fumbled turn in
    a 1-step run reported a perfectly capable model as MODEL_INCAPABLE and told
    the user to switch models."""
    llm = ScriptedLLM(["oops, prose", TURN("finish", "ReturnAnswer('done')")])
    outcome = _drive(llm, tmp_path, max_steps=1)

    assert outcome.termination is TerminationReason.RETURNED_ANSWER
    assert outcome.answer == "final summary"


def test_warmup_backstop_still_catches_a_genuinely_incapable_model(tmp_path: Path):
    """The backstop itself must survive: three unusable turns in a row is still
    an incapable model, regardless of how large ``max_steps`` is."""

    class GarbageLLM:
        def complete(self, prompt, *, temperature=0.0):
            return "I cannot follow this format at all."

    outcome = _drive(GarbageLLM(), tmp_path, max_steps=12)
    assert outcome.termination is TerminationReason.MODEL_INCAPABLE
    assert len(outcome.steps) == 3


# --------------------------------------------------------------------------- #
# 4. ledger accounting
# --------------------------------------------------------------------------- #


def test_ledger_separates_executed_steps_from_rejected_turns():
    ledger = BudgetLedger(budget=MiniAgentBudget(max_steps=2, max_rejected_turns=2))
    ledger.record_step(accepted=False, executed=False)
    assert ledger.steps_used == 0 and ledger.rejected_turns == 1
    assert ledger.consecutive_failures == 1  # still a failure for the repair lane
    assert ledger.exhausted_reason(elapsed_seconds=0) is None

    ledger.record_step(accepted=True)
    assert ledger.steps_used == 1 and ledger.remaining_steps == 1

    ledger.record_step(accepted=True)
    assert ledger.exhausted_reason(elapsed_seconds=0) is TerminationReason.STEP_BUDGET


def test_remaining_steps_never_goes_negative():
    ledger = BudgetLedger(budget=MiniAgentBudget(max_steps=1))
    ledger.record_step(accepted=True)
    ledger.record_step(accepted=True)
    assert ledger.remaining_steps == 0
