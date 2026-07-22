"""Integration tests: projection driver over a real ControlStateRepository."""

import hashlib

from omicsclaw.control import (
    ControlStateRepository,
    ProjectionIntentInput,
    RunAcceptanceIntent,
    RunReport,
)
from omicsclaw.memory.projection_driver import drive_pending_projections


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _record_run_projection(
    repo,
    *,
    content: bytes,
    source_ref: str = "run-store://completion/1",
    submission: str = "sub-1",
    fp: str = "e",
    projection_kind: str = "analysis_lineage",
):
    """Freeze one real pending Intent via a terminalizing project-scoped Run."""
    project = repo.create_project(f"Drive study {submission}")
    accepted = repo.accept_run(
        RunAcceptanceIntent(
            run_submission_id=submission,
            fingerprint_version=1,
            fingerprint_sha256=fp * 64,
            run_kind="skill",
            scope_kind="project",
            project_id=project.project_id,
            # ADR 0064: a run projection's source_ref must equal the Run's manifest_ref.
            manifest_ref=source_ref,
        )
    )
    assignment = repo.assign_run(accepted.run_id, executor_kind="local")
    repo.apply_run_report(
        RunReport(
            run_id=accepted.run_id,
            assignment_id=assignment.assignment_id,
            terminal_status="succeeded",
            projections=(
                ProjectionIntentInput(
                    projection_kind=projection_kind,
                    source_store="run",
                    source_ref=source_ref,
                    content_sha256=_digest(content),
                ),
            ),
        )
    )
    return project


class RecordingWriter:
    def __init__(self):
        self.writes: dict[str, bytes] = {}
        self.call_count = 0

    def __call__(self, *, intent, content):
        self.call_count += 1
        self.writes[intent.projection_intent_id] = content


def _reader_for(content):
    def _read(*, source_store, source_ref):
        return content

    return _read


def _dict_reader(mapping):
    def _read(*, source_store, source_ref):
        return mapping.get(source_ref)

    return _read


def _states(repo, project_id):
    return {i.state for i in repo.list_projection_intents(project_id)}


# --------------------------------------------------------------------------- #


def test_drives_pending_intent_to_applied(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        content = b"lineage-payload"
        project = _record_run_projection(repo, content=content)
        writer = RecordingWriter()

        summary = drive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=writer
        )

        assert (summary.processed, summary.applied, summary.failed) == (1, 1, 0)
        assert summary.deferred == ()
        assert list(writer.writes.values()) == [content]
        assert _states(repo, project.project_id) == {"applied"}


def test_drive_applies_after_archive(tmp_path):
    # The keystone ADR-0064 property, proven against the real DB: an Intent
    # frozen while active is still completed after the Project is archived.
    with ControlStateRepository(tmp_path) as repo:
        content = b"accepted-before-archive"
        project = _record_run_projection(repo, content=content)
        repo.archive_project(project.project_id)
        assert repo.get_project(project.project_id).lifecycle == "archived"

        summary = drive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=RecordingWriter()
        )

        assert summary.applied == 1
        assert _states(repo, project.project_id) == {"applied"}


def test_drive_marks_digest_mismatch_failed(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project = _record_run_projection(repo, content=b"original")
        writer = RecordingWriter()

        summary = drive_pending_projections(
            repo, read_source=_reader_for(b"tampered"), write_projection=writer
        )

        assert (summary.applied, summary.failed) == (0, 1)
        assert writer.call_count == 0
        intents = repo.list_projection_intents(project.project_id)
        assert intents[0].state == "failed"
        assert intents[0].last_error_code == "digest_mismatch"


def test_drive_marks_source_loss_failed(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project = _record_run_projection(repo, content=b"gone")

        summary = drive_pending_projections(
            repo, read_source=_reader_for(None), write_projection=RecordingWriter()
        )

        assert summary.failed == 1
        intents = repo.list_projection_intents(project.project_id)
        assert intents[0].state == "failed"
        assert intents[0].last_error_code == "source_missing"


def test_drive_is_idempotent_across_sweeps(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        content = b"once"
        _record_run_projection(repo, content=content)
        writer = RecordingWriter()

        first = drive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=writer
        )
        second = drive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=writer
        )

        assert first.applied == 1
        assert second.processed == 0  # nothing pending remains
        assert writer.call_count == 1  # no re-write


def test_drive_defers_transient_write_fault_then_succeeds(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        content = b"retry-me"
        project = _record_run_projection(repo, content=content)

        def _boom(*, intent, content):
            raise OSError("memory store unavailable")

        deferred_summary = drive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=_boom
        )
        assert deferred_summary.applied == 0
        assert len(deferred_summary.deferred) == 1
        # Left pending — not failed — so a healthy sweep can still complete it.
        assert _states(repo, project.project_id) == {"pending"}

        good = RecordingWriter()
        retry_summary = drive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=good
        )
        assert retry_summary.applied == 1
        assert _states(repo, project.project_id) == {"applied"}


def test_drive_processes_multiple_and_reports_counts(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        p_good = _record_run_projection(
            repo, content=b"good", source_ref="run://a", submission="s1", fp="a"
        )
        p_bad = _record_run_projection(
            repo, content=b"orig", source_ref="run://b", submission="s2", fp="b"
        )
        writer = RecordingWriter()

        summary = drive_pending_projections(
            repo,
            read_source=_dict_reader({"run://a": b"good", "run://b": b"tampered"}),
            write_projection=writer,
        )

        assert (summary.processed, summary.applied, summary.failed) == (2, 1, 1)
        assert _states(repo, p_good.project_id) == {"applied"}
        assert _states(repo, p_bad.project_id) == {"failed"}
