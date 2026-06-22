"""Budget envelope and step trace for the Autonomous Code Mini-Agent.

ADR 0032 §8 makes the budget part of the contract: the mini-agent is more
expensive than the one-shot generator, so step / failure / skill-call / token /
wall-clock limits are explicit, defaulted conservatively for v1, and enforced
by a ledger that reports *why* a run terminated.

Pure data + arithmetic; no kernel or LLM dependency so it is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar


class TerminationReason(StrEnum):
    """Why the mini-agent loop stopped."""

    RETURNED_ANSWER = "returned_answer"
    STEP_BUDGET = "step_budget_exhausted"
    CONSECUTIVE_FAILURES = "consecutive_failures_exhausted"
    SKILL_CALL_BUDGET = "skill_call_budget_exhausted"
    TOKEN_BUDGET = "token_budget_exhausted"
    WALL_CLOCK = "wall_clock_exhausted"
    CANCELLED = "cancelled"
    ENGINE_ERROR = "engine_error"
    MODEL_INCAPABLE = "model_incapable"


@dataclass(slots=True, frozen=True)
class MiniAgentBudget:
    """Bounds for one autonomous mini-agent run (ADR 0032 §8 defaults).

    ``max_steps`` defaults to 8; the 15 ceiling is only reached behind explicit
    benchmarking via :meth:`with_overrides`. Raw generated cells get a short
    timeout; vetted skill calls through the facade are allowed a much longer one
    because a single skill can legitimately run for minutes.
    """

    max_steps: int = 8
    max_consecutive_failures: int = 3
    raw_cell_timeout_seconds: int = 120
    skill_call_timeout_seconds: int = 1800
    max_skill_calls: int = 20
    max_total_tokens: int | None = None
    wall_clock_seconds: int = 3600

    # Hard ceiling on ``max_steps`` so an override cannot make the loop unbounded.
    # ClassVar (not a field) so it stays out of the constructor and slots.
    STEP_CEILING: ClassVar[int] = 15

    def with_overrides(self, **overrides: Any) -> "MiniAgentBudget":
        """Return a copy with selected fields overridden and re-clamped."""
        merged = {
            "max_steps": self.max_steps,
            "max_consecutive_failures": self.max_consecutive_failures,
            "raw_cell_timeout_seconds": self.raw_cell_timeout_seconds,
            "skill_call_timeout_seconds": self.skill_call_timeout_seconds,
            "max_skill_calls": self.max_skill_calls,
            "max_total_tokens": self.max_total_tokens,
            "wall_clock_seconds": self.wall_clock_seconds,
        }
        merged.update({k: v for k, v in overrides.items() if k in merged})
        return MiniAgentBudget(**merged).clamped()

    def clamped(self) -> "MiniAgentBudget":
        """Return a copy with every bound forced into a sane range."""
        return MiniAgentBudget(
            max_steps=max(1, min(int(self.max_steps), self.STEP_CEILING)),
            max_consecutive_failures=max(1, int(self.max_consecutive_failures)),
            raw_cell_timeout_seconds=max(5, int(self.raw_cell_timeout_seconds)),
            skill_call_timeout_seconds=max(30, int(self.skill_call_timeout_seconds)),
            max_skill_calls=max(0, int(self.max_skill_calls)),
            max_total_tokens=(None if self.max_total_tokens is None else max(0, int(self.max_total_tokens))),
            wall_clock_seconds=max(30, int(self.wall_clock_seconds)),
        )


@dataclass(slots=True)
class SkillCallTrace:
    """One nested skill invocation made through the ``oc`` facade."""

    skill: str
    params: dict[str, Any]
    input_artifact: str
    output_dir: str
    primary_artifact: str
    status: str
    manifest_path: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "params": dict(self.params),
            "input_artifact": self.input_artifact,
            "output_dir": self.output_dir,
            "primary_artifact": self.primary_artifact,
            "status": self.status,
            "manifest_path": self.manifest_path,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass(slots=True)
class MiniAgentStep:
    """Trace of one accepted-or-rejected mini-agent step for provenance."""

    index: int
    purpose: str = ""
    reasoning: str = ""
    next_goal: str = ""
    code: str = ""
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    new_variables: dict[str, str] = field(default_factory=dict)
    skill_calls: list[SkillCallTrace] = field(default_factory=list)
    duration_seconds: float = 0.0
    accepted: bool = False
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "purpose": self.purpose,
            "reasoning": self.reasoning,
            "next_goal": self.next_goal,
            "code": self.code,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "error": self.error,
            "new_variables": dict(self.new_variables),
            "skill_calls": [call.to_dict() for call in self.skill_calls],
            "duration_seconds": round(self.duration_seconds, 3),
            "accepted": self.accepted,
            "tokens": self.tokens,
        }


@dataclass(slots=True)
class BudgetLedger:
    """Mutable accounting of consumption against a :class:`MiniAgentBudget`."""

    budget: MiniAgentBudget
    steps_used: int = 0
    consecutive_failures: int = 0
    skill_calls_used: int = 0
    tokens_used: int = 0

    def record_step(self, *, accepted: bool, tokens: int = 0) -> None:
        self.steps_used += 1
        self.tokens_used += max(0, int(tokens))
        if accepted:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

    def record_skill_call(self) -> None:
        self.skill_calls_used += 1

    def exhausted_reason(self, *, elapsed_seconds: float) -> TerminationReason | None:
        """Return the first tripped limit, or ``None`` while there is headroom.

        Checked *before* each step so the loop never starts a step it cannot
        afford. Wall-clock uses the caller-supplied elapsed time.
        """
        if self.steps_used >= self.budget.max_steps:
            return TerminationReason.STEP_BUDGET
        if self.consecutive_failures >= self.budget.max_consecutive_failures:
            return TerminationReason.CONSECUTIVE_FAILURES
        # Redundant safety net: the skill facade (skill_facade.py) is the hard
        # per-call enforcer and raises SkillBudgetError before recording an
        # over-budget call, so skill_calls_used never actually exceeds the cap.
        if self.skill_calls_used > self.budget.max_skill_calls:
            return TerminationReason.SKILL_CALL_BUDGET
        if (
            self.budget.max_total_tokens is not None
            and self.tokens_used >= self.budget.max_total_tokens
        ):
            return TerminationReason.TOKEN_BUDGET
        if elapsed_seconds >= self.budget.wall_clock_seconds:
            return TerminationReason.WALL_CLOCK
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps_used": self.steps_used,
            "consecutive_failures": self.consecutive_failures,
            "skill_calls_used": self.skill_calls_used,
            "tokens_used": self.tokens_used,
            "max_steps": self.budget.max_steps,
            "max_skill_calls": self.budget.max_skill_calls,
            "max_total_tokens": self.budget.max_total_tokens,
            "wall_clock_seconds": self.budget.wall_clock_seconds,
        }


__all__ = [
    "BudgetLedger",
    "MiniAgentBudget",
    "MiniAgentStep",
    "SkillCallTrace",
    "TerminationReason",
]
