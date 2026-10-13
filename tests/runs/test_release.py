import asyncio
from types import SimpleNamespace

from omicsclaw.entry.desktop.jobs_manager import JobRecord, JobsManager
from omicsclaw.memory.database import Database
from omicsclaw.remote.bridge import RemoteJobsBridge
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.plane import RemotePlaneBinding
from omicsclaw.runs.models import Artifact, Job, Kernel, Run, Verification
from tests.entry.test_desktop_jobs import jobs_client, parse_sse


def test_unknown_remote_is_rechecked_and_syncs_run(tmp_path):
    plane = RemotePlaneBinding(Database(tmp_path / "remote.db"), workspace=tmp_path)
    outcome = SimpleNamespace(state="unknown")

    class Executor:
        async def status(self, handle):
            return outcome

    plane.executor_for = lambda alias: Executor()
    manager = JobsManager(
        SimpleNamespace(config=SimpleNamespace(workspace=tmp_path)),
        remote_runtime=RemoteJobsBridge(plane),
    )
    manager.store.insert_job(
        JobRecord(id="desktop", runtime="remote:localhost", status="running")
    )
    run = Run(status="running")
    job = Job(run_id=run.run_id, execution_ref="desktop", status="running")
    manager.run_store.save(run)
    manager.run_store.save(job)
    manager._run_ids["desktop"] = run.run_id
    manager._run_job_ids["desktop"] = job.job_id
    plane.job_store().record(
        RemoteJobHandle(
            kind="nohup",
            host="localhost",
            workdir="/tmp",
            pgid=123,
            local_job_id="desktop",
        )
    )
    asyncio.run(manager.reconcile_remote_jobs())
    assert manager.run_store.load(run.run_id).status == "unknown"
    outcome.state = "pending"
    asyncio.run(manager.reconcile_remote_jobs())
    assert manager.run_store.load(run.run_id).status == "queued"
    outcome.state = "done"
    assert asyncio.run(manager.reconcile_remote_jobs())["checked"] == 1
    assert manager.run_store.load(run.run_id).status == "succeeded"
    assert manager.run_store.load(job.job_id).status == "succeeded"
    before = manager.run_store.events()
    asyncio.run(manager.reconcile_remote_jobs())
    assert manager.run_store.events() == before


def test_run_stream_includes_kernel_and_verification(tmp_path):
    manager, client = jobs_client(tmp_path)
    run = Run(status="running")
    manager.run_store.save(run)
    kernel = Kernel(run_id=run.run_id)
    artifact = Artifact(run_id=run.run_id)
    verification = Verification(artifact_id=artifact.artifact_id)
    for obj in (kernel, artifact, verification):
        manager.run_store.save(obj)
    run.status = "succeeded"
    manager.run_store.save(run)
    with client:
        response = client.get(f"/runs/{run.run_id}/events/stream")
        frames = parse_sse(response.text)
        events = [body["data"] for _, body in frames if body["type"] == "run.event"]
        assert {e["kind"] for e in events} == {
            "run",
            "kernel",
            "artifact",
            "verification",
        }
        assert client.get(f"/runs/{kernel.kernel_id}").status_code == 404
        assert client.get(f"/runs/{run.run_id}/events").json()["events"] == events


def test_artifact_schema_runner_is_wired(tmp_path):
    manager, _client = jobs_client(tmp_path)
    run = Run(status="running")
    manager.run_store.save(run)
    manager._run_ids["desktop"] = run.run_id
    path = tmp_path / "broken.json"
    path.write_text("not json")
    manager.store.insert_job(JobRecord(id="desktop"))
    manager._emit(
        "desktop", "artifact.created", {"path": str(path), "artifact_id": "artifact"}
    )
    verifications = manager.run_store.list("verification")
    assert any(v.check == "json_schema" and v.verdict == "fail" for v in verifications)


def test_stream_cursor_replays_only_later_events_and_closes(tmp_path):
    manager, client = jobs_client(tmp_path)
    run = Run(status="running")
    manager.run_store.save(run)
    cursor = manager.run_store.events()[-1]["seq"]
    run.status = "succeeded"
    manager.run_store.save(run)
    with client:
        response = client.get(
            f"/runs/{run.run_id}/events/stream", headers={"Last-Event-ID": str(cursor)}
        )
        frames = parse_sse(response.text)
        assert [body["type"] for _, body in frames] == ["run.event", "run.done"]
        assert frames[0][0] > cursor
        assert frames[-1][0] is None
        assert (
            client.get(f"/runs/{run.run_id}/events/stream?last_event_id=-1").status_code
            == 422
        )
        assert (
            client.get(f"/runs/{run.run_id}/events?last_event_id=bad").status_code
            == 422
        )


def test_run_queries_trigger_reconcile_after_startup(tmp_path):
    manager, client = jobs_client(tmp_path)
    called = []

    async def reconcile():
        called.append(True)

    manager.reconcile_remote_jobs = reconcile
    with client:
        startup_count = len(called)
        client.get("/runs")
        assert len(called) == startup_count + 1
    assert startup_count == 1


def test_stream_auth_is_checked_before_reconcile(tmp_path):
    from fastapi.testclient import TestClient

    from omicsclaw.entry.desktop import create_desktop_app

    manager, _ = jobs_client(tmp_path)
    called = []

    async def reconcile():
        called.append(True)

    manager.reconcile_remote_jobs = reconcile
    client = TestClient(
        create_desktop_app(manager._app, jobs_manager=manager, bearer_token="secret")
    )
    with client:
        count = len(called)
        assert client.get("/runs/missing/events/stream").status_code == 401
        assert len(called) == count


def test_artifact_provenance_uses_job_snapshot(tmp_path):
    manager, _ = jobs_client(tmp_path)
    run = Run(status="running")
    manager.run_store.save(run)
    kernel = Kernel(run_id=run.run_id)
    manager.run_store.save(kernel)
    manager._run_ids["desktop"] = run.run_id
    manager.store.insert_job(
        JobRecord(
            id="desktop",
            runtime="remote:localhost",
            inputs={
                "code": "print(1)",
                "env": "analysis",
                "input_artifact_ids": ["input"],
            },
        )
    )
    path = tmp_path / "output.json"
    path.write_text("{}")
    manager._emit("desktop", "artifact.created", {"path": str(path)})
    artifact = manager.run_store.list("artifact")[0]
    assert artifact.code_ref.startswith("sha256:")
    assert artifact.host_id == "localhost"
    assert artifact.kernel_id == kernel.kernel_id
    assert artifact.environment_ref == "analysis"
    assert artifact.input_artifact_ids == ["input"]


def test_recovered_remote_cancel_requires_host_confirmation(tmp_path):
    from omicsclaw.entry.desktop.jobs_manager import JobError

    plane = RemotePlaneBinding(Database(tmp_path / "remote.db"), workspace=tmp_path)
    manager = JobsManager(
        SimpleNamespace(config=SimpleNamespace(workspace=tmp_path)),
        remote_runtime=RemoteJobsBridge(plane),
    )
    manager.store.insert_job(
        JobRecord(id="desktop", runtime="remote:localhost", status="running")
    )
    plane.job_store().record(
        RemoteJobHandle(
            kind="nohup",
            host="localhost",
            workdir="/tmp",
            pgid=123,
            local_job_id="desktop",
        )
    )

    async def offline_cancel(ref):
        return SimpleNamespace(state="unknown")

    plane.cancel = offline_cancel
    import pytest

    with pytest.raises(JobError, match="remote_cancel_unconfirmed"):
        asyncio.run(manager.cancel_job("desktop"))
    assert manager.get_job("desktop").status == "running"
