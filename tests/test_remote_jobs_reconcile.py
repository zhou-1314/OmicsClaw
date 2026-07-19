"""Legacy scientific Job startup closure is bounded and never replays JSON."""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.remote.routers import jobs as jobs_module
from omicsclaw.remote.schemas import Job


def _job(workspace: Path, job_id: str, status: str) -> Job:
    return Job(
        job_id=job_id,
        skill="historical-scientific-skill",
        status=status,
        workspace=str(workspace),
        inputs={"dataset": "/must/not/replay"},
        params={"legacy": True},
        created_at="2025-01-01T00:00:00Z",
    )


def test_startup_closes_queued_running_and_cancel_requested_without_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for status in ("queued", "running", "cancel_requested"):
        jobs_module._write_job(workspace, _job(workspace, status, status))

    migrated = jobs_module.terminalize_legacy_active_jobs_at_startup(workspace)

    assert migrated == ("cancel_requested", "queued", "running")
    for job_id in migrated:
        closed = jobs_module._read_job(workspace, job_id)
        assert closed is not None
        assert closed.status == "interrupted"
        assert closed.terminal_code == "legacy_execution_unrecoverable"
        assert closed.inputs == {"dataset": "/must/not/replay"}
        assert not jobs_module._artifact_root(workspace, job_id).exists()
    assert not hasattr(jobs_module, "_ensure_stub_job")


def test_startup_closure_excludes_chat_and_existing_terminal_jobs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chat = Job(
        job_id="chat-display",
        skill="chat",
        status="queued",
        workspace=str(workspace),
        inputs={},
        params={"job_kind": "chat_stream"},
        created_at="2025-01-01T00:00:00Z",
    )
    terminal = _job(workspace, "already-terminal", "failed")
    for job in (chat, terminal):
        jobs_module._write_job(workspace, job)
    before = {
        job.job_id: jobs_module._job_path(workspace, job.job_id).read_bytes()
        for job in (chat, terminal)
    }

    assert jobs_module.terminalize_legacy_active_jobs_at_startup(workspace) == ()
    assert {
        job.job_id: jobs_module._job_path(workspace, job.job_id).read_bytes()
        for job in (chat, terminal)
    } == before


def test_startup_scan_limit_fails_before_partial_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for job_id in ("a", "b"):
        jobs_module._write_job(workspace, _job(workspace, job_id, "running"))
    before = {
        job_id: jobs_module._job_path(workspace, job_id).read_bytes()
        for job_id in ("a", "b")
    }

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
        for job_id in ("a", "b")
    } == before


def test_startup_closure_never_follows_preexisting_temp_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job = _job(workspace, "symlink-temp", "running")
    jobs_module._write_job(workspace, job)
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("must-survive", encoding="utf-8")
    legacy_temp = jobs_module._job_path(workspace, job.job_id).with_suffix(
        ".json.tmp"
    )
    try:
        legacy_temp.symlink_to(victim)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    migrated = jobs_module.terminalize_legacy_active_jobs_at_startup(workspace)

    assert migrated == (job.job_id,)
    assert victim.read_text(encoding="utf-8") == "must-survive"
    assert legacy_temp.is_symlink()
    closed = jobs_module._read_job(workspace, job.job_id)
    assert closed is not None
    assert closed.status == "interrupted"
