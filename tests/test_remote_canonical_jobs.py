"""Contract tests for the canonical Remote Job compatibility Adapter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omicsclaw.control import (
    RunAcceptanceStatus,
    RunObservationPage,
    RunObservationSnapshot,
    RunRecord,
)
from omicsclaw.control.run_runtime import RunCancelResult, RunSubmissionResult
from omicsclaw.remote.routers import jobs as jobs_module
from omicsclaw.remote.schemas import Job


RUN_ID = "a" * 32
SUBMISSION_ID = "1" * 32


def _receipt(
    status: str = "queued",
    *,
    revision: int = 1,
    terminal_code: str | None = None,
) -> RunRecord:
    terminal = status in {"succeeded", "failed", "canceled", "interrupted"}
    return RunRecord(
        run_id=RUN_ID,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status=status,
        terminal_code=terminal_code,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1_700_000_000_000,
        started_at_ms=1_700_000_000_100 if status != "queued" else None,
        finished_at_ms=1_700_000_000_200 if terminal else None,
        revision=revision,
    )


def _canonical_body() -> dict:
    return {
        "schema_version": 1,
        "skill": "genomics-vcf-operations",
        "scope": {"kind": "unassigned"},
        "inputs": {"demo": True},
        "params": {},
        "resource_contract": {
            "kind": "simple",
            "request": {
                "cpu_cores": 1,
                "memory_mib": 1024,
                "gpu_devices": 0,
                "threads": 1,
                "temporary_disk_mib": 2048,
            },
        },
    }


class _Runtime:
    lifecycle_ready = True

    def __init__(self) -> None:
        self.receipt = _receipt()
        self.submit_calls = []
        self.cancel_calls = []
        self.wait_calls = []

    async def submit(self, submission):
        self.submit_calls.append(submission)
        status = (
            RunAcceptanceStatus.ACCEPTED
            if len(self.submit_calls) == 1
            else RunAcceptanceStatus.DUPLICATE
        )
        return RunSubmissionResult(status, self.receipt)

    def get_receipt(self, run_id: str) -> RunObservationSnapshot:
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return RunObservationSnapshot(self.receipt, None)

    def list_receipts(self, *, status=None, cursor=None, limit=50):
        del cursor, limit
        observations = ()
        if status is None or self.receipt.status == status:
            observations = (RunObservationSnapshot(self.receipt, None),)
        return RunObservationPage(observations, None)

    def get_receipt_skill_id(self, run_id: str) -> str:
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return "genomics-vcf-operations"

    async def wait_for_receipt_revision(self, run_id: str, *, after_revision: int):
        self.wait_calls.append((run_id, after_revision))
        return self.get_receipt(run_id)

    async def cancel(self, run_id: str) -> RunCancelResult:
        self.cancel_calls.append(run_id)
        self.receipt = replace(self.receipt, status="cancel_requested", revision=2)
        return RunCancelResult(True, "cancel_requested", self.receipt)


@pytest.fixture()
def remote_client(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _Runtime()
    monkeypatch.setattr(jobs_module, "get_remote_workspace", lambda: workspace)
    monkeypatch.setattr(jobs_module, "require_remote_run_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(jobs_module.router)
    with TestClient(app) as client:
        yield client, runtime, workspace


def test_canonical_submit_is_idempotent_runtime_only_and_never_writes_job_json(
    remote_client,
) -> None:
    client, runtime, workspace = remote_client
    headers = {"Idempotency-Key": SUBMISSION_ID}

    accepted = client.post("/jobs", json=_canonical_body(), headers=headers)
    assert accepted.status_code == 202
    assert accepted.headers["location"] == f"/jobs/run-{RUN_ID}"
    assert accepted.json() == {
        "job_id": f"run-{RUN_ID}",
        "status": "queued",
        "run_id": RUN_ID,
        "duplicate": False,
        "receipt_revision": 1,
    }
    duplicate = client.post("/jobs", json=_canonical_body(), headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    assert len(runtime.submit_calls) == 2
    submission = runtime.submit_calls[0]
    assert submission.run_submission_id == SUBMISSION_ID
    assert submission.scope.kind == "unassigned"
    assert submission.inputs["input"] == {"kind": "demo"}
    assert submission.parameters == {}
    assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()
    assert not hasattr(jobs_module, "_DEFAULT_EXECUTOR")
    assert not hasattr(jobs_module, "_STUB_JOB_TASKS")
    assert not hasattr(jobs_module, "_ensure_stub_job")


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (_canonical_body(), {}),
        (
            {**_canonical_body(), "inputs": {"demo": False}},
            {"Idempotency-Key": SUBMISSION_ID},
        ),
        ({**_canonical_body(), "params": {"x": 1}}, {"Idempotency-Key": SUBMISSION_ID}),
        (
            {**_canonical_body(), "scope": {"kind": "project", "project_id": "2" * 32}},
            {"Idempotency-Key": SUBMISSION_ID},
        ),
    ],
)
def test_canonical_submit_fails_closed_before_runtime(
    remote_client, body, headers
) -> None:
    client, runtime, workspace = remote_client
    response = client.post("/jobs", json=body, headers=headers)
    assert response.status_code == 422
    assert runtime.submit_calls == []
    assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()


@pytest.mark.parametrize(
    ("content", "headers", "expected_status", "expected_code"),
    [
        (
            b'{"schema_version":1,"schema_version":1}',
            {"Content-Type": "application/json", "Idempotency-Key": SUBMISSION_ID},
            400,
            "invalid_job_json",
        ),
        (
            b"\xff\xfe",
            {"Content-Type": "application/json", "Idempotency-Key": SUBMISSION_ID},
            400,
            "invalid_job_json_encoding",
        ),
        (
            b"{}",
            {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Idempotency-Key": SUBMISSION_ID,
            },
            415,
            "content_encoding_not_supported",
        ),
        (
            b"{}",
            {"Content-Type": "text/plain", "Idempotency-Key": SUBMISSION_ID},
            415,
            "application_json_required",
        ),
        (
            b"{" + b"x" * (64 * 1024),
            {"Content-Type": "application/json", "Idempotency-Key": SUBMISSION_ID},
            413,
            "job_request_too_large",
        ),
    ],
)
def test_canonical_wire_content_safety_precedes_runtime_and_storage(
    remote_client,
    content,
    headers,
    expected_status,
    expected_code,
) -> None:
    client, runtime, workspace = remote_client
    response = client.post("/jobs", content=content, headers=headers)
    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_code}
    assert runtime.submit_calls == []
    assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()


def test_chat_submission_is_retired_and_historical_active_rows_are_closed_projections(
    remote_client,
) -> None:
    client, _runtime, workspace = remote_client
    created = client.post(
        "/jobs",
        json={
            "session_id": "session-a",
            "skill": "chat",
            "params": {"job_kind": "chat_stream"},
        },
    )
    assert created.status_code == 409
    assert created.json()["detail"] == "legacy_chat_job_submission_retired"
    assert not (workspace / ".omicsclaw").exists()

    historical_chat = Job(
        job_id="historical-chat",
        session_id="session-a",
        skill="chat",
        status="queued",
        workspace=str(workspace),
        inputs={},
        params={"job_kind": "chat_stream"},
        created_at="2025-01-01T00:00:00+00:00",
        compatibility_kind="chat_stream",
    )
    jobs_module._write_job(workspace, historical_chat)

    historical = Job(
        job_id="historical-active",
        skill="old-scientific-skill",
        status="running",
        workspace=str(workspace),
        inputs={"dataset": "/must/not/replay"},
        params={"unsafe": True},
        created_at="2025-01-01T00:00:00+00:00",
    )
    jobs_module._write_job(workspace, historical)

    projected = client.get("/jobs/historical-active")
    assert projected.status_code == 200
    assert projected.json()["status"] == "interrupted"
    assert projected.json()["terminal_code"] == "legacy_execution_unrecoverable"
    assert projected.json()["compatibility_kind"] == "legacy_job"
    assert jobs_module._read_job(workspace, historical.job_id) == historical

    projected_chat = client.get("/jobs/historical-chat")
    assert projected_chat.status_code == 200
    assert projected_chat.json()["status"] == "interrupted"
    assert projected_chat.json()["terminal_code"] == "legacy_chat_job_retired"
    assert jobs_module._read_job(workspace, historical_chat.job_id) == historical_chat

    listed = client.get("/jobs")
    assert listed.status_code == 200
    by_id = {row["job_id"]: row for row in listed.json()["jobs"]}
    assert by_id[historical_chat.job_id]["status"] == "interrupted"
    assert by_id["historical-active"]["status"] == "interrupted"


def test_canonical_detail_cancel_retry_and_sse_are_pure_runtime_projections(
    remote_client,
) -> None:
    client, runtime, workspace = remote_client
    canonical_id = f"run-{RUN_ID}"

    detail = client.get(f"/jobs/{canonical_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["run_id"] == RUN_ID
    assert body["compatibility_kind"] == "canonical_run"
    assert body["artifact_root"] is None
    assert body["inputs"] == {"demo": True}
    assert body["workspace"] == ""
    assert str(workspace) not in detail.text

    listed = client.get("/jobs")
    assert listed.status_code == 200
    listed_row = next(
        row for row in listed.json()["jobs"] if row["job_id"] == canonical_id
    )
    assert listed_row["workspace"] == ""
    assert str(workspace) not in listed.text

    canceled = client.post(f"/jobs/{canonical_id}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "cancel_requested"
    assert runtime.cancel_calls == [RUN_ID]

    retry = client.post(f"/jobs/{canonical_id}/retry")
    assert retry.status_code == 409
    assert retry.json()["detail"] == "canonical_retry_not_supported"
    assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()

    runtime.receipt = _receipt("succeeded", revision=3)
    with client.stream("GET", f"/jobs/{canonical_id}/events") as stream:
        assert stream.status_code == 200
        text = "".join(stream.iter_text())
    assert "id: 3\n" in text
    assert "event: job_succeeded" in text
    assert "event: done" in text
    assert "job_log" not in text
    assert '"workspace": ""' in text
    assert str(workspace) not in text
    assert runtime.wait_calls == []


def test_legacy_cancel_retry_and_sse_never_mutate_or_execute(remote_client) -> None:
    client, _runtime, workspace = remote_client
    historical = Job(
        job_id="legacy-queued",
        skill="old",
        status="queued",
        workspace=str(workspace),
        inputs={"demo": True},
        params={},
        created_at="2025-01-01T00:00:00+00:00",
    )
    jobs_module._write_job(workspace, historical)

    assert client.post("/jobs/legacy-queued/cancel").status_code == 409
    assert client.post("/jobs/legacy-queued/retry").status_code == 409
    with client.stream("GET", "/jobs/legacy-queued/events") as stream:
        text = "".join(stream.iter_text())
    assert "event: job_interrupted" in text
    assert "event: done" in text
    assert "job_log" not in text
    assert jobs_module._read_job(workspace, historical.job_id) == historical


def test_run_prefix_is_reserved_and_never_falls_back_to_legacy_json(
    remote_client,
) -> None:
    client, _runtime, workspace = remote_client
    shadow_id = "run-" + "c" * 32
    shadow = Job(
        job_id=shadow_id,
        skill="legacy-shadow",
        status="succeeded",
        workspace=str(workspace),
        inputs={},
        params={},
        created_at="2025-01-01T00:00:00+00:00",
    )
    jobs_module._write_job(workspace, shadow)

    response = client.get(f"/jobs/{shadow_id}")
    assert response.status_code == 404
    assert jobs_module._read_job(workspace, shadow_id) == shadow


def test_legacy_reads_and_retired_chat_submission_ignore_symlinked_job_authority(
    remote_client,
    tmp_path: Path,
) -> None:
    client, _runtime, workspace = remote_client
    outside = tmp_path / "outside-jobs"
    outside.mkdir()
    jobs_parent = workspace / ".omicsclaw" / "remote"
    jobs_parent.mkdir(parents=True)
    (jobs_parent / "jobs").symlink_to(outside, target_is_directory=True)

    secret_dir = outside / "outside-secret"
    secret_dir.mkdir()
    secret = Job(
        job_id="outside-secret",
        skill="secret",
        status="succeeded",
        workspace=str(workspace),
        inputs={},
        params={},
        created_at="2025-01-01T00:00:00+00:00",
    )
    (secret_dir / "job.json").write_text(secret.model_dump_json(), encoding="utf-8")

    assert client.get("/jobs/outside-secret").status_code == 404
    chat = client.post(
        "/jobs",
        json={"skill": "chat", "params": {"job_kind": "chat_stream"}},
    )
    assert chat.status_code == 409
    assert chat.json()["detail"] == "legacy_chat_job_submission_retired"
    assert {entry.name for entry in outside.iterdir()} == {"outside-secret"}


def test_startup_migration_terminalizes_only_scientific_active_jobs_idempotently(
    remote_client,
) -> None:
    _client, _runtime, workspace = remote_client
    active = Job(
        job_id="active-scientific",
        skill="old-science",
        status="running",
        workspace=str(workspace),
        inputs={"dataset": "/never/replayed"},
        params={"method": "legacy"},
        created_at="2025-01-01T00:00:00+00:00",
    )
    chat = Job(
        job_id="active-chat",
        skill="chat",
        status="queued",
        workspace=str(workspace),
        inputs={},
        params={"job_kind": "chat_stream"},
        created_at="2025-01-01T00:00:01+00:00",
        compatibility_kind="chat_stream",
    )
    terminal = Job(
        job_id="terminal-scientific",
        skill="old-science",
        status="succeeded",
        workspace=str(workspace),
        inputs={"demo": True},
        params={},
        created_at="2025-01-01T00:00:02+00:00",
    )
    for job in (active, chat, terminal):
        jobs_module._write_job(workspace, job)
    chat_before = jobs_module._job_path(workspace, chat.job_id).read_bytes()
    terminal_before = jobs_module._job_path(workspace, terminal.job_id).read_bytes()

    migrated = jobs_module.terminalize_legacy_active_jobs_at_startup(workspace)
    assert migrated == (active.job_id,)
    closed = jobs_module._read_job(workspace, active.job_id)
    assert closed is not None
    assert closed.status == "interrupted"
    assert closed.terminal_code == "legacy_execution_unrecoverable"
    assert closed.error == "legacy_execution_unrecoverable"
    assert closed.exit_code == 1
    assert closed.finished_at is not None
    assert closed.inputs == active.inputs
    assert closed.params == active.params
    assert closed.artifact_root is None
    assert not jobs_module._artifact_root(workspace, active.job_id).exists()
    assert jobs_module._job_path(workspace, chat.job_id).read_bytes() == chat_before
    assert (
        jobs_module._job_path(workspace, terminal.job_id).read_bytes()
        == terminal_before
    )

    active_after_first = jobs_module._job_path(workspace, active.job_id).read_bytes()
    assert jobs_module.terminalize_legacy_active_jobs_at_startup(workspace) == ()
    assert (
        jobs_module._job_path(workspace, active.job_id).read_bytes()
        == active_after_first
    )


def test_startup_migration_scan_limit_fails_before_any_write(remote_client) -> None:
    _client, _runtime, workspace = remote_client
    originals: dict[str, bytes] = {}
    for job_id in ("active-a", "active-b"):
        job = Job(
            job_id=job_id,
            skill="old-science",
            status="queued",
            workspace=str(workspace),
            inputs={"demo": True},
            params={},
            created_at="2025-01-01T00:00:00+00:00",
        )
        jobs_module._write_job(workspace, job)
        originals[job_id] = jobs_module._job_path(workspace, job_id).read_bytes()

    with pytest.raises(
        jobs_module.LegacyJobMigrationError,
        match="legacy_job_migration_scan_limit_exceeded",
    ):
        jobs_module.terminalize_legacy_active_jobs_at_startup(
            workspace,
            max_entries=1,
        )
    assert {
        job_id: jobs_module._job_path(workspace, job_id).read_bytes()
        for job_id in originals
    } == originals


@pytest.mark.asyncio
async def test_remote_http_real_demo_uses_one_run_assignment_and_verified_artifacts(
    tmp_path: Path,
) -> None:
    """Tracer proof from Remote wire through the real shared Skill runner."""

    import asyncio

    import httpx

    from omicsclaw.control import ControlStateRepository
    from omicsclaw.control.run_runtime import RunRuntime
    from omicsclaw.remote.routers import artifacts as artifacts_module
    from omicsclaw.remote.runtime_binding import (
        bind_remote_run_runtime,
        unbind_remote_run_runtime,
    )
    from omicsclaw.skill.resource_scheduler import ExecutionResourceBudget

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = ControlStateRepository(tmp_path / "state")
    runtime = RunRuntime.for_local_surface(
        repository=repository,
        output_root=tmp_path / "output",
        resource_budget=ExecutionResourceBudget(
            cpu_cores=2,
            memory_mib=4096,
            gpu_device_ids=(),
            threads=2,
            temporary_disk_mib=8192,
            max_processes=2,
        ),
        max_buffered_runs=2,
        max_active_runs=1,
    )
    await runtime.start()
    bind_remote_run_runtime(runtime, workspace=workspace)
    app = FastAPI()
    app.include_router(jobs_module.router)
    app.include_router(artifacts_module.router)
    transport = httpx.ASGITransport(app=app)
    headers = {"Idempotency-Key": "e" * 32}
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            accepted = await client.post(
                "/jobs",
                json=_canonical_body(),
                headers=headers,
            )
            assert accepted.status_code == 202
            body = accepted.json()
            run_id = body["run_id"]
            assert body["job_id"] == f"run-{run_id}"
            assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()

            detail = None
            for _ in range(300):
                detail = await client.get(f"/jobs/run-{run_id}")
                if detail.json()["status"] in {
                    "succeeded",
                    "failed",
                    "canceled",
                    "interrupted",
                }:
                    break
                await asyncio.sleep(0.02)
            assert detail is not None
            assert detail.json()["status"] == "succeeded"

            duplicate = await client.post(
                "/jobs",
                json=_canonical_body(),
                headers=headers,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["run_id"] == run_id
            assert duplicate.json()["duplicate"] is True

            artifacts = await client.get(
                "/artifacts",
                params={"job_id": f"run-{run_id}"},
            )
            assert artifacts.status_code == 200, artifacts.text
            by_path = {
                item["relative_path"]: item for item in artifacts.json()["artifacts"]
            }
            assert "filtered.vcf" in by_path
            assert "result.json" in by_path
            downloaded = await client.get(
                f"/artifacts/{by_path['filtered.vcf']['artifact_id']}/download"
            )
            assert downloaded.status_code == 200
            assert downloaded.content.startswith(b"##fileformat=VCF")

            before_tamper = repository.get_run(run_id)
            artifact_path = (
                runtime.run_store.artifacts_dir(before_tamper.manifest_ref)
                / "filtered.vcf"
            )
            artifact_path.write_text("tampered after completion\n", encoding="utf-8")
            rejected = await client.get(
                "/artifacts",
                params={"job_id": f"run-{run_id}"},
            )
            assert rejected.status_code == 409
            assert rejected.json() == {"detail": "artifact_integrity_error"}
            assert repository.get_run(run_id).revision == before_tamper.revision

        observation = repository.get_run_observation(run_id)
        assert observation.assignment is not None
        assert observation.receipt.revision == 3
        assert (
            len(tuple((tmp_path / "output" / ".run-store" / "refs").glob("*.json")))
            == 1
        )
    finally:
        unbind_remote_run_runtime(runtime)
        await runtime.close()
        repository.close()
