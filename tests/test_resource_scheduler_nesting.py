"""ADR 0062 — the scheduler rejects nested global acquisition instead of
deadlocking.

A governed Run must not submit a second global Resource ticket while it still
holds part of its envelope: the reentrant child would wait for capacity the
parent still holds while strict FIFO blocks everything behind it. The scheduler
now tracks which Run IDs hold a global Lease and fails a same-Run reacquisition
closed, before it enqueues. Untagged (``run_id is None``) Leases — the fixed
Workflow / Candidate / chain steps that hold no parent Lease — are never
guarded, so their ordinary concurrency is unaffected.
"""

from __future__ import annotations

import asyncio

import pytest

from omicsclaw.skill.resource_scheduler import (
    ExecutionResourceBudget,
    ExecutionResourceRequest,
    ExecutionResourceScheduler,
    NestedResourceAcquisitionError,
    ResourceTicket,
)

_REQUEST = ExecutionResourceRequest(
    cpu_cores=1,
    memory_mib=1,
    gpu_devices=0,
    threads=1,
    temporary_disk_mib=0,
)


def _scheduler(max_processes: int = 4) -> ExecutionResourceScheduler:
    return ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=8,
            memory_mib=8192,
            gpu_device_ids=(),
            threads=8,
            temporary_disk_mib=8192,
            max_processes=max_processes,
        )
    )


def _ticket(run_id: str | None) -> ResourceTicket:
    return ResourceTicket(request=_REQUEST, run_id=run_id)


@pytest.mark.asyncio
async def test_nested_same_run_id_acquire_is_rejected_not_deadlocked() -> None:
    """While a Run holds a Lease, a second acquire under it fails fast.

    ``asyncio.wait_for`` bounds the assertion: a regression that enqueued the
    reentrant ticket would hang here instead of raising, so the timeout is the
    no-deadlock proof.
    """
    sched = _scheduler(max_processes=4)

    async def reacquire_same_run() -> None:
        async with sched.reserve(_ticket("R")):
            pass

    async with sched.reserve(_ticket("R")):
        with pytest.raises(NestedResourceAcquisitionError):
            await asyncio.wait_for(reacquire_same_run(), timeout=2.0)

    assert sched.quiescent


@pytest.mark.asyncio
async def test_distinct_run_ids_hold_leases_concurrently() -> None:
    """Different Runs are not each other's nesting — both may hold at once."""
    sched = _scheduler(max_processes=4)
    async with sched.reserve(_ticket("A")):
        async with sched.reserve(_ticket("B")):
            assert not sched.quiescent
    assert sched.quiescent


@pytest.mark.asyncio
async def test_untagged_leases_are_never_guarded() -> None:
    """run_id=None (fan-out / chain / plan-step) concurrency is unaffected."""
    sched = _scheduler(max_processes=4)
    async with sched.reserve(ResourceTicket(request=_REQUEST)):
        # A second untagged acquisition is ordinary concurrency, never nesting.
        async with sched.reserve(ResourceTicket(request=_REQUEST)):
            assert not sched.quiescent
    assert sched.quiescent


@pytest.mark.asyncio
async def test_same_run_id_sequential_reacquire_is_allowed() -> None:
    """Acquire → release → acquire under one Run is sequential, not nested."""
    sched = _scheduler(max_processes=4)
    async with sched.reserve(_ticket("R")):
        pass
    async with sched.reserve(_ticket("R")):
        assert not sched.quiescent
    assert sched.quiescent


@pytest.mark.asyncio
async def test_rejection_leaves_guard_state_clean() -> None:
    """A rejected reentrant acquire must not corrupt the held Run's tracking.

    The rejection happens before enqueue and before any accounting, so the
    outer Lease still releases normally and the Run is fully re-acquirable.
    """
    sched = _scheduler(max_processes=4)

    async def reacquire_same_run() -> None:
        async with sched.reserve(_ticket("R")):
            pass

    async with sched.reserve(_ticket("R")):
        with pytest.raises(NestedResourceAcquisitionError):
            await reacquire_same_run()

    assert sched.quiescent
    assert not sched._active_lease_run_ids

    # Re-acquirable after the whole episode.
    async with sched.reserve(_ticket("R")):
        assert sched._active_lease_run_ids == {"R"}
    assert sched.quiescent
