"""ADR 0061 D2 — ``run_skill_via_shared_runner`` reserves from the one global
Execution Resource Scheduler.

The chain shared runner is the launch path for agent tool dispatch, Bench
``execute_omicsclaw`` and the auto-prepare chain. It used to spawn a skill
subprocess with no capacity awareness at all, so several concurrent callers on
the Backend event loop could overcommit the host the governed path is trying to
bound. It now holds one process slot from ``get_process_resource_scheduler``
for the lifetime of each subprocess, so concurrency is capped by the shared
budget's ``max_processes`` — the single capacity authority (ADR 0061 D2).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import omicsclaw.skill.chain as chain_mod
import omicsclaw.skill.runner as runner_mod
from omicsclaw.skill import resource_scheduler
from omicsclaw.skill.resource_scheduler import ExecutionResourceBudget


@pytest.fixture(autouse=True)
def _reset_process_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the per-event-loop scheduler singleton for each test."""
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


def _peak_probe_run_skill(hold_seconds: float = 0.1):
    """A fake ``run_skill`` recording the peak number of concurrent subprocesses.

    Each invocation (which the chain runs in a worker thread via ``to_thread``)
    bumps a shared counter, holds briefly so genuine overlap is observable, then
    decrements. ``state["peak"]`` is the concurrency the scheduler permitted.
    """

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class _Result:
        stdout = ""
        stderr = ""
        adapter_exit_code = 0
        success = True

        def __init__(self, output_dir: str) -> None:
            self.output_path = output_dir

    def fake_run_skill(*args, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(hold_seconds)
        with lock:
            state["active"] -= 1
        return _Result(kwargs.get("output_dir"))

    return fake_run_skill, state


@pytest.mark.asyncio
async def test_chain_runner_concurrency_is_bounded_by_global_scheduler(
    tmp_path, monkeypatch
):
    """Five concurrent chain runs never exceed the budget's ``max_processes``.

    Regression guard for the third-capacity-authority gap: before the fix the
    chain reserved nothing, so all five subprocesses ran at once.
    """
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(2))
    fake_run_skill, state = _peak_probe_run_skill()
    monkeypatch.setattr(runner_mod, "run_skill", fake_run_skill)
    monkeypatch.setattr(chain_mod, "lookup_skill_info", lambda key: {"alias": key})

    results = await asyncio.gather(
        *[
            chain_mod.run_skill_via_shared_runner(
                skill_key="fake-skill",
                input_path=None,
                session_path=None,
                mode="demo",
                out_dir=tmp_path / f"run{i}",
            )
            for i in range(5)
        ]
    )

    assert all(r["success"] for r in results)
    assert state["peak"] == 2


@pytest.mark.asyncio
async def test_chain_runner_concurrency_tracks_the_budget(tmp_path, monkeypatch):
    """Peak concurrency follows the shared budget, not a fixed chain cap."""
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(4))
    fake_run_skill, state = _peak_probe_run_skill()
    monkeypatch.setattr(runner_mod, "run_skill", fake_run_skill)
    monkeypatch.setattr(chain_mod, "lookup_skill_info", lambda key: {"alias": key})

    results = await asyncio.gather(
        *[
            chain_mod.run_skill_via_shared_runner(
                skill_key="fake-skill",
                input_path=None,
                session_path=None,
                mode="demo",
                out_dir=tmp_path / f"run{i}",
            )
            for i in range(6)
        ]
    )

    assert all(r["success"] for r in results)
    assert state["peak"] == 4


@pytest.mark.asyncio
async def test_chain_runner_sequential_calls_do_not_deadlock_at_one_slot(
    tmp_path, monkeypatch
):
    """Two sequential runs under a one-slot budget both complete.

    The chain holds no outer lease, so each call fully releases its slot before
    the next acquires it — proving the reservation cannot self-wedge the FIFO
    even when ``max_processes`` is exhausted by a single run.
    """
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(1))

    class _Result:
        stdout = ""
        stderr = ""
        adapter_exit_code = 0
        success = True
        output_path = None

    monkeypatch.setattr(runner_mod, "run_skill", lambda *a, **k: _Result())
    monkeypatch.setattr(chain_mod, "lookup_skill_info", lambda key: {"alias": key})

    first = await chain_mod.run_skill_via_shared_runner(
        skill_key="fake-skill",
        input_path=None,
        session_path=None,
        mode="demo",
        out_dir=tmp_path / "a",
    )
    second = await chain_mod.run_skill_via_shared_runner(
        skill_key="fake-skill",
        input_path=None,
        session_path=None,
        mode="demo",
        out_dir=tmp_path / "b",
    )

    assert first["success"] is True
    assert second["success"] is True
    assert resource_scheduler._PROCESS_SCHEDULER.quiescent


@pytest.mark.asyncio
async def test_chain_runner_releases_slot_on_failure(tmp_path, monkeypatch):
    """A failed subprocess still releases its slot (scheduler left quiescent)."""
    resource_scheduler.get_process_resource_scheduler(tmp_path, budget=_budget(2))

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic subprocess crash")

    monkeypatch.setattr(runner_mod, "run_skill", boom)
    monkeypatch.setattr(chain_mod, "lookup_skill_info", lambda key: {"alias": key})

    with pytest.raises(RuntimeError, match="synthetic subprocess crash"):
        await chain_mod.run_skill_via_shared_runner(
            skill_key="fake-skill",
            input_path=None,
            session_path=None,
            mode="demo",
            out_dir=tmp_path / "run",
        )

    assert resource_scheduler._PROCESS_SCHEDULER.quiescent
