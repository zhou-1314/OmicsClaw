"""Cancellation crosses the Remote Adapter only through canonical RunRuntime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omicsclaw.control import ControlIntegrityError, RunObservationSnapshot, RunRecord
from omicsclaw.control.run_runtime import RunCancelResult
from omicsclaw.remote.routers import jobs as jobs_module
from omicsclaw.remote.schemas import Job


RUN_ID = "a" * 32


def _receipt(status: str = "running", revision: int = 2) -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status=status,
        terminal_code=None,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1,
        started_at_ms=2,
        finished_at_ms=None,
        revision=revision,
    )


class _Runtime:
    lifecycle_ready = True

    def __init__(self) -> None:
        self.receipt = _receipt()
        self.cancel_calls: list[str] = []
        self.fail_owner = False

    async def cancel(self, run_id: str) -> RunCancelResult:
        self.cancel_calls.append(run_id)
        if self.fail_owner:
            raise ControlIntegrityError("owner unknown")
        self.receipt = replace(
            self.receipt,
            status="cancel_requested",
            revision=self.receipt.revision + 1,
        )
        return RunCancelResult(True, "cancel_requested", self.receipt)

    def get_receipt(self, run_id: str) -> RunObservationSnapshot:
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return RunObservationSnapshot(self.receipt, None)

    def get_receipt_skill_id(self, run_id: str) -> str:
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return "genomics-vcf-operations"


def _client(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _Runtime()
    monkeypatch.setattr(jobs_module, "get_remote_workspace", lambda: workspace)
    monkeypatch.setattr(jobs_module, "require_remote_run_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(jobs_module.router)
    return TestClient(app), runtime, workspace


def test_canonical_cancel_delegates_once_and_preserves_cancel_requested(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client, runtime, workspace = _client(monkeypatch, tmp_path)
    response = client.post(f"/jobs/run-{RUN_ID}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancel_requested"
    assert runtime.cancel_calls == [RUN_ID]
    assert not (workspace / ".omicsclaw").exists()


def test_unknown_execution_owner_never_manufactures_canceled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client, runtime, workspace = _client(monkeypatch, tmp_path)
    runtime.fail_owner = True
    response = client.post(f"/jobs/run-{RUN_ID}/cancel")
    assert response.status_code == 409
    assert response.json()["detail"] == "run_cancel_owner_unconfirmed"
    assert runtime.receipt.status == "running"
    assert not (workspace / ".omicsclaw").exists()


def test_legacy_cancel_is_read_only_rejection(monkeypatch, tmp_path: Path) -> None:
    client, _runtime, workspace = _client(monkeypatch, tmp_path)
    legacy = Job(
        job_id="legacy-running",
        skill="old",
        status="running",
        workspace=str(workspace),
        inputs={"payload": "never replay"},
        params={},
        created_at="2025-01-01T00:00:00Z",
    )
    jobs_module._write_job(workspace, legacy)
    before = jobs_module._job_path(workspace, legacy.job_id).read_bytes()
    response = client.post(f"/jobs/{legacy.job_id}/cancel")
    assert response.status_code == 409
    assert jobs_module._job_path(workspace, legacy.job_id).read_bytes() == before
    assert not hasattr(jobs_module, "_STUB_JOB_TASKS")
