"""A remote execution limit survives loss of its observing manager."""

import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.desktop.job_service import create_job_from_document
from omicsclaw.entry.desktop.jobs_manager import JobsManager, JobStore
from omicsclaw.memory.database import Database
from omicsclaw.remote.bridge import RemoteJobsBridge
from omicsclaw.remote.local import LocalTargetSpawner
from omicsclaw.remote.plane import RemotePlaneBinding


@pytest.fixture
def deadline_python():
    explicit = os.environ.get("OMICSCLAW_DEADLINE_PYTHON")
    if explicit:
        return explicit
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        pytest.skip(
            "selected interpreter lacks native pidfd support; production runtime is tested separately"
        )
    return sys.executable


def test_owned_hard_deadline_stops_term_ignoring_job_after_observer_detaches(
    tmp_path, deadline_python
):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )
    bridge = RemoteJobsBridge(
        plane,
        target_alias="controller",
        target_root=str(tmp_path),
        poll_interval_s=0.01,
    )
    app = SimpleNamespace(config=SimpleNamespace(workspace=str(tmp_path)), plans=None)
    manager = JobsManager(
        app,
        store=JobStore(database),
        remote_runtime=bridge,
        register_default_runners=False,
    )
    manager.register_runner("command_fixture", bridge)

    async def run():
        record = await create_job_from_document(
            manager,
            {
                "kind": "command_fixture",
                "source_request_id": "offline-hard-deadline",
                "hard_timeout_s": 0.2,
                "inputs": {
                    "scheduler": "none",
                    "deadline_python": deadline_python,
                    "command": "printf '1\\n' >> execute-count.txt; printf ready > armed.txt; trap '' TERM; sleep 4; printf late > late.txt",
                },
            },
        )
        deadline = asyncio.get_running_loop().time() + 8
        directory = Path(record.target_workdir)
        while not (directory / "armed.txt").is_file():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        await manager.shutdown_observations()
        row = plane.job_store().by_local_job(record.id)
        assert row is not None
        while True:
            report = await plane.status(str(row.id))
            if report.state in {"done", "failed", "canceled"}:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert (
            report.state == "failed"
            and report.exit_code in {137, 143}
            and not (directory / "late.txt").exists()
        )
        await manager.reconcile_remote_jobs()
        restored = manager.store.get_job(record.id)
        assert (
            restored.error != "observation_detached" and restored.started_at is not None
        )

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_verified_recovery_clears_detachment_and_recovers_process_start(tmp_path):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )
    bridge = RemoteJobsBridge(
        plane, target_alias="controller", target_root=str(tmp_path), poll_interval_s=5
    )
    app = SimpleNamespace(config=SimpleNamespace(workspace=str(tmp_path)), plans=None)
    manager = JobsManager(
        app,
        store=JobStore(database),
        remote_runtime=bridge,
        register_default_runners=False,
    )
    manager.register_runner("command_fixture", bridge)

    async def run():
        record = await create_job_from_document(
            manager,
            {
                "kind": "command_fixture",
                "source_request_id": "natural-recovery",
                "inputs": {
                    "scheduler": "none",
                    "command": "printf ready > armed.txt; sleep .2",
                },
            },
        )
        directory = Path(record.target_workdir)
        expires = asyncio.get_running_loop().time() + 5
        while not (directory / "armed.txt").exists():
            assert asyncio.get_running_loop().time() < expires
            await asyncio.sleep(0.01)
        await manager.shutdown_observations()
        row = plane.job_store().by_local_job(record.id)
        while (await plane.status(str(row.id))).state != "done":
            assert asyncio.get_running_loop().time() < expires
            await asyncio.sleep(0.01)
        assert manager.store.get_job(record.id).error == "observation_detached"
        await manager.reconcile_remote_jobs()
        restored = manager.store.get_job(record.id)
        assert (
            restored.status == "succeeded"
            and restored.error == ""
            and record.created_at - 1 <= restored.started_at <= restored.finished_at
        )

    try:
        asyncio.run(run())
    finally:
        database.close()


@pytest.mark.parametrize(
    "command,seconds,state,code",
    [
        ("printf complete > result.txt", 2, "done", 0),
        ("(sleep 4; printf late > late.txt) & exit 0", 0.1, "failed", 124),
    ],
)
def test_original_handle_records_normal_exit_or_descendant_expiry(
    tmp_path, deadline_python, command, seconds, state, code
):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )

    async def run():
        result = await plane.submit(
            "controller",
            command,
            scheduler="none",
            target_workdir=str(tmp_path / "job"),
            hard_timeout_s=seconds,
            deadline_python=deadline_python,
        )
        expires = asyncio.get_running_loop().time() + 5
        while True:
            report = await plane.status(result.job_ref)
            if report.state in {"done", "failed"}:
                break
            assert asyncio.get_running_loop().time() < expires
            await asyncio.sleep(0.01)
        assert (
            report.state,
            report.exit_code,
            (tmp_path / "job" / "late.txt").exists(),
        ) == (state, code, False)

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_explicit_cancel_remains_distinct_from_hard_expiry(tmp_path, deadline_python):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )

    async def run():
        result = await plane.submit(
            "controller",
            "printf armed > armed.txt; trap '' TERM; sleep 4; printf late > late.txt",
            scheduler="none",
            target_workdir=str(tmp_path / "job"),
            hard_timeout_s=5,
            deadline_python=deadline_python,
        )
        expires = asyncio.get_running_loop().time() + 5
        while not (tmp_path / "job" / "armed.txt").exists():
            assert asyncio.get_running_loop().time() < expires
            await asyncio.sleep(0.01)
        await plane.cancel(result.job_ref)
        while True:
            report = await plane.status(result.job_ref)
            if report.state == "canceled":
                break
            assert asyncio.get_running_loop().time() < expires
            await asyncio.sleep(0.01)
        assert (
            report.exit_code in {137, 143}
            and not (tmp_path / "job" / "late.txt").exists()
        )

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_missing_deadline_capability_keeps_claim_unbound_before_command(tmp_path):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )

    async def run():
        with pytest.raises(RuntimeError, match="deadline_capability_unconfirmed"):
            await plane.submit(
                "controller",
                "printf late > late.txt",
                scheduler="none",
                target_workdir=str(tmp_path / "job"),
                request_id="unsupported",
                hard_timeout_s=1,
                deadline_python="/bin/false",
            )
        row = plane.job_store().by_request("unsupported")
        assert row.handle().pgid is None and not (tmp_path / "job").exists()

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_changed_execution_limit_conflicts_before_second_launch(
    tmp_path, deadline_python
):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )

    async def run():
        arguments = {
            "scheduler": "none",
            "target_workdir": str(tmp_path / "job"),
            "request_id": "limit",
            "deadline_python": deadline_python,
        }
        first = await plane.submit(
            "controller",
            "printf '1\\n' >> execute-count.txt",
            hard_timeout_s=1,
            **arguments,
        )
        with pytest.raises(ValueError, match="payload_conflict"):
            await plane.submit(
                "controller",
                "printf '1\\n' >> execute-count.txt",
                hard_timeout_s=2,
                **arguments,
            )
        while (await plane.status(first.job_ref)).state == "running":
            await asyncio.sleep(0.01)
        assert (tmp_path / "job" / "execute-count.txt").read_text() == "1\n"

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_supervisor_evidence_conflict_cannot_publish_terminal(
    tmp_path, deadline_python
):
    database = Database(tmp_path / "owned.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )

    async def run():
        result = await plane.submit(
            "controller",
            "printf broken > _omicsclaw_nonce; sleep .2",
            scheduler="none",
            target_workdir=str(tmp_path / "job"),
            hard_timeout_s=0.05,
            deadline_python=deadline_python,
        )
        await asyncio.sleep(0.4)
        report = await plane.status(result.job_ref)
        assert (
            report.state == "unknown"
            and not (tmp_path / "job" / "_omicsclaw_exit_code").exists()
        )

    try:
        asyncio.run(run())
    finally:
        database.close()


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, float("nan"), float("inf"), 604801, "1"]
)
def test_invalid_execution_limit_is_refused(value):
    from omicsclaw.remote.jobs import validate_hard_timeout

    with pytest.raises(ValueError, match="invalid_hard_timeout"):
        validate_hard_timeout(value)


def test_legacy_wrapper_is_unlimited_and_slurm_uses_native_minute_rounding():
    from omicsclaw.remote.jobs import (
        NOHUP_WRAPPER,
        build_job_scripts,
        build_submit_command,
    )

    assert build_job_scripts("true", "nohup")["job.sh"] == NOHUP_WRAPPER
    assert "--time=2 " in build_submit_command("/own/task", "slurm", hard_timeout_s=61)


def test_reading_owned_evidence_can_update_access_time_without_content_drift(tmp_path):
    from omicsclaw.remote.deadline import read_owned

    evidence = tmp_path / "proof"
    evidence.write_bytes(b"owned proof")
    os.utime(evidence, ns=(1, evidence.stat().st_mtime_ns))
    assert read_owned(str(evidence), 64) == b"owned proof"


def test_hardlinked_exit_proof_is_refused(tmp_path):
    from omicsclaw.remote.deadline import read_owned

    evidence = tmp_path / "proof"
    evidence.write_bytes(b"owned proof")
    os.link(evidence, tmp_path / "second-link")
    with pytest.raises(ValueError, match="deadline_evidence_owner"):
        read_owned(str(evidence), 64)


def test_persisted_execution_limit_cannot_be_changed_after_admission(tmp_path):
    from omicsclaw.entry.desktop.jobs_manager import JobRecord

    database = Database(tmp_path / "owned.db")
    try:
        store = JobStore(database)
        store.insert_job(JobRecord(id="limited", hard_timeout_s=1))
        with pytest.raises(ValueError, match="immutable"):
            store.update_job("limited", hard_timeout_s=2)
        assert store.get_job("limited").hard_timeout_s == 1
    finally:
        database.close()


def test_old_jobs_table_migrates_to_nullable_unlimited_execution_limit(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, skill TEXT, inputs_json TEXT, status TEXT, error TEXT, created_at REAL, started_at REAL, finished_at REAL)"
        )
        connection.execute(
            "INSERT INTO jobs (id,kind,inputs_json,status) VALUES ('legacy','fixture','{}','succeeded')"
        )
    database = Database(path)
    try:
        assert JobStore(database).get_job("legacy").hard_timeout_s is None
    finally:
        database.close()
