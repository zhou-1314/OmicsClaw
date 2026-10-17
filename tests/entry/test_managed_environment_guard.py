"""Common kernels and the scientific Job worker cannot bypass a global fence."""

import asyncio
import json
import multiprocessing
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.environment_coordination import (
    CoordinationError,
    EnvironmentCoordinator,
    root_evidence,
)
from omicsclaw.entry.environment_jobs import ScienceEnvironmentGuard
from omicsclaw.entry.environment_runtime import ProcessEnvironmentGuard
from omicsclaw.kernel.manager import PersistentKernelManager
from tests.entry.test_persistent_jobs_manager import manager_for


def test_original_local_kernel_manager_registers_actual_process_and_blocks_new_cells(
    tmp_path,
    monkeypatch,
):
    injected = tmp_path / "inherited-path"
    injected.mkdir()
    (injected / "legacy_import_canary.py").write_text("value = 42")
    monkeypatch.setenv("PYTHONPATH", str(injected))

    async def run():
        prefix = tmp_path / "venv"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            "--system-site-packages",
            str(prefix),
        )
        assert await process.wait() == 0
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        guard = ProcessEnvironmentGuard(tmp_path, coordinator=coordinator)
        manager = PersistentKernelManager(
            tmp_path, python=str(prefix / "bin/python"), environment_guard=guard
        )
        try:
            first = await manager.execute(
                "local",
                "from legacy_import_canary import value; local_marker = value; print(local_marker)",
            )
            assert first.status == "ok" and "42" in first.stdout
            roots = [root_evidence(str(prefix), role="install_write")]
            impact = coordinator.impact(roots)
            assert len(impact) == 1 and impact[0]["has_analysis"] == 1
            assert json.loads(impact[0]["roots_json"])[0]["canonicalPath"] == "/"
            assert (
                json.loads(impact[0]["identity_json"])["identity"]["reason"]
                == "legacy_python_scope_unverified"
            )
            assert json.loads(impact[0]["process_json"])["pid"] > 0
            coordinator.fence(
                "maintenance",
                roots,
                {"ownerId": "fixture", "ownerEpoch": 1},
                coordinator.impact_digest(impact),
            )
            with pytest.raises(CoordinationError, match="frozen"):
                await manager.execute("local", "local_marker = 'must_not_run'")
            # Explicit shutdown remains available under a maintenance fence.
            await manager.aclose()
            retained = coordinator.impact(roots)
            assert len(retained) == 1 and retained[0]["uncertain_exit"] == 1
        finally:
            await manager.aclose()

    asyncio.run(run())


def _worker(directory, request, ready, finish, result):
    from omicsclaw.entry import environment_jobs

    environment_jobs.coordinator_directory = lambda: Path(directory)
    try:
        reservation = environment_jobs.start_worker_reservation(request)
        ready.set()
        finish.wait(10)
        environment_jobs.finish_worker_reservation(reservation)
        result.put("finished")
    except Exception as error:  # noqa: BLE001 - send subprocess failures to the parent assertion
        result.put(str(error))
        ready.set()


def test_job_admission_precedes_local_receipt_and_real_worker_drains_after_fence(
    tmp_path, monkeypatch
):
    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        process_guard = ProcessEnvironmentGuard(tmp_path, coordinator=coordinator)
        prefix = tmp_path / "science"
        created = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            "--system-site-packages",
            str(prefix),
        )
        assert await created.wait() == 0
        interpreter = prefix / "bin/python"
        guard = ScienceEnvironmentGuard(process_guard, interpreter=interpreter)
        manager = manager_for(tmp_path)
        bound = []
        release_runner = asyncio.Event()
        science = {
            "python": str(interpreter),
            "payloadRoot": str(tmp_path),
            "payloadDigest": "a" * 64,
        }

        class Runner:
            async def reserve_admission(self, record):
                await guard.reserve(record)

            def commit_admission(self, record):
                guard.committed(record)

            def abandon_admission(self, record):
                guard.abandoned(record)

            async def __call__(self, record, ctx):
                bound.append(record)
                await release_runner.wait()

        runner = Runner()
        manager.register_runner("fixture", runner)
        claim_job = manager.store.claim_job

        def claim(record):
            row = coordinator.get(guard.key(record.id))
            assert row["state"] == "pending" and row["process_json"] is None
            assert manager.store.get_job(record.id) is None
            return claim_job(record)

        monkeypatch.setattr(manager.store, "claim_job", claim)
        record = await manager.create_job(
            kind="fixture",
            skill="fixture",
            inputs={"_science": science},
            source_request_id="once",
        )
        row = coordinator.get(guard.key(record.id))
        roots = [root_evidence(str(prefix), role="install_write")]
        request = {
            "jobId": record.id,
            "skill": record.skill,
            "inputs": {},
            "workspace": record.target_workdir,
            "payloadRoot": str(tmp_path),
            "payloadDigest": "a" * 64,
            "environmentReservation": guard.request(record),
        }
        guard.dispatching(record)
        impact = coordinator.impact(roots)
        coordinator.fence(
            "maintenance",
            roots,
            {"ownerId": "fixture", "ownerEpoch": 1},
            coordinator.impact_digest(impact),
        )
        ctx = multiprocessing.get_context("spawn")
        ready, finish, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(
            target=_worker,
            args=(str(coordinator.directory), request, ready, finish, queue),
        )
        ctx.set_executable(str(interpreter))
        try:
            process.start()
        finally:
            ctx.set_executable(sys.executable)
        try:
            assert await asyncio.to_thread(ready.wait, 10)
            assert process.is_alive()
            active = coordinator.get(row["reservation_id"])
            assert active["dispatch_state"] == "started"
            assert json.loads(active["process_json"])["pid"] == process.pid
            coordinator.reap_finished_jobs()
            assert coordinator.get(row["reservation_id"])["state"] == "active"
            finish.set()
            assert await asyncio.to_thread(queue.get, True, 10) == "finished"
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == 0
            coordinator.reap_finished_jobs()
            retained = coordinator.get(row["reservation_id"])
            assert retained["state"] == "finished" and retained["uncertain_exit"] == 1
        finally:
            finish.set()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                process.join()
            release_runner.set()
            await asyncio.gather(
                *(live.task for live in manager._live.values() if live.task)
            )

    asyncio.run(run())


def test_unsent_job_can_close_but_unknown_send_cannot(tmp_path):
    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        process_guard = ProcessEnvironmentGuard(tmp_path, coordinator=coordinator)
        guard = ScienceEnvironmentGuard(process_guard, interpreter=Path(sys.executable))
        record = SimpleNamespace(
            id="job",
            skill="fixture",
            inputs={"_science": {}},
            target_workdir=str(tmp_path),
        )
        await guard.reserve(record)
        guard.committed(record)
        guard.dispatching(record)
        guard.finished(record)
        row = coordinator.get(guard.key(record.id))
        assert row["state"] == "active" and row["dispatch_state"] == "dispatching"
        with pytest.raises(CoordinationError, match="unconfirmed"):
            guard.dispatching(record)
        unused = SimpleNamespace(**{**vars(record), "id": "never_sent"})
        await guard.reserve(unused)
        guard.committed(unused)
        guard.finished(unused)
        assert coordinator.get(guard.key(unused.id))["state"] == "closed"

    asyncio.run(run())


def test_actual_local_recycle_waits_for_birth_exit_and_records_each_launch_intent(
    tmp_path,
):
    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        guard = ProcessEnvironmentGuard(tmp_path, coordinator=coordinator)
        seen = []
        original = guard.register_kernel

        def registered(state, pid):
            row = coordinator.get(guard.kernels[id(state)]["id"])
            seen.append((row["reservation_id"], row["dispatch_state"]))
            return original(state, pid)

        guard.register_kernel = registered
        manager = PersistentKernelManager(
            tmp_path, python=sys.executable, environment_guard=guard
        )
        try:
            for number in range(3):
                result = await manager.execute("local", f"print({number})")
                assert result.status == "ok", result
                manager.state_for("local").restart_needed = "test planned recycle"
            assert len(seen) == 3 and len({item[0] for item in seen}) == 3
            assert all(item[1] == "dispatching" for item in seen)
        finally:
            await manager.aclose()
        assert all(coordinator.get(rid)["uncertain_exit"] == 1 for rid, _ in seen)

    asyncio.run(run())
