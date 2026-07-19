"""Contract tests for ``omicsclaw/remote/`` routers.

Validates the JSON shape that ``OmicsClaw-App`` (Stage 0/1 already shipped)
relies on. These tests pin the wire format; behavioural changes that affect
the App must update both sides.

Test pattern follows ``tests/test_app_server.py``: build an ad-hoc FastAPI
instance and ``include_router`` directly to skip the heavy lifespan.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from omicsclaw.control import RunObservationPage  # noqa: E402
from omicsclaw.remote.app_integration import register_remote_routers  # noqa: E402
from omicsclaw.remote.auth import capture_remote_bearer_authority  # noqa: E402
from omicsclaw.remote.routers import artifacts as artifacts_module  # noqa: E402
from omicsclaw.remote.routers import datasets as datasets_module  # noqa: E402
from omicsclaw.remote.routers import env as env_module  # noqa: E402
from omicsclaw.remote.routers import jobs as jobs_module  # noqa: E402
from omicsclaw.remote.routers import sessions as sessions_module  # noqa: E402
from omicsclaw.remote.schemas import Job  # noqa: E402


class _EmptyRunRuntime:
    lifecycle_ready = True

    def list_receipts(self, *, status=None, cursor=None, limit=50):
        del status, cursor, limit
        return RunObservationPage((), None)


@pytest.fixture()
def client(monkeypatch, tmp_path: Path) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(workspace))
    for module in (artifacts_module, datasets_module, env_module, jobs_module):
        monkeypatch.setattr(module, "get_remote_workspace", lambda: workspace)
    monkeypatch.setattr(
        jobs_module,
        "require_remote_run_runtime",
        lambda: _EmptyRunRuntime(),
    )
    app = FastAPI()
    capture_remote_bearer_authority(app, {})
    register_remote_routers(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /connections/test  +  /env/doctor
# ---------------------------------------------------------------------------


def test_connections_test_returns_version_and_extras(client: TestClient) -> None:
    response = client.post("/connections/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["version"], str) and body["version"]
    assert "server_time" in body
    extras = body["extras"]
    assert "gpu" in extras and "available" in extras["gpu"]
    assert "disk_free_bytes" in extras


def test_env_doctor_returns_doctor_report_shape(client: TestClient) -> None:
    response = client.get("/env/doctor")
    assert response.status_code == 200
    body = response.json()
    for field in ("generated_at", "workspace_dir", "omicsclaw_dir",
                  "overall_status", "failure_count", "warning_count", "checks"):
        assert field in body, f"missing field: {field}"
    assert body["overall_status"] in {"ok", "warn", "fail"}
    assert isinstance(body["checks"], list)
    if body["checks"]:
        check = body["checks"][0]
        assert {"name", "status", "summary", "details"} <= set(check.keys())


# ---------------------------------------------------------------------------
# /datasets
# ---------------------------------------------------------------------------


def test_dataset_upload_persists_and_lists(client: TestClient) -> None:
    payload = b"H5AD-fake-content" * 64
    response = client.post(
        "/datasets/upload",
        files={"file": ("demo.h5ad", io.BytesIO(payload), "application/octet-stream")},
        data={"display_name": "Demo dataset", "execution_target": "local"},
    )
    assert response.status_code == 200, response.text
    ref = response.json()
    assert ref["display_name"] == "Demo dataset"
    assert ref["execution_target"] == "local"
    assert ref["size_bytes"] == len(payload)
    assert ref["checksum"].startswith("sha256-64k:")
    assert ref["status"] == "synced"
    dataset_id = ref["dataset_id"]

    listed = client.get("/datasets")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert any(d["dataset_id"] == dataset_id for d in body["datasets"])


def test_import_remote_rejects_relative_path(client: TestClient) -> None:
    response = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": "relative/file.h5ad",
            "execution_target": "remote:profile-a",
        },
    )
    assert response.status_code == 400


def test_import_remote_registers_existing_file(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "remote_input.h5ad"
    src.write_bytes(b"x" * 1024)
    response = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(src),
            "display_name": "from-scp",
            "execution_target": "remote:profile-a",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_name"] == "from-scp"
    assert body["size_bytes"] == 1024
    assert body["storage_uri"].startswith("file://")
    assert body["execution_target"] == "remote:profile-a"


def test_dataset_upload_requires_execution_target(client: TestClient) -> None:
    response = client.post(
        "/datasets/upload",
        files={"file": ("demo.h5ad", io.BytesIO(b"x" * 16), "application/octet-stream")},
    )
    assert response.status_code == 422


def test_import_remote_requires_execution_target(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "remote_required.h5ad"
    src.write_bytes(b"x" * 16)
    response = client.post(
        "/datasets/import-remote",
        json={"remote_path": str(src)},
    )
    assert response.status_code == 422


def test_dataset_upload_same_payload_does_not_cross_execution_target_boundaries(
    client: TestClient,
) -> None:
    payload = b"same-content" * 64
    first = client.post(
        "/datasets/upload",
        files={"file": ("same.h5ad", io.BytesIO(payload), "application/octet-stream")},
        data={"execution_target": "remote:profile-a"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/datasets/upload",
        files={"file": ("same.h5ad", io.BytesIO(payload), "application/octet-stream")},
        data={"execution_target": "remote:profile-b"},
    )
    assert second.status_code == 200, second.text

    first_body = first.json()
    second_body = second.json()
    assert first_body["checksum"] == second_body["checksum"]
    assert first_body["dataset_id"] != second_body["dataset_id"]
    assert first_body["execution_target"] == "remote:profile-a"
    assert second_body["execution_target"] == "remote:profile-b"

    listed = client.get("/datasets")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 2
    assert {dataset["execution_target"] for dataset in body["datasets"]} == {
        "remote:profile-a",
        "remote:profile-b",
    }


def test_list_datasets_marks_missing_storage_as_stale(
    client: TestClient,
    tmp_path: Path,
) -> None:
    src = tmp_path / "remote_missing.h5ad"
    src.write_bytes(b"x" * 16)
    imported = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(src),
            "execution_target": "remote:profile-a",
        },
    )
    assert imported.status_code == 200, imported.text
    dataset_id = imported.json()["dataset_id"]

    src.unlink()

    listed = client.get("/datasets")
    assert listed.status_code == 200
    body = listed.json()
    stale = next(dataset for dataset in body["datasets"] if dataset["dataset_id"] == dataset_id)
    assert stale["status"] == "stale"


def test_import_remote_does_not_deduplicate_against_stale_dataset(
    client: TestClient,
    tmp_path: Path,
) -> None:
    original = tmp_path / "remote_original.h5ad"
    original.write_bytes(b"same-content" * 16)
    first = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(original),
            "execution_target": "remote:profile-a",
        },
    )
    assert first.status_code == 200, first.text
    first_body = first.json()

    original.unlink()
    listed = client.get("/datasets")
    assert listed.status_code == 200
    stale = next(
        dataset
        for dataset in listed.json()["datasets"]
        if dataset["dataset_id"] == first_body["dataset_id"]
    )
    assert stale["status"] == "stale"

    replacement = tmp_path / "remote_replacement.h5ad"
    replacement.write_bytes(b"same-content" * 16)
    second = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(replacement),
            "execution_target": "remote:profile-a",
        },
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["dataset_id"] != first_body["dataset_id"]
    assert second_body["status"] == "synced"
    assert second_body["storage_uri"] == replacement.resolve().as_uri()


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------


def _seed_legacy_job(
    *,
    job_id: str,
    status: str = "queued",
    session_id: str = "",
) -> Job:
    import os

    workspace = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job = Job(
        job_id=job_id,
        session_id=session_id,
        skill="historical-skill",
        status=status,
        workspace=str(workspace),
        inputs={"dataset_id": "historical-only"},
        params={},
        created_at="2025-01-01T00:00:00Z",
    )
    jobs_module._write_job(workspace, job)
    return job


def test_legacy_scientific_submit_fails_closed_without_creating_job_json(
    client: TestClient,
) -> None:
    import os

    response = client.post(
        "/jobs",
        json={"skill": "spatial-preprocess", "inputs": {"dataset_id": "abc"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_job_submission"
    assert not (Path(os.environ["OMICSCLAW_WORKSPACE"]) / ".omicsclaw").exists()


def test_client_workspace_claim_is_rejected_before_any_job_write(
    client: TestClient,
) -> None:
    response = client.post(
        "/jobs",
        json={"skill": "spatial-preprocess", "workspace": "/tmp/not-authority"},
    )
    assert response.status_code == 422


def test_chat_display_job_submission_is_retired_without_creating_state(
    client: TestClient,
) -> None:
    import os

    workspace = Path(os.environ["OMICSCLAW_WORKSPACE"])
    response = client.post(
        "/jobs",
        json={
            "skill": "chat",
            "session_id": "sess-chat",
            "params": {"job_kind": "chat_stream", "display_name": "chat turn"},
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "legacy_chat_job_submission_retired"
    assert not (workspace / ".omicsclaw").exists()


def test_legacy_cancel_and_retry_are_read_only_rejections(client: TestClient) -> None:
    original = _seed_legacy_job(job_id="legacy-active")
    assert client.post("/jobs/legacy-active/cancel").status_code == 409
    assert client.post("/jobs/legacy-active/retry").status_code == 409
    import os

    assert jobs_module._read_job(
        Path(os.environ["OMICSCLAW_WORKSPACE"]), original.job_id
    ) == original


def test_legacy_active_sse_is_interrupted_snapshot_without_replay_or_logs(
    client: TestClient,
) -> None:
    original = _seed_legacy_job(job_id="legacy-sse", status="running")
    with client.stream("GET", "/jobs/legacy-sse/events") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: job_interrupted" in body
    assert "event: done" in body
    assert "job_log" not in body
    assert "event: job_started" not in body
    import os

    assert jobs_module._read_job(
        Path(os.environ["OMICSCLAW_WORKSPACE"]), original.job_id
    ) == original


def test_jobs_list_is_bounded_and_projects_legacy_active_as_interrupted(
    client: TestClient,
) -> None:
    for index in range(3):
        _seed_legacy_job(job_id=f"legacy-{index}")
    response = client.get("/jobs", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 1
    assert body["total"] == 1
    assert body["jobs"][0]["status"] == "interrupted"


# ---------------------------------------------------------------------------
# /artifacts
# ---------------------------------------------------------------------------


def _seed_terminal_artifact_job(workspace: Path, job_id: str) -> None:
    jobs_module._write_job(
        workspace,
        Job(
            job_id=job_id,
            skill="historical-skill",
            status="succeeded",
            workspace=str(workspace),
            inputs={},
            params={},
            created_at="2025-01-01T00:00:00Z",
        ),
    )


def test_artifacts_list_empty_when_no_outputs(client: TestClient) -> None:
    response = client.get("/artifacts", params={"job_id": "nonexistent"})
    assert response.status_code == 200
    assert response.json() == {"artifacts": [], "total": 0, "next_cursor": None}


def test_artifacts_list_picks_up_files(client: TestClient) -> None:
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job_id = "job_with_output"
    artifact_dir = ws / ".omicsclaw" / "remote" / "jobs" / job_id / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text("# hi\n")
    _seed_terminal_artifact_job(ws, job_id)

    response = client.get("/artifacts", params={"job_id": job_id})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    artifact = body["artifacts"][0]
    assert artifact["relative_path"] == "report.md"
    assert artifact["mime_type"].startswith("text/")

    download = client.get(f"/artifacts/{artifact['artifact_id']}/download")
    assert download.status_code == 200
    assert download.text == "# hi\n"


def test_artifacts_only_expose_owned_scientific_files(client: TestClient) -> None:
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job_id = "job_with_unowned_aliases"
    artifact_dir = ws / ".omicsclaw" / "remote" / "jobs" / job_id / "artifacts"
    artifact_dir.mkdir(parents=True)

    (artifact_dir / "report.md").write_text("# owned\n")
    (artifact_dir / ".omicsclaw-run-claim.json").write_text("{}\n")

    outside_file = ws / "outside.txt"
    outside_file.write_text("outside\n")
    (artifact_dir / "escape.txt").symlink_to(outside_file)
    (artifact_dir / "report-link.md").symlink_to("report.md")
    os.link(outside_file, artifact_dir / "outside-hardlink.txt")

    real_dir = artifact_dir / "real"
    real_dir.mkdir()
    (real_dir / "inside.txt").write_text("inside\n")
    (artifact_dir / "aliased-dir").symlink_to(real_dir, target_is_directory=True)
    _seed_terminal_artifact_job(ws, job_id)

    response = client.get("/artifacts", params={"job_id": job_id})
    assert response.status_code == 200
    assert {artifact["relative_path"] for artifact in response.json()["artifacts"]} == {
        "report.md",
        "real/inside.txt",
    }

    for relative_path in (
        ".omicsclaw-run-claim.json",
        "escape.txt",
        "report-link.md",
        "outside-hardlink.txt",
        "aliased-dir/inside.txt",
    ):
        download = client.get(f"/artifacts/{job_id}:{relative_path}/download")
        assert download.status_code == 404, relative_path


def test_artifacts_reject_jobs_root_symlink_alias(
    client: TestClient,
    tmp_path: Path,
) -> None:
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job_id = "job_outside_jobs_alias"
    outside_jobs = tmp_path / "outside-jobs"
    artifact_dir = outside_jobs / job_id / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "secret.txt").write_text("outside\n")

    remote_dir = ws / ".omicsclaw" / "remote"
    remote_dir.mkdir(parents=True)
    (remote_dir / "jobs").symlink_to(outside_jobs, target_is_directory=True)

    listed = client.get("/artifacts", params={"job_id": job_id})
    assert listed.status_code == 200
    assert listed.json() == {"artifacts": [], "total": 0, "next_cursor": None}

    downloaded = client.get(f"/artifacts/{job_id}:secret.txt/download")
    assert downloaded.status_code == 404


def test_artifacts_reject_remote_parent_symlink_alias(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """The jobs leaf is not trusted when an ancestor below workspace is aliased."""
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job_id = "job_outside_remote_alias"
    outside_remote = tmp_path / "outside-remote"
    artifact_dir = outside_remote / "jobs" / job_id / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "secret.txt").write_text("outside\n")

    omicsclaw_dir = ws / ".omicsclaw"
    omicsclaw_dir.mkdir()
    (omicsclaw_dir / "remote").symlink_to(outside_remote, target_is_directory=True)

    listed = client.get("/artifacts", params={"job_id": job_id})
    assert listed.status_code == 200
    assert listed.json() == {"artifacts": [], "total": 0, "next_cursor": None}

    downloaded = client.get(f"/artifacts/{job_id}:secret.txt/download")
    assert downloaded.status_code == 404


def test_artifact_reads_do_not_materialize_state_through_remote_parent_alias(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """A read-only artifact request must reject an alias before mkdir side effects."""
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    outside_remote = tmp_path / "empty-outside-remote"
    outside_remote.mkdir()

    omicsclaw_dir = ws / ".omicsclaw"
    omicsclaw_dir.mkdir()
    (omicsclaw_dir / "remote").symlink_to(outside_remote, target_is_directory=True)

    listed = client.get("/artifacts", params={"job_id": "missing-job"})
    assert listed.status_code == 200
    assert listed.json() == {"artifacts": [], "total": 0, "next_cursor": None}
    assert not (outside_remote / "jobs").exists()

    downloaded = client.get("/artifacts/missing-job:result.txt/download")
    assert downloaded.status_code == 404
    assert not (outside_remote / "jobs").exists()


def test_artifacts_created_at_is_stable_across_list_calls(client: TestClient) -> None:
    import os
    import time

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job_id = "job_stable_artifact_time"
    artifact_dir = ws / ".omicsclaw" / "remote" / "jobs" / job_id / "artifacts"
    artifact_dir.mkdir(parents=True)
    report = artifact_dir / "report.md"
    report.write_text("# hi\n")
    _seed_terminal_artifact_job(ws, job_id)

    first = client.get("/artifacts", params={"job_id": job_id})
    assert first.status_code == 200
    created_at_1 = first.json()["artifacts"][0]["created_at"]

    time.sleep(0.01)

    second = client.get("/artifacts", params={"job_id": job_id})
    assert second.status_code == 200
    created_at_2 = second.json()["artifacts"][0]["created_at"]
    assert created_at_1 == created_at_2


def test_artifacts_reject_unsafe_job_id_query(client: TestClient) -> None:
    response = client.get("/artifacts", params={"job_id": "../../../../outside"})
    assert response.status_code == 400


def test_artifacts_reject_symlink_loop_in_job_directory(client: TestClient) -> None:
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    jobs_dir = ws / ".omicsclaw" / "remote" / "jobs"
    jobs_dir.mkdir(parents=True)
    job_id = "job_symlink_loop"
    (jobs_dir / job_id).symlink_to(job_id, target_is_directory=True)

    response = client.get("/artifacts", params={"job_id": job_id})
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_artifact_download_rejects_unsafe_job_id_in_artifact_id(client: TestClient) -> None:
    response = client.get("/artifacts/..%2F..%2F..%2F..%2Foutside:secret.txt/download")
    assert response.status_code == 400


def test_unrecoverable_legacy_active_job_never_exposes_partial_artifacts(
    client: TestClient,
) -> None:
    import os

    ws = Path(os.environ["OMICSCLAW_WORKSPACE"])
    job = _seed_legacy_job(job_id="legacy-partial", status="running")
    partial = jobs_module._artifact_root(ws, job.job_id) / "partial.txt"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("untrusted partial\n", encoding="utf-8")
    jobs_module.terminalize_legacy_active_jobs_at_startup(ws)

    listed = client.get("/artifacts", params={"job_id": job.job_id})
    downloaded = client.get(f"/artifacts/{job.job_id}:partial.txt/download")
    assert listed.status_code == 200
    assert listed.json()["total"] == 0
    assert downloaded.status_code == 404


# ---------------------------------------------------------------------------
# /sessions
# ---------------------------------------------------------------------------


def test_legacy_session_resume_is_a_fixed_retired_response(client: TestClient) -> None:
    response = client.post("/sessions/sess-xyz/resume")
    assert response.status_code == 200
    assert response.json() == {
        "session_id": "sess-xyz",
        "resumed": False,
        "reason": "legacy_session_resume_retired",
        "active_job_ids": [],
    }


def test_legacy_session_resume_never_discovers_active_jobs(
    client: TestClient,
    monkeypatch,
) -> None:
    _seed_legacy_job(job_id="historical-a", session_id="sess-a")
    _seed_legacy_job(job_id="historical-b", session_id="sess-b")

    def forbidden_job_read(*_args, **_kwargs):
        raise AssertionError("retired Session resume read legacy job.json")

    monkeypatch.setattr(
        sessions_module,
        "_read_job",
        forbidden_job_read,
        raising=False,
    )

    resumed = client.post("/sessions/sess-a/resume")
    assert resumed.status_code == 200
    assert resumed.json() == {
        "session_id": "sess-a",
        "resumed": False,
        "reason": "legacy_session_resume_retired",
        "active_job_ids": [],
    }


def test_legacy_session_resume_has_zero_workspace_or_runtime_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(workspace))
    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden_authority")
        raise AssertionError("retired Session resume reached legacy authority")

    for name in (
        "resolve_workspace",
        "jobs_root",
        "_read_job",
        "get_remote_run_runtime",
        "require_remote_run_runtime",
    ):
        monkeypatch.setattr(sessions_module, name, forbidden, raising=False)

    app = FastAPI()
    capture_remote_bearer_authority(app, {})
    app.include_router(sessions_module.router)
    response = TestClient(app).post("/sessions/legacy-session/resume")

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "legacy-session",
        "resumed": False,
        "reason": "legacy_session_resume_retired",
        "active_job_ids": [],
    }
    assert calls == []
    assert not (workspace / ".omicsclaw").exists()


def test_dataset_upload_deduplicates_same_payload(client: TestClient) -> None:
    payload = b"same-content" * 64
    first = client.post(
        "/datasets/upload",
        files={"file": ("same.h5ad", io.BytesIO(payload), "application/octet-stream")},
        data={"execution_target": "local"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/datasets/upload",
        files={"file": ("same.h5ad", io.BytesIO(payload), "application/octet-stream")},
        data={"execution_target": "local"},
    )
    assert second.status_code == 200, second.text

    first_body = first.json()
    second_body = second.json()
    assert first_body["checksum"] == second_body["checksum"]
    assert first_body["dataset_id"] == second_body["dataset_id"]

    listed = client.get("/datasets")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
