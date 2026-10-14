"""Persistent job requests reuse the existing stores and process handles."""

import pytest

from omicsclaw.entry.desktop.jobs_manager import JobRecord, JobStore
from omicsclaw.memory.database import Database
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.store import RemoteJobStore


def unbound_handle():
    return RemoteJobHandle(
        kind="nohup", host="same-host", workdir="/owned/job", local_job_id="j"
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
