"""ADR 0064: a Run-sourced Projection Intent must cite that Run's own Manifest."""

import pytest

from omicsclaw.control import (
    ControlStateRepository,
    ProjectionIntentInput,
    RunAcceptanceIntent,
    RunReport,
)


def _accept_project_run(repo, *, manifest_ref="run-store://manifest/1"):
    project = repo.create_project("Binding study")
    accepted = repo.accept_run(
        RunAcceptanceIntent(
            run_submission_id="s" * 32,
            fingerprint_version=1,
            fingerprint_sha256="e" * 64,
            run_kind="skill",
            scope_kind="project",
            project_id=project.project_id,
            manifest_ref=manifest_ref,
        )
    )
    assignment = repo.assign_run(accepted.run_id, executor_kind="local")
    return project, accepted, assignment


def _report(run_id, assignment_id, *, source_ref):
    return RunReport(
        run_id=run_id,
        assignment_id=assignment_id,
        terminal_status="succeeded",
        projections=(
            ProjectionIntentInput(
                projection_kind="analysis_lineage",
                source_store="run",
                source_ref=source_ref,
                content_sha256="f" * 64,
            ),
        ),
    )


def test_wrong_source_ref_is_rejected(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project, accepted, assignment = _accept_project_run(repo)
        with pytest.raises(ValueError, match="manifest_ref"):
            repo.apply_run_report(
                _report(
                    accepted.run_id,
                    assignment.assignment_id,
                    source_ref="run-store://WRONG",
                )
            )
        # The rejected transaction rolled back: the Run is still terminalizable
        # and no Intent was recorded.
        assert repo.get_run(accepted.run_id).status == "running"
        assert repo.list_projection_intents(project.project_id) == ()


def test_matching_source_ref_is_accepted(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project, accepted, assignment = _accept_project_run(
            repo, manifest_ref="run-store://manifest/1"
        )
        applied = repo.apply_run_report(
            _report(
                accepted.run_id,
                assignment.assignment_id,
                source_ref="run-store://manifest/1",
            )
        )
        assert applied.changed is True
        intents = repo.list_projection_intents(project.project_id)
        assert len(intents) == 1
        assert intents[0].source_ref == "run-store://manifest/1"
