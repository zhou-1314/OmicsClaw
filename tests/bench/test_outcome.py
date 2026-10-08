"""The outcome rules, one row per way a run can end.

The split that matters is ``infra_failure`` against everything else: an
infrastructure failure is retried and never graded, every other outcome is
a result about the agent. A provider error recorded as a result is how a
batch of runs once had to be thrown away, so the rows that keep the two
apart are spelled out.
"""

from __future__ import annotations

import pytest

from omicsclaw.bench.outcome import (
    AGENT_OUTCOMES,
    APPROVAL_DENIED,
    COMPLETED,
    INFRA_FAILURE,
    MAX_TURNS,
    NO_DELIVERABLE,
    TIMEOUT,
    TRUNCATED,
    Evidence,
    ProcessExit,
    Usage,
    classify,
)

EXITED = ProcessExit(started=True, returncode=0)
ANSWERED = Usage(llm_calls=3, llm_errors=0, llm_cancelled=0)


def evidence(**fields) -> Evidence:
    return Evidence(**{"stop_reason": "converged", "usage": ANSWERED, **fields})


@pytest.mark.parametrize(
    ("exit", "found", "missing", "outcome", "reason"),
    [
        (EXITED, evidence(), [], COMPLETED, ""),
        (EXITED, evidence(), ["output/a.json"], NO_DELIVERABLE, "output/a.json"),
        (EXITED, evidence(approvals_denied=1), [], APPROVAL_DENIED, "1 approval"),
        (EXITED, evidence(approvals_pending=2), ["x"], APPROVAL_DENIED, "2 approval"),
        (EXITED, evidence(stop_reason="max_turns"), [], MAX_TURNS, ""),
        (EXITED, evidence(stop_reason="truncated"), ["x"], TRUNCATED, ""),
        (EXITED, evidence(stop_reason=""), [], INFRA_FAILURE, "no_stop_reason"),
        (
            ProcessExit(started=False, error="No such file"),
            Evidence(),
            ["x"],
            INFRA_FAILURE,
            "spawn_failed: No such file",
        ),
        (
            EXITED,
            evidence(infra_reason="provider_error: ProviderError status=500"),
            [],
            INFRA_FAILURE,
            "provider_error",
        ),
        (
            ProcessExit(started=True, returncode=1),
            evidence(stop_reason="", failure="Failed: ProviderError"),
            ["x"],
            INFRA_FAILURE,
            "exit_1: Failed: ProviderError",
        ),
        (
            ProcessExit(started=True, returncode=2),
            Evidence(),
            ["x"],
            INFRA_FAILURE,
            "exit_2",
        ),
        (
            ProcessExit(started=True, returncode=-9),
            Evidence(),
            ["x"],
            INFRA_FAILURE,
            "signal_SIGKILL",
        ),
        (
            ProcessExit(started=True, returncode=143, timed_out=True),
            evidence(stop_reason=""),
            ["x"],
            TIMEOUT,
            "wall_clock",
        ),
        (
            ProcessExit(started=True, returncode=-9, timed_out=True, killed=True),
            evidence(stop_reason="", usage=Usage()),
            [],
            TIMEOUT,
            "wall_clock",
        ),
        (
            ProcessExit(started=True, returncode=143, timed_out=True),
            evidence(stop_reason="", usage=Usage(llm_calls=1, llm_cancelled=1)),
            ["x"],
            INFRA_FAILURE,
            "timeout_before_any_model_response",
        ),
        (
            ProcessExit(started=True, returncode=143, timed_out=True),
            evidence(stop_reason="", usage=Usage(llm_calls=0)),
            ["x"],
            INFRA_FAILURE,
            "timeout_before_any_model_response",
        ),
        (
            ProcessExit(started=True, returncode=1),
            evidence(stop_reason="timeout", failure="Failed: TimeoutError"),
            ["x"],
            TIMEOUT,
            "agent_deadline",
        ),
        (
            ProcessExit(started=True, returncode=1),
            evidence(
                stop_reason="timeout", usage=Usage(llm_calls=1, llm_cancelled=1)
            ),
            ["x"],
            INFRA_FAILURE,
            "timeout_before_any_model_response",
        ),
    ],
)
def test_each_ending_has_one_outcome(exit, found, missing, outcome, reason):
    got, why = classify(exit, found, missing)

    assert got == outcome
    assert reason in why


def test_a_failed_model_call_outranks_a_delivered_answer():
    """The agent exited ``0``, said it converged and delivered its file,
    but a model call inside it failed and was never recovered. That run is
    an infrastructure failure, however complete it looks.
    """
    found = evidence(infra_reason="provider_error: ProviderError in a sub-agent")

    assert classify(EXITED, found, [])[0] == INFRA_FAILURE


def test_a_failed_model_call_outranks_a_timeout():
    exit = ProcessExit(started=True, returncode=143, timed_out=True)
    found = evidence(stop_reason="", infra_reason="provider_error: ProviderError")

    assert classify(exit, found, [])[0] == INFRA_FAILURE


def test_a_timeout_is_a_result_when_the_adapter_cannot_count_calls():
    exit = ProcessExit(started=True, returncode=-15, timed_out=True)

    assert classify(exit, Evidence(), ["x"])[0] == TIMEOUT


def test_only_an_infrastructure_failure_is_not_about_the_agent():
    assert INFRA_FAILURE not in AGENT_OUTCOMES
    assert AGENT_OUTCOMES == {
        COMPLETED, NO_DELIVERABLE, APPROVAL_DENIED, MAX_TURNS, TRUNCATED, TIMEOUT,
    }
