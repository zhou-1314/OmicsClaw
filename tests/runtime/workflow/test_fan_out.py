"""Tests for the domain-neutral L1 ``fan_out`` primitive (ADR 0016 L1).

These exercise ``fan_out`` directly through a non-consensus step type to prove
the runtime sets no survivor policy of its own: the ``required_survivors``
minimum is purely caller-supplied. The consensus ``>=2`` rule lives one layer
up (``runtime/consensus/team.run_team`` default + the driver's readable-label
gate), covered by ``tests/runtime/consensus/test_team_runtime.py`` and
``test_driver.py``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from omicsclaw.runtime.workflow.fan_out import (
    FanOutResult,
    InsufficientSurvivorsError,
    _resolve_parallel_window,
    fan_out,
)
from omicsclaw.skill import resource_scheduler
from omicsclaw.skill.resource_scheduler import ExecutionResourceBudget


@pytest.fixture(autouse=True)
def _reset_process_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-global scheduler singleton per test.

    ``fan_out`` now reserves capacity from the process-global Execution Resource
    Scheduler (ADR 0061 D2). That singleton is bound to one event loop and
    survives between tests, so a leftover from another test (or a different
    ``max_processes`` budget) must not leak in. Nulling it lets each test's first
    ``fan_out`` rebuild the scheduler from that test's own environment.
    """
    monkeypatch.setattr(resource_scheduler, "_PROCESS_SCHEDULER", None)
    monkeypatch.setattr(resource_scheduler, "_PROCESS_SCHEDULER_LOOP", None)


def _budget(max_processes: int) -> ExecutionResourceBudget:
    return ExecutionResourceBudget(
        cpu_cores=8,
        memory_mib=8192,
        gpu_device_ids=(),
        threads=8,
        temporary_disk_mib=8192,
        max_processes=max_processes,
    )


def _concurrency_probe(hold_seconds: float = 0.1):
    """Runner stub recording the peak number of steps running at once.

    Each step increments a shared counter on entry, holds briefly so concurrent
    steps overlap, then decrements. ``state["peak"]`` is the maximum overlap
    observed — i.e. the concurrency the capacity authority actually permitted.
    """

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(hold_seconds)
        with lock:
            state["active"] -= 1
        return _StubResult(exit_code=0)

    return runner, state


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
async def test_fan_out_workers_are_daemon_threads(tmp_path: Path) -> None:
    """fan_out runs each blocking runner in a DAEMON thread.

    Structural fix for the Round 1-3 pytest-asyncio teardown hangs: earlier
    variants (``asyncio.to_thread`` shared default executor; a call-scoped
    ``ThreadPoolExecutor``) could leave an idle worker that stalled teardown across
    a test sequence. A daemon worker can NEVER block process or test teardown, even
    if it outlives the call (a timed-out/cancelled, unkillable runner). Invariant:
    every thread fan_out spawns is a daemon.
    """
    import threading

    steps = [_Step(f"s{i}") for i in range(3)]

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    before = {t.ident for t in threading.enumerate()}
    result = await fan_out(steps, input_path="/dev/null", output_root=tmp_path, runner=runner)
    assert result.n_survived == 3
    nondaemon = [
        t.name for t in threading.enumerate()
        if t.ident not in before and t.name.startswith("fanout") and not t.daemon
    ]
    assert not nondaemon, f"fan_out spawned non-daemon worker thread(s): {nondaemon}"


@pytest.mark.asyncio
async def test_two_fan_out_calls_in_sequence_dont_hang_or_leak(tmp_path: Path) -> None:
    """Codex Round-3: a SEQUENCE of fan_out calls hung while isolated tests passed.

    Two calls back-to-back (one survivor, then all fail) both return — the test
    completing IS the no-hang assertion — and any fan_out worker spawned is a
    daemon (so a leftover cannot stall the next test's teardown).
    """
    import threading

    steps = [_Step(f"s{i}") for i in range(3)]

    def crash_all(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        raise RuntimeError("synthetic crash")

    before = {t.ident for t in threading.enumerate()}
    r1 = await fan_out(
        steps, input_path="/dev/null", output_root=tmp_path / "a", runner=_crash_all_but("s0")
    )
    assert r1.n_survived == 1
    r2 = await fan_out(steps, input_path="/dev/null", output_root=tmp_path / "b", runner=crash_all)
    assert r2.n_survived == 0
    nondaemon = [
        t.name for t in threading.enumerate()
        if t.ident not in before and t.name.startswith("fanout") and not t.daemon
    ]
    assert not nondaemon, f"fan_out spawned non-daemon worker thread(s): {nondaemon}"


# --------------- ADR 0061 D2: single global capacity authority ------------- #


@pytest.mark.asyncio
async def test_fan_out_capacity_is_gated_by_global_scheduler(tmp_path: Path) -> None:
    """fan-out concurrency is bounded by the shared scheduler (ADR 0061 D2).

    With the process budget pinned to ``max_processes=1`` the four ready steps
    must run strictly one at a time. This is the regression assertion for the
    third-capacity-authority gap: the old ``_compute_max_parallel`` would have
    let ``min(4, cpu//2, 4)`` steps overlap with no scheduler awareness at all.
    """
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(1))
    runner, state = _concurrency_probe()
    steps = [_Step(f"s{i}") for i in range(4)]

    result = await fan_out(
        steps, input_path="/dev/null", output_root=tmp_path, runner=runner
    )

    assert result.n_survived == 4
    assert state["peak"] == 1


@pytest.mark.asyncio
async def test_fan_out_concurrency_tracks_scheduler_budget(tmp_path: Path) -> None:
    """Concurrency follows the scheduler budget, not a fixed fan-out ceiling.

    Raising ``max_processes`` to 3 lets exactly three of the five ready steps
    overlap: the peak tracks the shared budget (never four), confirming the
    scheduler — not fan-out — decides capacity.
    """
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(3))
    runner, state = _concurrency_probe()
    steps = [_Step(f"s{i}") for i in range(5)]

    result = await fan_out(
        steps, input_path="/dev/null", output_root=tmp_path, runner=runner
    )

    assert result.n_survived == 5
    assert state["peak"] == 3


@pytest.mark.asyncio
async def test_fan_out_reserves_from_the_process_global_scheduler(
    tmp_path: Path,
) -> None:
    """fan-out reuses the process singleton and releases every lease.

    The instance fan-out reserves against is the very one every other executor
    (plan execution included) gets from ``get_process_resource_scheduler`` — one
    shared authority, not a fan-out-private pool — and it is left quiescent so
    the next run/loop can take ownership cleanly.
    """
    seeded = resource_scheduler.get_process_resource_scheduler(
        tmp_path, budget=_budget(2)
    )
    steps = [_Step(f"s{i}") for i in range(2)]

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    result = await fan_out(
        steps, input_path="/dev/null", output_root=tmp_path, runner=runner
    )

    assert result.n_survived == 2
    assert resource_scheduler._PROCESS_SCHEDULER is seeded
    assert seeded.quiescent


@pytest.mark.asyncio
async def test_fan_out_voluntary_max_parallel_serialises_under_scheduler(
    tmp_path: Path,
) -> None:
    """An explicit ``max_parallel`` is a voluntary ceiling on top of the scheduler.

    With ``max_processes=4`` the scheduler would admit four steps at once, but
    ``max_parallel=1`` (the ``--max-parallel 1`` GPU-serialisation knob) holds
    the fairness window to one, so the steps still run one at a time — the knob
    survives the cut-over to scheduler-gated capacity.
    """
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(4))
    runner, state = _concurrency_probe()
    steps = [_Step(f"s{i}") for i in range(4)]

    result = await fan_out(
        steps,
        input_path="/dev/null",
        output_root=tmp_path,
        max_parallel=1,
        runner=runner,
    )

    assert result.n_survived == 4
    assert state["peak"] == 1


def test_resolve_parallel_window_defaults_above_max_processes() -> None:
    # Default window sits one above the scheduler's own bound, so it is a
    # fairness guard that never binds before the global authority.
    assert _resolve_parallel_window(None, _budget(4)) == 5


def test_resolve_parallel_window_allows_voluntary_cap_below_max_processes() -> None:
    # An explicit ceiling may sit at or below max_processes on purpose: a caller
    # is free to admit fewer steps than the scheduler would (never more).
    assert _resolve_parallel_window(1, _budget(4)) == 1
    assert _resolve_parallel_window(4, _budget(4)) == 4


def test_resolve_parallel_window_honours_explicit_above_max_processes() -> None:
    assert _resolve_parallel_window(10, _budget(4)) == 10


@pytest.mark.parametrize("bad", [0, -1, True, 2.0])
def test_resolve_parallel_window_rejects_non_positive_int(bad) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _resolve_parallel_window(bad, _budget(4))
