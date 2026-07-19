from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from omicsclaw.skill import resource_scheduler
from omicsclaw.skill.resource_scheduler import (
    ExecutionResourceBudget,
    ExecutionResourceRequest,
    ExecutionResourceScheduler,
    ResourceConfigurationError,
    ResourceTicket,
    detect_execution_resource_budget,
    get_process_resource_scheduler,
)


def test_detect_execution_resource_budget_honors_operator_overrides(
    tmp_path: Path,
) -> None:
    budget = detect_execution_resource_budget(
        tmp_path,
        environ={
            "OMICSCLAW_PLAN_CPU_CORES": "8",
            "OMICSCLAW_PLAN_MEMORY_MIB": "16384",
            "OMICSCLAW_PLAN_GPU_DEVICE_IDS": "2,5",
            "OMICSCLAW_PLAN_THREADS": "8",
            "OMICSCLAW_PLAN_TEMPORARY_DISK_MIB": "32768",
            "OMICSCLAW_PLAN_MAX_PROCESSES": "3",
        },
    )

    assert budget.cpu_cores == 8
    assert budget.memory_mib == 16384
    assert budget.gpu_device_ids == ("2", "5")
    assert budget.threads == 8
    assert budget.temporary_disk_mib == 32768
    assert budget.max_processes == 3
    assert budget.to_public_dict()["gpu_devices"] == 2
    assert "gpu_device_ids" not in budget.to_public_dict()


def test_detect_execution_resource_budget_honors_process_cpu_affinity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_scheduler.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(
        resource_scheduler.os,
        "sched_getaffinity",
        lambda _pid: {3, 7},
        raising=False,
    )

    budget = detect_execution_resource_budget(tmp_path, environ={})

    assert budget.cpu_cores == 2
    assert budget.threads == 2
    assert budget.max_processes == 2


def test_process_resource_scheduler_is_shared_within_one_event_loop(
    tmp_path: Path,
) -> None:
    environ = {
        "OMICSCLAW_PLAN_CPU_CORES": "2",
        "OMICSCLAW_PLAN_MEMORY_MIB": "4096",
        "OMICSCLAW_PLAN_GPU_DEVICE_IDS": "0",
        "OMICSCLAW_PLAN_THREADS": "2",
        "OMICSCLAW_PLAN_TEMPORARY_DISK_MIB": "4096",
        "OMICSCLAW_PLAN_MAX_PROCESSES": "2",
    }

    async def scenario():
        first = get_process_resource_scheduler(tmp_path, environ=environ)
        second = get_process_resource_scheduler(tmp_path, environ=environ)
        return first, second

    first, second = asyncio.run(scenario())

    assert first is second
    assert first.budget.gpu_device_ids == ("0",)


def test_gpu_device_ids_reject_values_that_cannot_be_safely_forwarded(
    tmp_path: Path,
) -> None:
    with pytest.raises(ResourceConfigurationError, match="GPU device"):
        detect_execution_resource_budget(
            tmp_path,
            environ={"OMICSCLAW_PLAN_GPU_DEVICE_IDS": "GPU 0"},
        )

    budget = ExecutionResourceBudget(
        cpu_cores=2,
        memory_mib=2048,
        gpu_device_ids=("MIG-GPU-abc/1/2",),
        threads=2,
        temporary_disk_mib=2048,
    )
    assert budget.gpu_device_ids == ("MIG-GPU-abc/1/2",)


def test_cancelling_head_waiter_removes_ticket_and_unblocks_next() -> None:
    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=1,
            memory_mib=1024,
            gpu_device_ids=(),
            threads=1,
            temporary_disk_mib=256,
            max_processes=1,
        )
    )
    request = ExecutionResourceRequest(
        cpu_cores=1,
        memory_mib=1024,
        gpu_devices=0,
        threads=1,
        temporary_disk_mib=256,
    )

    async def scenario() -> None:
        holder = scheduler.reserve(request)
        await holder.__aenter__()

        async def reserve_once() -> None:
            async with scheduler.reserve(request):
                return

        cancelled_waiter = asyncio.create_task(reserve_once())
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        next_waiter = asyncio.create_task(reserve_once())
        await asyncio.sleep(0)
        await holder.__aexit__(None, None, None)
        await asyncio.wait_for(next_waiter, timeout=1)

    asyncio.run(scenario())


def test_resource_lease_preserves_run_and_step_correlation() -> None:
    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=1,
            memory_mib=1024,
            gpu_device_ids=(),
            threads=1,
            temporary_disk_mib=256,
            max_processes=1,
        )
    )
    request = ExecutionResourceRequest(1, 1024, 0, 1, 256)

    async def scenario() -> None:
        async with scheduler.reserve(
            ResourceTicket(request, run_id="a" * 32, step_id="step-1")
        ) as lease:
            assert lease.run_id == "a" * 32
            assert lease.step_id == "step-1"
            assert lease.request == request

    asyncio.run(scenario())


def test_closed_loop_cannot_rebind_while_old_scheduler_owns_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_scheduler, "_PROCESS_SCHEDULER", None)
    monkeypatch.setattr(resource_scheduler, "_PROCESS_SCHEDULER_LOOP", None)
    budget = ExecutionResourceBudget(1, 1024, (), 1, 256, 1)
    request = ExecutionResourceRequest(1, 1024, 0, 1, 256)
    old_loop = asyncio.new_event_loop()

    async def acquire_without_release():
        scheduler = get_process_resource_scheduler(tmp_path, budget=budget)
        context = scheduler.reserve(request)
        await context.__aenter__()
        return scheduler, context

    old_scheduler, _context = old_loop.run_until_complete(acquire_without_release())
    old_loop.close()

    async def attempt_rebind() -> None:
        with pytest.raises(ResourceConfigurationError, match="still owns capacity"):
            get_process_resource_scheduler(tmp_path, budget=budget)

    asyncio.run(attempt_rebind())
    assert old_scheduler.quiescent is False


def test_second_live_loop_cannot_create_an_independent_capacity_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_scheduler, "_PROCESS_SCHEDULER", None)
    monkeypatch.setattr(resource_scheduler, "_PROCESS_SCHEDULER_LOOP", None)
    budget = ExecutionResourceBudget(1, 1024, (), 1, 256, 1)
    ready = threading.Event()
    stop = threading.Event()
    errors: list[BaseException] = []

    def owner_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def own() -> None:
            get_process_resource_scheduler(tmp_path, budget=budget)
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.01)

        try:
            loop.run_until_complete(own())
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=owner_thread)
    thread.start()
    assert ready.wait(timeout=2)
    try:

        async def contender() -> None:
            with pytest.raises(ResourceConfigurationError, match="another live"):
                get_process_resource_scheduler(tmp_path, budget=budget)

        asyncio.run(contender())
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []


def test_quarantined_scheduler_retains_uncertain_lease_and_rejects_waiters() -> None:
    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(1, 1024, (), 1, 256, 1)
    )
    request = ExecutionResourceRequest(1, 1024, 0, 1, 256)

    async def scenario() -> None:
        context = scheduler.reserve(request)
        lease = await context.__aenter__()
        await scheduler.quarantine(lease)
        await context.__aexit__(None, None, None)
        assert scheduler.ready is False
        assert scheduler.quiescent is False
        with pytest.raises(ResourceConfigurationError, match="quarantined"):
            async with scheduler.reserve(request):
                raise AssertionError("quarantined scheduler granted capacity")

    asyncio.run(scenario())
