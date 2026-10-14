"""Observed facts require kind-specific proof before source projection."""

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.memory.database import Database
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.plane import RemotePlaneBinding
from omicsclaw.remote.reconcile import refresh_in_flight


@pytest.mark.parametrize("kind", ["nohup", "slurm"])
def test_missing_kind_proof_never_promotes_remote_source_or_callback(tmp_path, kind):
    plane = RemotePlaneBinding(Database(tmp_path / "own.db"), workspace=tmp_path)
    ref = plane.job_store().record(
        RemoteJobHandle(
            kind=kind,
            host="fixture",
            workdir="/task",
            pgid=123 if kind == "nohup" else None,
            job_id="123" if kind == "slurm" else None,
            local_job_id="business",
        )
    )
    facts = []

    class Executor:
        async def status(self, handle):
            return SimpleNamespace(state="done", exit_code=0, identity_verified=True)

    plane.executor_for = lambda _: Executor()
    asyncio.run(
        refresh_in_flight(plane, on_remote_status=lambda *value: facts.append(value))
    )
    assert (plane.job_store().get(str(ref)).status, facts) == (
        "unknown",
        [("business", "fixture", "unknown")],
    )


def test_scheduler_proof_has_cluster_user_submit_nonce_without_fake_pid():
    proof = {
        "uid": 1000,
        "user": "owner",
        "cluster": "site",
        "job_id": "123",
        "submit_time": "2026-10-15T10:00:00",
        "nonce": "a" * 32,
    }
    handle = RemoteJobHandle(
        kind="slurm",
        host="fixture",
        workdir="/task",
        job_id="123",
        cluster="site",
        scheduler_identity=proof,
    )
    restored = RemoteJobHandle.from_json(handle.to_json())
    assert dict(restored.scheduler_identity) == proof and restored.pgid is None
    with pytest.raises(ValueError, match="scheduler_identity"):
        RemoteJobHandle(
            kind="slurm",
            host="fixture",
            workdir="/task",
            job_id="124",
            cluster="site",
            scheduler_identity=proof,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", 2000),
        ("user", "other"),
        ("cluster", "other"),
        ("submit_time", "2026-10-15T11:00:00"),
        ("nonce", "b" * 32),
    ],
)
def test_scheduler_mismatched_proof_never_confirms_a_fact(field, value):
    from omicsclaw.remote.scheduler import parse_observation

    proof = {
        "uid": 1000,
        "user": "owner",
        "cluster": "site",
        "job_id": "123",
        "submit_time": "2026-10-15T10:00:00",
        "nonce": "a" * 32,
    }
    output = (
        "UID=1000\nUSER=owner\nCLUSTER=site\nLIVE\nJobId=123 UserId=owner(1000) JobName=omicsclaw_"
        + "a" * 32
        + " SubmitTime=2026-10-15T10:00:00 JobState=RUNNING\nACCOUNTING\n"
    )
    _, result = parse_observation(
        output, job_id="123", nonce=proof["nonce"], expected={**proof, field: value}
    )
    assert result.state == "unknown" and not result.identity_verified


def test_scheduler_accounting_requires_exact_identity_exit_and_unique_record():
    from omicsclaw.remote.scheduler import parse_observation

    proof = {
        "uid": 1000,
        "user": "owner",
        "cluster": "site",
        "job_id": "123",
        "submit_time": "2026-10-15T10:00:00",
        "nonce": "a" * 32,
    }
    row = (
        "123|site|owner|1000|2026-10-15T10:00:00|omicsclaw_"
        + "a" * 32
        + "|COMPLETED|0:0\n"
    )
    output = "UID=1000\nUSER=owner\nCLUSTER=site\nLIVE\n\nACCOUNTING\n"
    _, result = parse_observation(
        output + row, job_id="123", nonce=proof["nonce"], expected=proof
    )
    assert (result.state, result.exit_code, result.identity_verified) == (
        "done",
        0,
        True,
    )
    _, duplicated = parse_observation(
        output + row + row, job_id="123", nonce=proof["nonce"], expected=proof
    )
    assert duplicated.state == "unknown" and not duplicated.identity_verified


def test_missing_scheduler_proof_never_queries_or_signals(tmp_path):
    from omicsclaw.remote.plane import RemoteExecutor

    class NoTransport:
        async def spawn(self, *args, **kwargs):
            raise AssertionError("unproved scheduler operation")

    async def scenario():
        executor = RemoteExecutor("fixture", NoTransport())
        handle = RemoteJobHandle(
            kind="slurm", host="fixture", workdir=str(tmp_path), job_id="123"
        )
        assert (await executor.status(handle)).state == "unknown"
        assert (await executor.cancel(handle)).state == "unknown"

    asyncio.run(scenario())


def test_truncated_scheduler_page_cannot_confirm_its_first_matching_record():
    from omicsclaw.remote.scheduler import MAX_SCHEDULER_BYTES, parse_observation

    row = (
        "JobId=123 UserId=owner(1000) JobName=omicsclaw_"
        + "a" * 32
        + " SubmitTime=2026-10-15T10:00:00 JobState=RUNNING"
    )
    output = (
        "UID=1000\nUSER=owner\nCLUSTER=site\nLIVE\n"
        + row.ljust(MAX_SCHEDULER_BYTES + 1)
        + "\nACCOUNTING\n"
    )
    _, result = parse_observation(output, job_id="123", nonce="a" * 32)
    assert result.state == "unknown" and not result.identity_verified


def test_ordinary_worker_json_cannot_supply_executor_verification(tmp_path):
    from omicsclaw.remote.plane import RemoteExecutor
    from omicsclaw.tools.builtin.remote import ExecOutcome

    class WorkerTextTransport:
        async def spawn(self, *args, **kwargs):
            return ExecOutcome(
                output='{"state":"done","exit_code":0,"identity_verified":true}',
                exit_code=0,
            )

    handle = RemoteJobHandle(
        kind="nohup",
        host="fixture",
        workdir=str(tmp_path),
        pgid=123,
        process_identity={
            "uid": 1000,
            "pid": 123,
            "pgid": 123,
            "sid": 123,
            "start_ticks": 1,
            "host_id": "fixture",
            "boot_id": "a" * 32,
            "nonce": "b" * 32,
        },
    )
    report = asyncio.run(
        RemoteExecutor("fixture", WorkerTextTransport()).status(handle)
    )
    assert report.state == "unknown" and not report.identity_verified
