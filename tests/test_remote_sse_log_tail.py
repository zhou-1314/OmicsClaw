"""Live stdout is deliberately outside canonical Remote Job SSE."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omicsclaw.remote.routers import jobs as jobs_module
from omicsclaw.remote.schemas import Job


def _client(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(jobs_module, "get_remote_workspace", lambda: workspace)
    app = FastAPI()
    app.include_router(jobs_module.router)
    return TestClient(app), workspace


def _seed(workspace: Path, *, job_id: str, status: str) -> Job:
    job = Job(
        job_id=job_id,
        skill="historical",
        status=status,
        workspace=str(workspace),
        inputs={},
        params={},
        created_at="2025-01-01T00:00:00Z",
    )
    jobs_module._write_job(workspace, job)
    return job


def test_terminal_legacy_sse_does_not_tail_persisted_stdout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client, workspace = _client(monkeypatch, tmp_path)
    job = _seed(workspace, job_id="terminal", status="succeeded")
    jobs_module.append_job_stdout_line(workspace, job.job_id, "sensitive-live-line")
    with client.stream("GET", f"/jobs/{job.job_id}/events") as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert "event: job_succeeded" in body
    assert "event: done" in body
    assert "job_log" not in body
    assert "sensitive-live-line" not in body


def test_active_legacy_sse_is_one_interrupted_snapshot_without_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client, workspace = _client(monkeypatch, tmp_path)
    job = _seed(workspace, job_id="active", status="queued")
    before = jobs_module._job_path(workspace, job.job_id).read_bytes()
    with client.stream("GET", f"/jobs/{job.job_id}/events") as response:
        body = "".join(response.iter_text())
    assert "event: job_interrupted" in body
    assert "event: job_started" not in body
    assert "job_log" not in body
    assert jobs_module._job_path(workspace, job.job_id).read_bytes() == before


def test_jobs_module_contains_no_log_tail_execution_driver() -> None:
    assert not hasattr(jobs_module, "_tail_new_lines")
    assert not hasattr(jobs_module, "_run_job")
    assert not hasattr(jobs_module, "_DEFAULT_EXECUTOR")
