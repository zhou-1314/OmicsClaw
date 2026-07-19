"""Authentication is evaluated before every canonical Job/artifact side effect."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omicsclaw.remote.app_integration import register_remote_routers
from omicsclaw.remote.auth import capture_remote_bearer_authority
from omicsclaw.remote.routers import artifacts as artifacts_module
from omicsclaw.remote.routers import jobs as jobs_module


RUN_ID = "a" * 32
JOB_ID = f"run-{RUN_ID}"


@pytest.fixture()
def guarded_client(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(workspace))
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    calls: list[str] = []

    def forbidden(label: str):
        def invoke(*_args, **_kwargs):
            calls.append(label)
            raise AssertionError(f"unauthorized request reached {label}")

        return invoke

    monkeypatch.setattr(
        jobs_module,
        "_workspace_or_503",
        forbidden("jobs.workspace"),
    )
    monkeypatch.setattr(
        jobs_module,
        "require_remote_run_runtime",
        forbidden("jobs.runtime"),
    )
    monkeypatch.setattr(
        artifacts_module,
        "_workspace_or_503",
        forbidden("artifacts.workspace"),
    )
    monkeypatch.setattr(
        artifacts_module,
        "get_remote_run_runtime",
        forbidden("artifacts.runtime"),
    )
    app = FastAPI()
    capture_remote_bearer_authority(
        app,
        {"OMICSCLAW_REMOTE_AUTH_TOKEN": "correct-token"},
    )
    register_remote_routers(app)
    with TestClient(app) as client:
        yield client, workspace, calls


REQUESTS = (
    ("POST", "/jobs", {"content": b"{malformed", "headers": {"Content-Type": "application/json"}}),
    ("GET", "/jobs", {}),
    ("GET", f"/jobs/{JOB_ID}", {}),
    ("GET", f"/jobs/{JOB_ID}/events", {}),
    ("POST", f"/jobs/{JOB_ID}/cancel", {}),
    ("POST", f"/jobs/{JOB_ID}/retry", {}),
    ("GET", f"/artifacts?job_id={JOB_ID}", {}),
    ("GET", f"/artifacts/{JOB_ID}:results/data.bin/download", {}),
)


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token", "Basic correct-token"],
)
@pytest.mark.parametrize(("method", "path", "kwargs"), REQUESTS)
def test_auth_rejection_has_zero_runtime_or_filesystem_side_effects(
    guarded_client,
    authorization,
    method,
    path,
    kwargs,
) -> None:
    client, workspace, calls = guarded_client
    headers = dict(kwargs.get("headers", {}))
    if authorization is not None:
        headers["Authorization"] = authorization
    request_kwargs = {key: value for key, value in kwargs.items() if key != "headers"}
    request_kwargs["headers"] = headers

    response = client.request(method, path, **request_kwargs)

    assert response.status_code == 401
    assert calls == []
    assert not (workspace / ".omicsclaw").exists()
