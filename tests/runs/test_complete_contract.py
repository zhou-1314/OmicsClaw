import asyncio
from types import SimpleNamespace

from omicsclaw.entry.desktop.jobs_manager import JobRecord
from omicsclaw.planning.book import PlanBook
from omicsclaw.planning.plan import PlanItem, PlanStatus
from omicsclaw.runs.models import Run
from tests.entry.test_desktop_jobs import jobs_client


def test_legacy_session_plan_projection_is_stable_and_unknown(tmp_path):
    manager, _ = jobs_client(tmp_path)

    async def list_sessions(limit=100):
        return [SimpleNamespace(session_id="legacy-chat")]

    manager._app = SimpleNamespace(
        config=manager._app.config,
        sessions=SimpleNamespace(list_sessions=list_sessions),
        plans=PlanBook(),
    )
    manager._app.plans = PlanBook()
    manager._app.plans.for_session("legacy-chat").write(
        [PlanItem("step-a", "inspect", PlanStatus.IN_PROGRESS)]
    )
    asyncio.run(manager.refresh_projections())
    projected = manager.run_store.load("legacy:legacy-chat")
    assert projected.status == "unknown"
    assert projected.plan_id == "session-plan:legacy-chat"
    assert projected.step_id == "step-a"
    before = len(manager.run_store.events())
    asyncio.run(manager.refresh_projections())
    assert len(manager.run_store.events()) == before


def test_approval_resolution_is_part_of_durable_run_timeline(tmp_path):
    manager, _ = jobs_client(tmp_path)
    run = Run(status="running")
    manager.run_store.save(run)
    manager._run_ids["job"] = run.run_id
    manager.store.insert_job(JobRecord(id="job"))
    manager._emit("job", "tool_approval_request", {"call_id": "job.1", "tool": "write"})
    manager._emit(
        "job", "tool_approval_resolved", {"call_id": "job.1", "decision": "approve"}
    )
    events = manager.run_store.events(run_id=run.run_id)
    assert [e["payload"]["event_type"] for e in events if e["kind"] == "activity"] == [
        "tool_approval_request",
        "tool_approval_resolved",
    ]


def test_retry_output_keeps_lineage_with_new_artifact_id(tmp_path):
    manager, _ = jobs_client(tmp_path)
    path = tmp_path / "result.json"
    path.write_text("{}")
    for number in range(2):
        run = Run(status="running", session_id="same-session")
        manager.run_store.save(run)
        job_id = "job" + str(number)
        manager._run_ids[job_id] = run.run_id
        manager.store.insert_job(
            JobRecord(id=job_id, session_id="same-session", inputs={"code": "print(1)"})
        )
        manager._emit(
            job_id,
            "artifact.created",
            {"artifact_id": "fresh-" + str(number), "path": str(path)},
        )
    artifacts = manager.run_store.list("artifact")
    assert artifacts[1].version == 2
    assert artifacts[1].parent_artifact_id == artifacts[0].artifact_id


def test_fast_scanner_retry_claims_rewritten_output(tmp_path):
    from omicsclaw.entry.desktop.artifacts_manager import register_job_artifacts
    from omicsclaw.memory.artifacts import ArtifactStore
    from omicsclaw.memory.database import Database

    store = ArtifactStore(Database(":memory:"))
    output = tmp_path / "tables" / "out.csv"
    output.parent.mkdir()
    output.write_text("x\n1\n")
    first = register_job_artifacts(
        store,
        job_id="one",
        session_id="same",
        skill_directory=tmp_path,
        workspace=tmp_path,
    )
    output.write_text("x\n2\n")
    second = register_job_artifacts(
        store,
        job_id="two",
        session_id="same",
        skill_directory=tmp_path,
        workspace=tmp_path,
    )
    assert len(first) == len(second) == 1


def test_remote_kernel_without_live_transport_has_no_fresh_heartbeat():
    from omicsclaw.runs.projections import observe_kernel

    state = SimpleNamespace(
        kernel=SimpleNamespace(
            is_alive=lambda: True,
            pid=42,
            started_at=1,
            python="python",
            _tunnel=SimpleNamespace(poll=lambda: 255),
        ),
        born_at=1,
        location="remote:hpc",
        session_id="same",
        language="python",
        executing=False,
    )
    observed = observe_kernel(state)
    assert observed.status == "unknown"
    assert observed.last_heartbeat is None


def test_verification_started_is_durable_before_check_and_failure_is_evidence(tmp_path):
    manager, _ = jobs_client(tmp_path)

    def check():
        assert manager.run_store.events()[-1]["event"] == "started"
        raise RuntimeError("test verifier unavailable")

    result = manager._run_verification("fixture", check, run_id="run")
    events = manager.run_store.events(result.verification_id)
    assert [e["event"] for e in events] == ["started", "finished"]
    assert events[0]["payload"]["created_at"] == events[1]["payload"]["created_at"]
    assert result.verdict == "inconclusive"


def test_reviewer_verdict_endpoint_binds_exact_artifact_version(tmp_path):
    from omicsclaw.runs.models import Artifact

    manager, client = jobs_client(tmp_path)
    run = Run(status="succeeded")
    artifact = Artifact(run_id=run.run_id, version=2, job_id="execution")
    manager.run_store.save(run)
    manager.run_store.save(artifact)
    body = {
        "reviewer": "independent-test",
        "verdict": "pass",
        "artifact_id": artifact.artifact_id,
        "artifact_version": 2,
        "evidence_ref": "fixture://review",
    }
    with client:
        response = client.post(f"/runs/{run.run_id}/verifications", json=body)
        assert response.status_code == 201
        body["artifact_version"] = 1
        assert (
            client.post(f"/runs/{run.run_id}/verifications", json=body).status_code
            == 409
        )
    result = manager.run_store.list("verification")[0]
    assert (result.run_id, result.job_id, result.artifact_version, result.check) == (
        run.run_id,
        "execution",
        2,
        "reviewer",
    )


def test_shared_kernel_links_both_runs_and_artifacts(tmp_path):
    from dataclasses import asdict

    from omicsclaw.runs.projections import observe_kernel

    manager, _client = jobs_client(tmp_path)
    state = SimpleNamespace(
        kernel=SimpleNamespace(
            is_alive=lambda: True, pid=42, started_at=1, python="/test/python"
        ),
        born_at=1,
        session_id="shared",
        language="python",
        location="local",
        executing=False,
    )
    kernel = observe_kernel(state)
    path = tmp_path / "output.json"
    path.write_text("{}")
    run_ids = []
    for i in range(2):
        run = Run(status="succeeded")
        run_ids.append(run.run_id)
        manager.run_store.save(run)
        manager._run_ids[str(i)] = run.run_id
        manager.store.insert_job(JobRecord(id=str(i), session_id="shared"))
        manager._emit(str(i), "kernel.observed", asdict(kernel))
        manager._emit(str(i), "artifact.created", {"path": str(path)})
    stored = manager.run_store.load(kernel.kernel_id)
    assert set(stored.run_ids) == set(run_ids)
    assert stored.status == "ready"
    assert all(
        a.kernel_id == kernel.kernel_id for a in manager.run_store.list("artifact")
    )
    assert all(
        any(e["kind"] == "kernel" for e in manager.run_store.events(run_id=r))
        for r in run_ids
    )
    state.kernel.started_at = 2
    assert observe_kernel(state).kernel_id != kernel.kernel_id


def test_new_backend_marks_unowned_kernel_lost_without_claiming_process_death(tmp_path):
    import json
    import subprocess
    import sys

    from omicsclaw.runs.models import Kernel

    manager, _ = jobs_client(tmp_path)
    manager.run_store.save(
        Kernel(
            kernel_id="runtime-before-restart",
            status="ready",
            last_heartbeat="2026-10-01T00:00:00+00:00",
            pid=123,
        )
    )
    script = """
import asyncio,json,sys
from types import SimpleNamespace
from omicsclaw.entry.desktop.jobs_manager import JobsManager
manager=JobsManager(SimpleNamespace(config=SimpleNamespace(workspace=sys.argv[1])))
asyncio.run(manager.refresh_projections())
k=manager.run_store.load('runtime-before-restart')
print(json.dumps({'status':k.status,'pid':k.pid,'heartbeat':k.last_heartbeat}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state == {
        "status": "lost",
        "pid": 123,
        "heartbeat": "2026-10-01T00:00:00+00:00",
    }


def test_restarted_backend_interrupts_local_run_and_job_projection(tmp_path):
    import json
    import subprocess
    import sys

    from omicsclaw.entry.desktop.jobs_manager import JobsManager
    from omicsclaw.runs.models import Job

    manager = JobsManager(SimpleNamespace(config=SimpleNamespace(workspace=tmp_path)))
    manager.store.insert_job(JobRecord(id="local-orphan", status="queued"))
    run = Run(run_id="run-orphan", status="created")
    manager.run_store.save(run)
    manager.run_store.save(
        Job(job_id="domain-orphan", run_id=run.run_id, execution_ref="local-orphan")
    )
    script = """
import json,sys
from types import SimpleNamespace
from omicsclaw.entry.desktop.jobs_manager import JobsManager
manager=JobsManager(SimpleNamespace(config=SimpleNamespace(workspace=sys.argv[1])))
print(json.dumps([manager.get_job('local-orphan').status,manager.run_store.load('run-orphan').status,manager.run_store.load('domain-orphan').status]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["interrupted"] * 3
