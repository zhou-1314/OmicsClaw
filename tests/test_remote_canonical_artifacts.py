"""HTTP contract for Receipt/Manifest-authoritative Remote artifacts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omicsclaw.control import (
    RunArtifactProjectionIntegrityError,
    RunRecord,
    VerifiedRunArtifact,
)
from omicsclaw.remote.routers import artifacts as artifacts_module
from omicsclaw.remote.routers import jobs as jobs_module
from omicsclaw.remote.schemas import Job


RUN_ID = "a" * 32
JOB_ID = f"run-{RUN_ID}"
PAYLOAD = b"0123456789abcdefghijklmnopqrstuvwxyz"


def _receipt() -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status="succeeded",
        terminal_code=None,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1_700_000_000_000,
        started_at_ms=1_700_000_000_100,
        finished_at_ms=1_700_000_000_200,
        revision=3,
    )


ARTIFACT = VerifiedRunArtifact(
    relative_path="results/data.bin",
    size_bytes=len(PAYLOAD),
    sha256="0" * 64,
    media_type="application/octet-stream",
)


class _Reader:
    def __init__(self) -> None:
        self.receipt = _receipt()
        self.skill_id = "genomics-vcf-operations"
        self.artifact = ARTIFACT
        self.closed = False
        self.reads: list[tuple[int, int]] = []

    async def read_chunk(self, *, offset: int, max_bytes: int) -> bytes:
        self.reads.append((offset, max_bytes))
        return PAYLOAD[offset : offset + max_bytes]

    async def aclose(self) -> None:
        self.closed = True


class _Runtime:
    lifecycle_ready = True

    def __init__(self) -> None:
        self.list_calls: list[tuple[str, str | None, int]] = []
        self.open_calls: list[tuple[str, str]] = []
        self.readers: list[_Reader] = []
        self.failure: Exception | None = None

    async def list_verified_artifacts(self, run_id: str, *, cursor=None, limit=50):
        self.list_calls.append((run_id, cursor, limit))
        if self.failure is not None:
            raise self.failure
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return SimpleNamespace(
            receipt=_receipt(),
            skill_id="genomics-vcf-operations",
            artifacts=(ARTIFACT,),
            total=1,
            next_cursor=None,
        )

    async def open_verified_artifact(self, run_id: str, relative_path: str):
        self.open_calls.append((run_id, relative_path))
        if self.failure is not None:
            raise self.failure
        if run_id != RUN_ID or relative_path != ARTIFACT.relative_path:
            raise KeyError(relative_path)
        reader = _Reader()
        self.readers.append(reader)
        return reader


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _Runtime()
    monkeypatch.setattr(
        artifacts_module,
        "get_remote_run_runtime",
        lambda: runtime,
    )
    monkeypatch.setattr(
        artifacts_module,
        "get_remote_workspace",
        lambda: workspace,
    )
    app = FastAPI()
    app.include_router(artifacts_module.router)
    with TestClient(app) as http:
        yield http, runtime, workspace


def test_canonical_list_is_manifest_inventory_projection(client) -> None:
    http, runtime, workspace = client
    response = http.get(
        "/artifacts",
        params={"job_id": JOB_ID, "cursor": "prior/item", "limit": 7},
    )
    assert response.status_code == 200
    assert runtime.list_calls == [(RUN_ID, "prior/item", 7)]
    assert response.json() == {
        "artifacts": [
            {
                "artifact_id": f"{JOB_ID}:results/data.bin",
                "job_id": JOB_ID,
                "run_id": RUN_ID,
                "relative_path": "results/data.bin",
                "size_bytes": len(PAYLOAD),
                "sha256": "0" * 64,
                "mime_type": "application/octet-stream",
                "created_at": "2023-11-14T22:13:20.200000Z",
            }
        ],
        "total": 1,
        "next_cursor": None,
    }
    assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()


@pytest.mark.parametrize(
    ("range_header", "status", "expected", "content_range"),
    [
        (None, 200, PAYLOAD, None),
        ("bytes=2-7", 206, PAYLOAD[2:8], f"bytes 2-7/{len(PAYLOAD)}"),
        ("bytes=10-", 206, PAYLOAD[10:], f"bytes 10-{len(PAYLOAD)-1}/{len(PAYLOAD)}"),
        ("bytes=-5", 206, PAYLOAD[-5:], f"bytes {len(PAYLOAD)-5}-{len(PAYLOAD)-1}/{len(PAYLOAD)}"),
    ],
)
def test_canonical_download_streams_only_the_verified_descriptor(
    client,
    range_header,
    status,
    expected,
    content_range,
) -> None:
    http, runtime, _workspace = client
    headers = {"Range": range_header} if range_header else {}
    response = http.get(
        f"/artifacts/{JOB_ID}:results/data.bin/download",
        headers=headers,
    )
    assert response.status_code == status
    assert response.content == expected
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers.get("content-range") == content_range
    assert response.headers["content-length"] == str(len(expected))
    assert runtime.open_calls[-1] == (RUN_ID, "results/data.bin")
    assert runtime.readers[-1].closed is True


def test_unsatisfiable_range_closes_reader_before_response(client) -> None:
    http, runtime, _workspace = client
    response = http.get(
        f"/artifacts/{JOB_ID}:results/data.bin/download",
        headers={"Range": "bytes=999-1000"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(PAYLOAD)}"
    assert runtime.readers[-1].closed is True


def test_canonical_integrity_errors_are_content_free(client) -> None:
    http, runtime, _workspace = client
    runtime.failure = RunArtifactProjectionIntegrityError()
    listed = http.get("/artifacts", params={"job_id": JOB_ID})
    downloaded = http.get(f"/artifacts/{JOB_ID}:results/data.bin/download")
    assert listed.status_code == downloaded.status_code == 409
    assert listed.json() == downloaded.json() == {"detail": "artifact_integrity_error"}
    assert "manifest" not in listed.text.lower()
    assert "workspace" not in listed.text.lower()


def test_reserved_canonical_namespace_never_falls_back_to_legacy(client) -> None:
    http, runtime, workspace = client
    unknown_run = "f" * 32
    forged_job_id = f"run-{unknown_run}"
    forged = Job(
        job_id=forged_job_id,
        skill="old",
        status="succeeded",
        workspace=str(workspace),
        inputs={},
        params={},
        created_at="2025-01-01T00:00:00Z",
    )
    jobs_module._write_job(workspace, forged)
    artifact = jobs_module._artifact_root(workspace, forged_job_id) / "secret.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"must-not-leak")

    listed = http.get("/artifacts", params={"job_id": forged_job_id})
    downloaded = http.get(f"/artifacts/{forged_job_id}:secret.txt/download")
    assert listed.status_code == downloaded.status_code == 404
    assert unknown_run in {call[0] for call in runtime.list_calls + runtime.open_calls}
    assert b"must-not-leak" not in downloaded.content


def test_legacy_artifacts_require_a_terminal_job_receipt(client) -> None:
    http, _runtime, workspace = client
    job_id = "legacy-artifacts"
    output = jobs_module._artifact_root(workspace, job_id) / "report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("# report\n", encoding="utf-8")

    no_receipt = http.get("/artifacts", params={"job_id": job_id})
    assert no_receipt.json() == {"artifacts": [], "total": 0, "next_cursor": None}

    active = Job(
        job_id=job_id,
        skill="old",
        status="running",
        workspace=str(workspace),
        inputs={},
        params={},
        created_at="2025-01-01T00:00:00Z",
    )
    jobs_module._write_job(workspace, active)
    assert http.get("/artifacts", params={"job_id": job_id}).json()["total"] == 0

    jobs_module._write_job(
        workspace,
        active.model_copy(update={"status": "succeeded"}),
    )
    terminal = http.get("/artifacts", params={"job_id": job_id})
    assert terminal.status_code == 200
    assert [item["relative_path"] for item in terminal.json()["artifacts"]] == [
        "report.md"
    ]
    downloaded = http.get(f"/artifacts/{job_id}:report.md/download")
    assert downloaded.content == b"# report\n"
