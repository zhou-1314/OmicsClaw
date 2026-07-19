"""Atomic ``job.json`` writes.

``_write_job`` used to call ``Path.write_text`` directly, which under the
hood is ``open(O_TRUNC) → write → close``. A crash / OOM-kill between the
truncate and the final write leaves ``job.json`` empty or partially
written; ``_read_job`` then returns ``None`` and the job status is
lost *permanently* — the orphan reconciler can't recover it because it
relies on reading the very file that was corrupted.

Fix: write to a sibling ``.tmp`` file, then ``os.replace``. The rename
is atomic on POSIX, so any concurrent reader always sees either the old
complete content or the new complete content — never a truncated file.

Tests simulate the crash by patching the temp write to raise mid-flight
and assert the target file still deserializes to the pre-write state.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omicsclaw.remote.routers import jobs as jobs_module
from omicsclaw.remote.schemas import Job


def _make_job(*, job_id: str, status: str = "running", error: str | None = None) -> Job:
    return Job(
        job_id=job_id,
        session_id="",
        skill="spatial-preprocess",
        status=status,
        workspace="/tmp/placeholder",
        inputs={},
        params={},
        created_at="2025-01-01T00:00:00+00:00",
        started_at="2025-01-01T00:00:01+00:00",
        error=error,
    )


def test_write_job_crash_between_truncate_and_write_preserves_old_content(
    monkeypatch, tmp_path: Path
) -> None:
    """RED: a naive ``path.write_text`` writer truncates the target
    before writing the new content; a crash in between leaves an empty
    file and ``_read_job`` returns ``None``.

    GREEN: an atomic writer uses a ``.tmp`` sibling + ``os.replace``,
    so sabotaging the final rename still leaves the target with its
    pre-write content."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "atomic-subject"

    jobs_module._write_job(workspace, _make_job(job_id=job_id, status="running"))
    path_before = jobs_module._job_path(workspace, job_id)
    assert path_before.is_file()

    real_replace = os.replace

    def crash_replace_into_target(src, target, *args, **kwargs):
        # The handle-relative temp inode was written, but its final atomic
        # replacement fails. The old target must remain complete.
        if target == "job.json":
            raise OSError("simulated crash mid-rename")
        return real_replace(src, target, *args, **kwargs)

    monkeypatch.setattr(jobs_module.os, "replace", crash_replace_into_target)

    with pytest.raises(ValueError):
        jobs_module._write_job(
            workspace, _make_job(job_id=job_id, status="failed", error="x")
        )

    preserved = jobs_module._read_job(workspace, job_id)
    assert preserved is not None, (
        "target job.json was truncated — non-atomic writer corrupted state"
    )
    assert preserved.status == "running"


def test_write_job_failure_in_temp_leaves_target_untouched(
    monkeypatch, tmp_path: Path
) -> None:
    """If the temp write itself errors, the original target must remain
    the pre-write version, not the partial new content."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "atomic-2"
    jobs_module._write_job(workspace, _make_job(job_id=job_id, status="queued"))

    target = jobs_module._job_path(workspace, job_id)
    pre_content = target.read_text(encoding="utf-8")

    def fail_temp_sync(_fd: int) -> None:
        raise OSError("simulated disk-full")

    monkeypatch.setattr(jobs_module.os, "fsync", fail_temp_sync)

    with pytest.raises(ValueError):
        jobs_module._write_job(
            workspace, _make_job(job_id=job_id, status="running")
        )

    assert target.read_text(encoding="utf-8") == pre_content


def test_write_job_completes_cleans_up_stray_tmp_files(
    tmp_path: Path,
) -> None:
    """After a successful write, no ``.tmp`` sibling should linger next
    to the target (either it renamed into place, or nothing was left)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "atomic-3"
    jobs_module._write_job(
        workspace, _make_job(job_id=job_id, status="queued")
    )

    target = jobs_module._job_path(workspace, job_id)
    tmp_candidates = list(target.parent.glob("*.tmp"))
    assert tmp_candidates == [], f"stray temp files: {tmp_candidates}"


def test_write_job_roundtrips_content_unchanged(tmp_path: Path) -> None:
    """Sanity: the atomic write must produce a file the reader can parse
    — atomic semantics must not break the content format."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job = _make_job(job_id="atomic-rt", status="succeeded")
    jobs_module._write_job(workspace, job)

    roundtripped = jobs_module._read_job(workspace, "atomic-rt")
    assert roundtripped is not None
    assert roundtripped.model_dump() == job.model_dump()


def test_write_job_overwrites_same_id_idempotently(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jid = "atomic-overwrite"
    jobs_module._write_job(workspace, _make_job(job_id=jid, status="queued"))
    jobs_module._write_job(workspace, _make_job(job_id=jid, status="running"))
    jobs_module._write_job(workspace, _make_job(job_id=jid, status="succeeded"))

    final = jobs_module._read_job(workspace, jid)
    assert final is not None
    assert final.status == "succeeded"
