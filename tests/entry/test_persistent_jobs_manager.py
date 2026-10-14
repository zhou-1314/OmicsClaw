"""Canonical job admission reuses the original manager and projections."""

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.entry.desktop.jobs_manager import JobError, JobsManager, JobStore
from omicsclaw.memory.database import Database


@pytest.mark.parametrize("original", ["cancel_requested", "succeeded"])
def test_observation_cannot_regress_a_cancel_or_confirmed_terminal(
    tmp_path, monkeypatch, original
):
    from omicsclaw.entry.desktop import jobs_manager
    from omicsclaw.entry.desktop.jobs_manager import JobRecord

    manager = manager_for(tmp_path)
    manager.store.insert_job(
        JobRecord(id="stable", lifetime="external", status=original)
    )
    manager._remote_runtime = SimpleNamespace(
        plane=SimpleNamespace(
            job_store=lambda: SimpleNamespace(by_local_job=lambda _: None)
        )
    )

    async def refresh(plane, *, on_remote_status):
        on_remote_status("stable", "controller", "running")
        on_remote_status("stable", "controller", "unknown")
        return {}

    monkeypatch.setattr(jobs_manager, "refresh_in_flight", refresh)
    asyncio.run(manager.reconcile_remote_jobs())
    assert manager.store.get_job("stable").status == original


def manager_for(workspace):
    app = SimpleNamespace(
        config=SimpleNamespace(workspace=str(workspace), approval_timeout_s=None)
    )
    manager = JobsManager(
        app,
        store=JobStore(Database(workspace / "jobs.db")),
        register_default_runners=False,
    )
    return manager


def test_duplicate_canonical_create_returns_one_job_and_executes_once(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        executions = []

        async def runner(record, ctx):
            executions.append(record.id)

        manager.register_runner("fixture", runner)
        first = await manager.create_job(
            kind="fixture", inputs={"value": "same"}, source_request_id="once"
        )
        second = await manager.create_job(
            kind="fixture", inputs={"value": "same"}, source_request_id="once"
        )
        await asyncio.gather(
            *(live.task for live in manager._live.values() if live.task)
        )
        assert (
            first.id,
            second.id,
            executions,
            len(manager.run_store.list("run")),
        ) == (first.id, first.id, [first.id], 1)

    asyncio.run(scenario())


def test_same_source_with_changed_job_payload_refuses_without_second_task(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)

        async def runner(record, ctx):
            return None

        manager.register_runner("fixture", runner)
        original = await manager.create_job(
            kind="fixture", inputs={"value": "first"}, source_request_id="conflict"
        )
        try:
            with pytest.raises(JobError, match="job_payload_conflict"):
                await manager.create_job(
                    kind="fixture",
                    inputs={"value": "other"},
                    source_request_id="conflict",
                )
            assert [record.id for record in manager.list_jobs()] == [original.id]
        finally:
            await asyncio.gather(
                *(live.task for live in manager._live.values() if live.task)
            )

    asyncio.run(scenario())


def test_projection_crash_repairs_original_job_without_replaying_execution(
    tmp_path, monkeypatch
):
    async def scenario():
        original = manager_for(tmp_path)

        async def runner(record, ctx):
            raise AssertionError("unadmitted_replay")

        original.register_runner("fixture", runner)
        save = original.run_store.save
        monkeypatch.setattr(
            original.run_store,
            "save",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("owned_projection_crash")
            ),
        )
        with pytest.raises(OSError, match="owned_projection_crash"):
            await original.create_job(
                kind="fixture", source_request_id="projection", lifetime="external"
            )
        original_record = original.list_jobs()[0]
        monkeypatch.setattr(original.run_store, "save", save)
        observed = await original.create_job(
            kind="fixture", source_request_id="projection", lifetime="external"
        )
        assert (
            observed.id,
            len(original.run_store.list("run")),
            len(original.run_store.list("job")),
            len(original._live),
        ) == (original_record.id, 1, 1, 0)

    asyncio.run(scenario())


def test_duplicate_request_cannot_bypass_a_different_workspace(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)

        async def runner(record, ctx):
            return None

        manager.register_runner("fixture", runner)
        await manager.create_job(kind="fixture", source_request_id="workspace")
        try:
            with pytest.raises(
                JobError, match="workspace_does_not_match_backend_runtime"
            ):
                await manager.create_job(
                    kind="fixture",
                    source_request_id="workspace",
                    workspace=str(tmp_path / "other"),
                )
        finally:
            await asyncio.gather(
                *(live.task for live in manager._live.values() if live.task)
            )

    asyncio.run(scenario())


def test_conflicting_existing_run_identity_is_refused_without_overwriting(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)

        async def runner(record, ctx):
            return None

        manager.register_runner("fixture", runner)
        record = await manager.create_job(
            kind="fixture", source_request_id="run-conflict"
        )
        await asyncio.gather(
            *(live.task for live in manager._live.values() if live.task)
        )
        projection = manager.run_store.load(manager._run_ids[record.id])
        projection.execution_location = "remote:forged"
        manager.run_store.save(projection)
        with pytest.raises(JobError, match="job_projection_conflict"):
            await manager.create_job(kind="fixture", source_request_id="run-conflict")
        assert (
            manager.run_store.load(projection.run_id).execution_location
            == "remote:forged"
        )

    asyncio.run(scenario())


def test_projection_second_store_window_repairs_one_run_link_without_task(
    tmp_path, monkeypatch
):
    from omicsclaw.runs.models import Job

    async def scenario():
        manager = manager_for(tmp_path)

        async def runner(record, ctx):
            raise AssertionError("unadmitted_replay")

        manager.register_runner("fixture", runner)
        save = manager.run_store.save

        def fail_link(obj, **kwargs):
            if isinstance(obj, Job):
                raise OSError("owned_link_crash")
            return save(obj, **kwargs)

        monkeypatch.setattr(manager.run_store, "save", fail_link)
        with pytest.raises(OSError, match="owned_link_crash"):
            await manager.create_job(
                kind="fixture", source_request_id="link", lifetime="external"
            )
        claimed = manager.list_jobs()[0]
        run_id = manager.run_store.list("run")[0].run_id
        monkeypatch.setattr(manager.run_store, "save", save)
        observed = await manager.create_job(
            kind="fixture", source_request_id="link", lifetime="external"
        )
        assert (
            observed.id,
            [run.run_id for run in manager.run_store.list("run")],
            len(manager.run_store.list("job")),
            len(manager._live),
        ) == (claimed.id, [run_id], 1, 0)

    asyncio.run(scenario())


def test_projection_utf8_byte_guard_precedes_json_parsing(tmp_path):
    from omicsclaw.entry.desktop.jobs_manager import JobRecord

    store = JobStore(Database(tmp_path / "guard.db"))
    store.insert_job(JobRecord(id="own-byte-guard"))
    store.database.run(
        lambda conn: conn.execute(
            "UPDATE jobs SET projection_json=? WHERE id=?",
            ("{" + "中" * 200000, "own-byte-guard"),
        )
    )
    with pytest.raises(JobError, match="job_projection_too_large"):
        store.get_job("own-byte-guard")


def test_worker_source_events_are_idempotent_and_conflicts_refuse(tmp_path):
    from omicsclaw.entry.desktop.jobs_manager import JobRecord

    manager = manager_for(tmp_path)
    manager.store.insert_job(JobRecord(id="source-event", lifetime="external"))
    value = {"text": "source", "source_event_id": "worker:1"}
    first = manager._emit("source-event", "tool_output_chunk", value)
    second = manager._emit("source-event", "tool_output_chunk", value)
    with pytest.raises(JobError, match="job_source_event_conflict"):
        manager._emit("source-event", "tool_output_chunk", {**value, "text": "drift"})
    assert (first, second, len(manager.replay_events("source-event", 0))) == (1, 1, 1)


def test_external_observer_entry_does_not_claim_running_before_process_proof(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        entered = asyncio.Event()

        async def runner(record, ctx):
            entered.set()
            await asyncio.Event().wait()

        manager.register_runner("fixture", runner)
        record = await manager.create_job(
            kind="fixture", lifetime="external", source_request_id="no-fact"
        )
        await entered.wait()
        events = manager.replay_events(record.id, 0)
        assert manager.get_job(record.id).status == "queued"
        assert not any(
            kind in {"job.started", "execution.finished", "job.done"}
            for _, kind, _ in events
        )
        await manager.shutdown_observations()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "event_type", ["execution.finished", "artifact.created", "job.done"]
)
def test_event_payload_cannot_self_declare_a_verified_external_fact(
    tmp_path, event_type
):
    from omicsclaw.entry.desktop.jobs_manager import JobRecord
    from omicsclaw.remote.bridge import RemoteJobsBridge
    from omicsclaw.remote.jobs import RemoteJobHandle
    from omicsclaw.remote.plane import RemotePlaneBinding

    manager = manager_for(tmp_path)
    plane = RemotePlaneBinding(Database(tmp_path / "remote.db"), workspace=tmp_path)
    manager._remote_runtime = RemoteJobsBridge(plane)
    manager.store.insert_job(JobRecord(id="unobserved", lifetime="external"))
    handle = RemoteJobHandle(
        kind="nohup",
        host="fixture",
        workdir="/task",
        pgid=123,
        local_job_id="unobserved",
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
    plane.job_store().record(handle)
    with pytest.raises(JobError, match="external_observation_unconfirmed"):
        manager._emit(
            "unobserved",
            event_type,
            {
                "identity_verified": True,
                "identity_digest": handle.proof_digest(),
                "exit_code": 0,
                "path": "/task/result",
            },
        )
    assert manager.replay_events("unobserved", 0) == []


def test_lost_observation_revokes_an_earlier_external_projection_permission(tmp_path):
    from omicsclaw.entry.desktop.jobs_manager import JobRecord
    from omicsclaw.remote.bridge import RemoteJobsBridge
    from omicsclaw.remote.jobs import RemoteJobHandle
    from omicsclaw.remote.plane import RemotePlaneBinding
    from omicsclaw.tools.builtin.remote import StatusReport

    manager = manager_for(tmp_path)
    plane = RemotePlaneBinding(Database(tmp_path / "remote.db"), workspace=tmp_path)
    manager._remote_runtime = RemoteJobsBridge(plane)
    manager.store.insert_job(JobRecord(id="lost", lifetime="external"))
    handle = RemoteJobHandle(
        kind="nohup",
        host="fixture",
        workdir="/task",
        pgid=123,
        local_job_id="lost",
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
    ref = plane.job_store().record(handle)
    manager._observe_external_fact(
        "lost",
        str(ref),
        StatusReport(
            state="done",
            exit_code=0,
            identity_verified=True,
            identity_digest=handle.proof_digest(),
        ),
    )
    manager._observe_external_fact("lost", str(ref), StatusReport(detail="offline"))
    with pytest.raises(JobError, match="external_observation_unconfirmed"):
        manager._emit("lost", "artifact.created", {"path": "/task/result"})
