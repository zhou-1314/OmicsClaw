"""Executor abstraction scaffolding.

``omicsclaw/execution/executors/`` provides the Executor Protocol that
real executors (``SkillRunnerExecutor`` for the in-process runner,
``SubprocessExecutor`` for the SSH/Slurm path) implement. The legacy
``LocalExecutor`` ``executor_not_implemented`` stub was removed during
OMI-12 P1.5 — tests that need an instant-return executor define one
inline.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest


def test_base_module_exports_protocol_and_dataclasses() -> None:
    from omicsclaw.execution.executors import (
        Executor,
        JobContext,
        JobOutcome,
    )

    assert inspect.isclass(JobContext)
    assert inspect.isclass(JobOutcome)
    assert hasattr(Executor, "run")


def test_executor_protocol_accepts_duck_typed_implementations() -> None:
    """Any callable that exposes ``async def run(ctx) -> JobOutcome`` is a
    valid ``Executor`` — no subclassing required. This is the contract that
    SSH / Slurm / mock executors all rely on."""
    from omicsclaw.execution.executors import Executor, JobContext, JobOutcome

    class FakeExecutor:
        async def run(self, ctx: JobContext) -> JobOutcome:
            return JobOutcome(
                exit_code=0,
                error=None,
                stdout_text="custom-run",
            )

    # Structural — no runtime isinstance needed; this asserts the shape.
    fake: Executor = FakeExecutor()  # type: ignore[assignment]
    outcome = asyncio.run(fake.run(  # type: ignore[arg-type]
        JobContext(
            job_id="x",
            workspace=Path("/tmp"),
            skill="noop",
            inputs={},
            params={},
            artifact_root=Path("/tmp/a"),
            stdout_log=Path("/tmp/s.log"),
        )
    ))
    assert outcome.exit_code == 0
    assert outcome.stdout_text == "custom-run"


def test_jobs_router_has_no_legacy_executor_dispatch_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Remote scientific Jobs delegate only to the canonical RunRuntime.

    The old generic Executor Protocol may remain available to explicit legacy
    consumers, but `/jobs` must not import it, persist its payload, or accept
    the former arbitrary Job wire shape.
    """
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from omicsclaw.remote.app_integration import register_remote_routers
    from omicsclaw.remote.auth import capture_remote_bearer_authority
    from omicsclaw.remote.routers import jobs as jobs_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(workspace))
    monkeypatch.delenv("OMICSCLAW_REMOTE_AUTH_TOKEN", raising=False)

    assert not hasattr(jobs_module, "_DEFAULT_EXECUTOR")
    assert not hasattr(jobs_module, "_run_job")
    assert not hasattr(jobs_module, "_ensure_stub_job")

    app = FastAPI()
    capture_remote_bearer_authority(app, {})
    register_remote_routers(app)
    client = TestClient(app)

    response = client.post("/jobs", json={"skill": "noop", "inputs": {}})
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_job_submission"
    assert not (workspace / ".omicsclaw" / "remote" / "jobs").exists()
