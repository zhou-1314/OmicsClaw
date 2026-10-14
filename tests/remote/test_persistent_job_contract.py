"""Persistent job requests reuse the existing stores and process handles."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from omicsclaw.entry.desktop.jobs_manager import JobRecord, JobStore
from omicsclaw.memory.database import Database
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.plane import RemoteExecutor, StatusReport
from omicsclaw.remote.ssh import SystemSshSpawner
from omicsclaw.remote.store import RemoteJobStore
from omicsclaw.tools.builtin.remote import ExecOutcome


def unbound_handle():
    return RemoteJobHandle(
        kind="nohup", host="fixture-host", workdir="/owned/job", local_job_id="j"
    )


def test_same_request_claim_returns_the_original_unlaunched_handle():
    db = Database(":memory:")
    store = RemoteJobStore(db)
    try:
        original = store.claim_submission("i:r", "a" * 64, unbound_handle())
        repeated = store.claim_submission("i:r", "a" * 64, unbound_handle())
        assert (repeated.id, repeated.status, repeated.pgid) == (
            original.id,
            "unknown",
            None,
        )
    finally:
        db.close()


def test_same_request_with_different_intent_refuses_before_launch():
    db = Database(":memory:")
    store = RemoteJobStore(db)
    try:
        store.claim_submission("i:r", "a" * 64, unbound_handle())
        with pytest.raises(ValueError, match="submission_payload_conflict"):
            store.claim_submission("i:r", "b" * 64, unbound_handle())
    finally:
        db.close()


def test_lost_observation_keeps_the_last_proven_terminal_in_the_original_row():
    db = Database(":memory:")
    store = RemoteJobStore(db)
    try:
        row_id = store.record(bound_handle(), status="done")
        store.update_status(row_id, "unknown")
        assert store.get(str(row_id)).status == "done"
    finally:
        db.close()


def test_same_host_external_job_is_not_an_in_process_orphan():
    db = Database(":memory:")
    store = JobStore(db)
    try:
        store.insert_job(
            JobRecord(
                id="external", status="running", runtime="local", lifetime="external"
            )
        )
        store.insert_job(JobRecord(id="foreground", status="running"))
        store.interrupt_orphans()
        assert (
            store.get_job("external").status,
            store.get_job("foreground").status,
        ) == (
            "running",
            "interrupted",
        )
    finally:
        db.close()


def test_verified_process_identity_survives_handle_serialization():
    identity = {
        "uid": 0,
        "host_id": "fixture-host",
        "boot_id": "b" * 32,
        "pid": 123,
        "start_ticks": 456,
        "pgid": 123,
        "sid": 123,
        "nonce": "c" * 32,
    }
    handle = RemoteJobHandle(
        kind="nohup",
        host="same-host",
        workdir="/owned/job",
        pgid=123,
        process_identity=identity,
    )
    assert RemoteJobHandle.from_json(handle.to_json()).process_identity == identity


def bound_handle(request_id="i:r", nonce="c" * 32):
    return replace(
        unbound_handle(),
        pgid=123,
        request_id=request_id,
        payload_hash="a" * 64,
        process_identity={
            "uid": 0,
            "host_id": "fixture-host",
            "boot_id": "b" * 32,
            "pid": 123,
            "start_ticks": 456,
            "pgid": 123,
            "sid": 123,
            "nonce": nonce,
        },
    )


def test_bound_handle_is_published_once_on_the_original_claim():
    db = Database(":memory:")
    store = RemoteJobStore(db)
    try:
        original = store.claim_submission("i:r", "a" * 64, unbound_handle())
        store.bind_submission(original.id, bound_handle())
        repeated = store.bind_submission(original.id, bound_handle())
        assert (repeated.id, repeated.handle().process_identity) == (
            original.id,
            bound_handle().process_identity,
        )
    finally:
        db.close()


def test_different_process_proof_cannot_replace_an_existing_bound_handle():
    db = Database(":memory:")
    store = RemoteJobStore(db)
    try:
        original = store.claim_submission("i:r", "a" * 64, unbound_handle())
        store.bind_submission(original.id, bound_handle())
        with pytest.raises(ValueError, match="submission_handle_conflict"):
            store.bind_submission(original.id, bound_handle(nonce="d" * 32))
    finally:
        db.close()


def test_unverified_legacy_handle_never_sends_a_cancel_signal():
    async def run():
        executor = RemoteExecutor("fixture-host", SystemSshSpawner())
        executor.run = AsyncMock(side_effect=AssertionError("unproved_signal"))
        legacy = RemoteJobHandle(
            kind="nohup", host="fixture-host", workdir="/owned/job", pgid=123
        )
        report = await executor.cancel(legacy)
        assert (report.state, executor.run.await_count) == ("unknown", 0)

    asyncio.run(run())


def test_unverified_legacy_handle_is_unknown_without_probing_a_reused_pid():
    async def run():
        executor = RemoteExecutor("fixture-host", SystemSshSpawner())
        executor.run = AsyncMock(side_effect=AssertionError("unproved_pid_status"))
        legacy = RemoteJobHandle(
            kind="nohup", host="fixture-host", workdir="/owned/job", pgid=123
        )
        report = await executor.status(legacy)
        assert (report.state, executor.run.await_count) == ("unknown", 0)

    asyncio.run(run())


def test_signal_ack_does_not_replace_a_still_running_status_with_canceled():
    async def run():
        executor = RemoteExecutor("fixture-host", SystemSshSpawner())
        executor.run = AsyncMock(return_value=ExecOutcome(exit_code=0, output=""))
        executor.status = AsyncMock(return_value=StatusReport(state="running"))
        report = await executor.cancel(bound_handle())
        assert (report.state, executor.status.await_count) == ("running", 1)

    asyncio.run(run())


def test_existing_unlaunched_claim_is_observed_without_a_second_upload_or_launch(
    tmp_path,
):
    from omicsclaw.remote.plane import RemotePlaneBinding
    from tests.remote._fakes import FakeSpawner

    spawner = FakeSpawner()
    db = Database(":memory:")
    plane = RemotePlaneBinding(db, spawner=spawner, workspace=tmp_path)
    try:
        spawner.on("_omicsclaw_submission_claim", "CLAIMED")
        spawner.on("_omicsclaw_launch_claim", RuntimeError("owned_before_ack"))
        with pytest.raises(RuntimeError, match="owned_before_ack"):
            asyncio.run(
                plane.submit(
                    "fixture-host",
                    "echo once",
                    request_id="no-proof",
                    target_workdir="/owned/job",
                )
            )
        count = len(spawner.sftp_batches())
        outcome = asyncio.run(
            plane.submit(
                "fixture-host",
                "echo once",
                request_id="no-proof",
                target_workdir="/owned/job",
            )
        )
        assert (
            outcome.pgid,
            len(spawner.sftp_batches()),
            sum("_omicsclaw_launch_claim" in call for call in spawner.ssh_commands()),
        ) == (None, count, 1)
    finally:
        db.close()


@pytest.mark.parametrize(
    "changed", ["command", "outputs", "input", "workdir", "scheduler", "host"]
)
def test_duplicate_full_intent_conflict_never_reaches_transport(tmp_path, changed):
    from omicsclaw.remote.plane import RemotePlaneBinding
    from tests.remote._fakes import FakeSpawner

    source = tmp_path / "input.txt"
    source.write_text("first")
    db = Database(":memory:")
    spawner = (
        FakeSpawner()
        .on("_omicsclaw_submission_claim", "CLAIMED")
        .on("_omicsclaw_launch_claim", RuntimeError("owned_before_ack"))
    )
    plane = RemotePlaneBinding(db, spawner=spawner, workspace=tmp_path)
    original = {
        "host": "fixture-host",
        "command": "echo once",
        "request_id": "full-intent",
        "target_workdir": "/owned/job",
        "inputs": (("input.txt", "input.txt"),),
        "outputs": ("one.txt",),
        "scheduler": "none",
    }
    try:
        with pytest.raises(RuntimeError, match="owned_before_ack"):
            asyncio.run(plane.submit(**original))
        calls = len(spawner.calls)
        modified = dict(original)
        if changed == "input":
            source.write_text("other")
        else:
            name = "target_workdir" if changed == "workdir" else changed
            modified[name] = {
                "command": "echo twice",
                "outputs": ("two.txt",),
                "target_workdir": "/owned/other",
                "scheduler": "slurm",
                "host": "other-host",
            }[name]
        with pytest.raises(ValueError, match="submission_payload_conflict"):
            asyncio.run(plane.submit(**modified))
        assert len(spawner.calls) == calls
    finally:
        db.close()
