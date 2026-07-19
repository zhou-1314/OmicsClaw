"""Canonical SSE resumes from durable Receipt revisions, not log offsets."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omicsclaw.control import RunObservationSnapshot, RunRecord
from omicsclaw.remote.routers import jobs as jobs_module


RUN_ID = "a" * 32


class _Runtime:
    lifecycle_ready = True

    def __init__(self) -> None:
        self.receipt = RunRecord(
            run_id=RUN_ID,
            scope_kind="unassigned",
            project_id=None,
            run_kind="skill",
            parent_turn_id=None,
            retry_of_run_id=None,
            status="succeeded",
            terminal_code=None,
            manifest_ref="run-store:v1:" + "b" * 32,
            created_at_ms=1,
            started_at_ms=2,
            finished_at_ms=3,
            revision=7,
        )

    def get_receipt(self, run_id: str) -> RunObservationSnapshot:
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return RunObservationSnapshot(self.receipt, None)

    def get_receipt_skill_id(self, run_id: str) -> str:
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return "genomics-vcf-operations"


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _Runtime()
    monkeypatch.setattr(jobs_module, "get_remote_workspace", lambda: workspace)
    monkeypatch.setattr(jobs_module, "require_remote_run_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(jobs_module.router)
    return TestClient(app)


def test_snapshot_first_reconnect_emits_current_revision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    with client.stream(
        "GET",
        f"/jobs/run-{RUN_ID}/events",
        headers={"Last-Event-ID": "7"},
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert "id: 7\n" in body
    assert "event: job_succeeded" in body
    assert "event: done" in body


def test_revision_cursor_ahead_of_receipt_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.get(
        f"/jobs/run-{RUN_ID}/events",
        headers={"Last-Event-ID": "8"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_job_event_cursor"


def test_non_numeric_revision_cursor_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.get(
        f"/jobs/run-{RUN_ID}/events",
        headers={"Last-Event-ID": "stdout-byte-42"},
    )
    assert response.status_code == 400


def test_canonical_sse_never_emits_live_stdout_frames(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    body = client.get(f"/jobs/run-{RUN_ID}/events").text
    assert "job_log" not in body
    assert "stream\"" not in body
