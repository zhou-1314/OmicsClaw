"""How a run ended, decided from what its process and its adapter report.

Every finished run gets exactly one outcome:

``completed``
    The agent finished on its own and every deliverable exists.
``no_deliverable``
    The agent finished on its own and a deliverable is missing.
``approval_denied``
    The agent finished on its own, but at least one tool call asked for an
    approval nobody was there to give.
``max_turns`` / ``truncated``
    The agent's own loop stopped at its turn ceiling, or at the model's
    output limit.
``timeout``
    The wall-clock budget ran out, or the agent's own deadline did.
``infra_failure``
    The run says nothing about the agent: the process could not start or
    crashed, a model call failed and was not recovered, or the record of
    the run is missing. ``reason`` says which.

Only ``infra_failure`` is retried and kept out of grading; the others are
results.
"""

from __future__ import annotations

import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AGENT_OUTCOMES",
    "APPROVAL_DENIED",
    "COMPLETED",
    "INFRA_FAILURE",
    "MAX_TURNS",
    "NO_DELIVERABLE",
    "TIMEOUT",
    "TRUNCATED",
    "Command",
    "Evidence",
    "ProcessExit",
    "Usage",
    "classify",
]

COMPLETED = "completed"
NO_DELIVERABLE = "no_deliverable"
APPROVAL_DENIED = "approval_denied"
MAX_TURNS = "max_turns"
TRUNCATED = "truncated"
TIMEOUT = "timeout"
INFRA_FAILURE = "infra_failure"

AGENT_OUTCOMES = frozenset(
    {COMPLETED, NO_DELIVERABLE, APPROVAL_DENIED, MAX_TURNS, TRUNCATED, TIMEOUT}
)
"""Outcomes that describe the agent and stay in every denominator."""


@dataclass(frozen=True)
class ProcessExit:
    """What happened to a run's process.

    :param started: ``False`` when the process could not be started at all;
        *error* then says why.
    :param returncode: The exit status; negative for a signal.
    :param timed_out: The wall-clock budget ran out and the run was stopped.
    :param killed: The polite stop was not enough and ``SIGKILL`` was sent.
    :param strays: Processes of the run still alive after it exited, all
        of which were killed.
    :param wall_s: Seconds from start to exit.
    """

    started: bool
    returncode: int | None = None
    timed_out: bool = False
    killed: bool = False
    strays: int = 0
    wall_s: float = 0.0
    started_at: str = ""
    ended_at: str = ""
    error: str = ""


@dataclass(frozen=True)
class Usage:
    """Model usage of one run. ``None`` means the adapter cannot tell.

    :param input_tokens: Prompt tokens over every model call, cached ones
        included.
    :param cached_input_tokens: The part of *input_tokens* served from a
        cache.
    :param llm_calls: Model calls the agent made, counting each attempt it
        made itself. Retries hidden inside a client library are not seen.
    :param llm_errors: Model calls that failed.
    :param llm_cancelled: Model calls cut short when the run was stopped.
    :param subagent_llm_calls: The part of *llm_calls* made by sub-agents.
    :param calls_without_usage: Calls that reported no input tokens and
        so add nothing to the totals: failed, cancelled, answered with
        nothing, or made to a backend that reports no usage. Above zero,
        the totals are a lower bound.
    :param includes_subagents: Whether the totals count sub-agent calls.
    :param source: Where the numbers were read from.
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    llm_calls: int | None = None
    llm_errors: int | None = None
    llm_cancelled: int | None = None
    subagent_llm_calls: int | None = None
    calls_without_usage: int | None = None
    includes_subagents: bool | None = None
    source: str = ""

    def answered(self) -> int | None:
        """Model calls that were answered, or ``None`` when unknown.

        The calls that reported input tokens, when the adapter counts
        those; otherwise the calls that neither failed nor were cancelled.
        """
        if self.llm_calls is None:
            return None
        if self.calls_without_usage is not None:
            return self.llm_calls - self.calls_without_usage
        return self.llm_calls - (self.llm_errors or 0) - (self.llm_cancelled or 0)

    def as_row(self) -> dict[str, Any]:
        """The fields as they appear in a usage row."""
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
            "llm_cancelled": self.llm_cancelled,
            "subagent_llm_calls": self.subagent_llm_calls,
            "calls_without_usage": self.calls_without_usage,
            "includes_subagents": self.includes_subagents,
            "usage_source": self.source,
        }


@dataclass(frozen=True)
class Command:
    """One tool call the agent asked for, as text the access audit can scan."""

    tool: str
    text: str


@dataclass(frozen=True)
class Evidence:
    """What an adapter read back from a finished run.

    :param stop_reason: ``converged``, ``max_turns``, ``truncated`` or
        ``timeout`` as the agent itself reported it; empty when unknown.
    :param infra_reason: Non-empty when the adapter found a failure that is
        not the agent's doing, such as an unrecovered model call.
    :param failure: The agent's own one-line account of a failed run.
    :param approvals_required: Approval requests the run raised.
    :param approvals_denied: Approval requests that were refused.
    :param approvals_pending: Approval requests left unanswered.
    :param turns: Turns the agent's main loop made, when known.
    :param model_resolved: The model name the agent reports having called,
        which is the only record of it when the manifest names none.
    :param commands: The tool calls the agent asked for, for the access
        audit. ``None`` when the adapter could not recover them, which is
        different from an agent that made none.
    :param notes: Adapter-specific facts recorded with the run.
    """

    stop_reason: str = ""
    infra_reason: str = ""
    failure: str = ""
    approvals_required: int = 0
    approvals_denied: int = 0
    approvals_pending: int = 0
    turns: int | None = None
    model_resolved: str = ""
    usage: Usage = field(default_factory=Usage)
    commands: tuple[Command, ...] | None = ()
    notes: Mapping[str, Any] = field(default_factory=dict)


def classify(
    exit: ProcessExit, evidence: Evidence, missing: Sequence[str]
) -> tuple[str, str]:
    """Decide a run's outcome.

    :param exit: What happened to the process.
    :param evidence: What the adapter read back.
    :param missing: Declared deliverables that do not exist.
    :returns: ``(outcome, reason)``. *reason* is empty for ``completed``.

    The first rule that applies wins: a process that never started; an
    infrastructure failure the adapter found; the wall clock; the agent's
    own deadline; any other non-zero exit; the agent's turn or output
    limit; a run whose ending nobody recorded; a refused approval; a
    missing deliverable. A run stopped by either clock before any model
    call was answered is an infrastructure failure, not a timeout.
    """
    if not exit.started:
        return INFRA_FAILURE, f"spawn_failed: {exit.error}"
    if evidence.infra_reason:
        return INFRA_FAILURE, evidence.infra_reason
    if exit.timed_out or evidence.stop_reason == "timeout":
        if evidence.usage.answered() == 0:
            return INFRA_FAILURE, "timeout_before_any_model_response"
        return TIMEOUT, "wall_clock" if exit.timed_out else "agent_deadline"
    if exit.returncode != 0:
        return INFRA_FAILURE, _exit_reason(exit.returncode, evidence.failure)
    if evidence.stop_reason == "max_turns":
        return MAX_TURNS, ""
    if evidence.stop_reason == "truncated":
        return TRUNCATED, ""
    if evidence.stop_reason != "converged":
        return INFRA_FAILURE, "no_stop_reason: the run left no record of how it ended"
    if evidence.approvals_denied or evidence.approvals_pending:
        refused = evidence.approvals_denied + evidence.approvals_pending
        return APPROVAL_DENIED, f"{refused} approval request(s) not granted"
    if missing:
        return NO_DELIVERABLE, "missing: " + ", ".join(missing)
    return COMPLETED, ""


def _exit_reason(returncode: int | None, failure: str) -> str:
    if returncode is not None and returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        reason = f"signal_{name}"
    else:
        reason = f"exit_{returncode}"
    return f"{reason}: {failure}" if failure else reason
