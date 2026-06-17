"""Tests for the domain-neutral L1 ``fan_out`` primitive (ADR 0016 L1).

These exercise ``fan_out`` directly through a non-consensus step type to prove
the runtime sets no survivor policy of its own: the ``required_survivors``
minimum is purely caller-supplied. The consensus ``>=2`` rule lives one layer
up (``runtime/consensus/team.run_team`` default + the driver's readable-label
gate), covered by ``tests/runtime/consensus/test_team_runtime.py`` and
``test_driver.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from omicsclaw.runtime.workflow.fan_out import (
    FanOutResult,
    InsufficientSurvivorsError,
    fan_out,
)


# A non-consensus step: anything carrying name / skill_name / to_extra_args().
@dataclass
class _Step:
    name: str
    skill_name: str = "noop-skill"

    def to_extra_args(self) -> list[str]:
        return []


@dataclass
class _StubResult:
    exit_code: int = 0


def _crash_all_but(survivor: str):
    """Runner stub that succeeds only for the step whose output dir ends in ``survivor``."""

    def runner(**kwargs):
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        if out.name == survivor:
            return _StubResult(exit_code=0)
        raise RuntimeError("synthetic crash")

    return runner


@pytest.mark.asyncio
async def test_fan_out_one_survivor_returns_without_minimum(tmp_path: Path) -> None:
    """A non-consensus caller with exactly one survivor and no minimum gets a result."""
    steps = [_Step(f"s{i}") for i in range(3)]

    result = await fan_out(
        steps,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=_crash_all_but("s0"),
    )

    assert isinstance(result, FanOutResult)
    assert result.n_survived == 1
    assert result.n_failed == 2


@pytest.mark.asyncio
async def test_fan_out_zero_survivors_returns_without_minimum(tmp_path: Path) -> None:
    """With no minimum requested, even an all-fail fan-out returns a result, not a raise."""
    steps = [_Step(f"s{i}") for i in range(2)]

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        raise RuntimeError("synthetic crash")

    result = await fan_out(
        steps,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=runner,
    )

    assert result.n_survived == 0
    assert result.n_failed == 2


@pytest.mark.asyncio
async def test_fan_out_raises_only_when_caller_opts_into_minimum(tmp_path: Path) -> None:
    """The same one-survivor run raises iff the caller passes required_survivors."""
    steps = [_Step(f"s{i}") for i in range(3)]

    with pytest.raises(InsufficientSurvivorsError) as exc_info:
        await fan_out(
            steps,
            input_path="/dev/null",
            output_root=tmp_path,
            required_survivors=2,
            runner=_crash_all_but("s0"),
        )
    assert "Only 1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fan_out_required_survivors_met_returns_result(tmp_path: Path) -> None:
    """When survivors meet the requested minimum, no exception is raised."""
    steps = [_Step(f"s{i}") for i in range(3)]

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    result = await fan_out(
        steps,
        input_path="/dev/null",
        output_root=tmp_path,
        required_survivors=2,
        runner=runner,
    )
    assert result.n_survived == 3


@pytest.mark.asyncio
async def test_run_team_wrapper_defaults_to_consensus_minimum(tmp_path: Path) -> None:
    """The consensus back-compat wrapper keeps the historical >=2 default..."""
    from omicsclaw.runtime.consensus.team import MIN_SURVIVING_MEMBERS, run_team

    assert MIN_SURVIVING_MEMBERS == 2
    steps = [_Step(f"s{i}") for i in range(3)]

    with pytest.raises(InsufficientSurvivorsError):
        await run_team(
            steps,
            input_path="/dev/null",
            output_root=tmp_path,
            runner=_crash_all_but("s0"),
        )


@pytest.mark.asyncio
async def test_run_team_wrapper_can_opt_out_of_minimum(tmp_path: Path) -> None:
    """...but a caller can opt out by passing required_survivors=None."""
    from omicsclaw.runtime.consensus.team import run_team

    steps = [_Step(f"s{i}") for i in range(3)]
    result = await run_team(
        steps,
        input_path="/dev/null",
        output_root=tmp_path,
        required_survivors=None,
        runner=_crash_all_but("s0"),
    )
    assert result.n_survived == 1


@pytest.mark.asyncio
async def test_fan_out_reclaims_its_worker_threads(tmp_path: Path) -> None:
    """fan_out owns a call-scoped threadpool and shuts it down on return.

    Regression for the Round-2 Codex finding: with ``asyncio.to_thread`` the
    worker ran on the event loop's shared default executor, whose threads
    persist after fan_out returns (the loop is still open) and accumulate across
    per-test loops, stalling interpreter teardown in constrained sandboxes.
    Identity-based check: no thread created during fan_out stays alive after it
    returns. Fails before the scoped-executor fix (a default-executor worker
    lingers); passes after (the scoped pool is shut down).
    """
    import threading
    import time

    steps = [_Step(f"s{i}") for i in range(3)]

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    before = {t.ident for t in threading.enumerate() if t.is_alive()}
    result = await fan_out(steps, input_path="/dev/null", output_root=tmp_path, runner=runner)
    assert result.n_survived == 3

    # shutdown(wait=False) reclaims workers promptly but not synchronously — poll.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        new_alive = [t for t in threading.enumerate() if t.is_alive() and t.ident not in before]
        if not new_alive:
            break
        time.sleep(0.05)
    new_alive = [t.name for t in threading.enumerate() if t.is_alive() and t.ident not in before]
    assert not new_alive, f"fan_out left worker thread(s) alive after return: {new_alive}"
