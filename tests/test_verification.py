from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omicsclaw.common.manifest import read_manifest
from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME
from omicsclaw.runtime.tools.hooks import EVENT_VERIFICATION_COMPLETED
from omicsclaw.runtime.tools.hooks import LifecycleHookRuntime, LifecycleHookSpec
from omicsclaw.runtime.policy.verification import (
    ArtifactRequirement,
    COMPLETION_STATUS_COMPLETE,
    COMPLETION_STATUS_INCOMPLETE,
    WORKSPACE_KIND_ANALYSIS_RUN,
    build_completion_report,
    format_completion_mapping_summary,
    isolated_workspace,
    update_workspace_manifest,
    write_completion_report,
)


def test_build_completion_report_detects_missing_required_artifacts(tmp_path: Path):
    (tmp_path / "present.txt").write_text("ok", encoding="utf-8")

    report = build_completion_report(
        tmp_path,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="unit_test",
        requirements=[
            ArtifactRequirement(name="present", path="present.txt"),
            ArtifactRequirement(name="missing", path="missing.txt"),
        ],
    )

    assert report.status == COMPLETION_STATUS_INCOMPLETE
    assert report.completed is False
    assert report.missing_required_artifacts() == ["missing.txt"]


def test_update_workspace_manifest_persists_completion_contract(tmp_path: Path):
    (tmp_path / "report.md").write_text("# report\n", encoding="utf-8")
    requirements = [ArtifactRequirement(name="report", path="report.md")]

    manifest_path = update_workspace_manifest(
        tmp_path,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="verification_test",
        requirements=requirements,
        metadata={"source": "pytest"},
    )
    report = build_completion_report(
        tmp_path,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="verification_test",
        requirements=requirements,
        manifest_path=str(manifest_path),
        metadata={"source": "pytest"},
    )
    report_path = write_completion_report(tmp_path, report)
    update_workspace_manifest(
        tmp_path,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="verification_test",
        requirements=requirements,
        completion_report=report,
        metadata={"source": "pytest"},
        append_step=False,
    )

    assert report.status == COMPLETION_STATUS_COMPLETE
    assert Path(report_path).exists()

    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert payload["status"] == COMPLETION_STATUS_COMPLETE
    assert payload["workspace_purpose"] == "verification_test"

    manifest = read_manifest(tmp_path)
    assert manifest is not None
    assert manifest.workspace is not None
    assert manifest.workspace.kind == WORKSPACE_KIND_ANALYSIS_RUN
    assert manifest.verification is not None
    assert manifest.verification.status == COMPLETION_STATUS_COMPLETE
    assert manifest.required_artifacts[0].status == "present"


def test_isolated_workspace_cleans_up(tmp_path: Path):
    staging_root = tmp_path / "staging"
    captured_path = None

    with isolated_workspace(staging_root, prefix="verify") as workspace:
        captured_path = workspace
        assert workspace.exists()
        (workspace / "artifact.txt").write_text("ok", encoding="utf-8")

    assert captured_path is not None
    assert not captured_path.exists()


def test_format_completion_mapping_summary_renders_missing_warnings_and_errors():
    summary = format_completion_mapping_summary(
        {
            "status": "incomplete",
            "completed": False,
            "missing_required_artifacts": ["final_report.md"],
            "warnings": ["review skipped"],
            "errors": ["artifact missing"],
        }
    )

    assert "Status: incomplete" in summary
    assert "Completed: False" in summary
    assert "Missing required artifacts:" in summary
    assert "- final_report.md" in summary
    assert "Warnings:" in summary
    assert "- review skipped" in summary
    assert "Errors:" in summary
    assert "- artifact missing" in summary


def test_write_completion_report_emits_verification_hook(tmp_path: Path):
    (tmp_path / "report.md").write_text("# report\n", encoding="utf-8")
    hook_runtime = LifecycleHookRuntime(
        [
            LifecycleHookSpec(
                name="verification-note",
                event=EVENT_VERIFICATION_COMPLETED,
                message="Verification finished for {workspace_purpose}.",
            )
        ]
    )

    report = build_completion_report(
        tmp_path,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="verification_test",
        requirements=[ArtifactRequirement(name="report", path="report.md")],
    )

    write_completion_report(tmp_path, report, hook_runtime=hook_runtime)

    notices = hook_runtime.consume_pending_messages(
        event_names=(EVENT_VERIFICATION_COMPLETED,),
    )
    assert notices == ["Verification finished for verification_test."]


def test_completion_cannot_be_forced_when_required_artifact_is_missing(tmp_path: Path):
    report = build_completion_report(
        tmp_path,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="forced_completion",
        requirements=[ArtifactRequirement(name="missing", path="missing.txt")],
        status=COMPLETION_STATUS_COMPLETE,
        completed=True,
    )

    assert report.status == COMPLETION_STATUS_INCOMPLETE
    assert report.completed is False
    assert report.missing_required_artifacts() == ["missing.txt"]


def test_completion_rejects_aliases_and_paths_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "run"
    workspace.mkdir()
    external_file = tmp_path / "external.txt"
    external_file.write_text("outside", encoding="utf-8")
    external_dir = tmp_path / "external-dir"
    external_dir.mkdir()
    claim = workspace / OUTPUT_CLAIM_FILENAME
    claim.write_text('{"owner": "runner"}', encoding="utf-8")
    os.link(claim, workspace / "claim-alias.txt")
    (workspace / "escape.txt").symlink_to(external_file)
    (workspace / "escape-dir").symlink_to(external_dir, target_is_directory=True)

    requirements = [
        ArtifactRequirement(name="claim", path="claim-alias.txt"),
        ArtifactRequirement(name="symlink", path="escape.txt"),
        ArtifactRequirement(name="traversal", path="../external.txt"),
        ArtifactRequirement(name="directory", path="escape-dir", kind="dir"),
    ]
    report = build_completion_report(
        workspace,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="alias_rejection",
        requirements=requirements,
        status=COMPLETION_STATUS_COMPLETE,
        completed=True,
    )

    assert report.status == COMPLETION_STATUS_INCOMPLETE
    assert report.completed is False
    assert report.missing_required_artifacts() == [item.path for item in requirements]

    update_workspace_manifest(
        workspace,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="alias_rejection",
        requirements=requirements,
        completion_report=report,
    )
    manifest = read_manifest(workspace)
    assert manifest is not None
    assert [artifact.status for artifact in manifest.required_artifacts] == [
        "missing",
        "missing",
        "missing",
        "missing",
    ]


def test_write_completion_report_refuses_external_alias(tmp_path: Path):
    workspace = tmp_path / "run"
    workspace.mkdir()
    external = tmp_path / "external-report.json"
    original = '{"external": true}'
    external.write_text(original, encoding="utf-8")
    (workspace / "completion_report.json").symlink_to(external)
    report = build_completion_report(
        workspace,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose="safe_write",
        requirements=[],
    )

    with pytest.raises(RuntimeError, match="unowned completion report"):
        write_completion_report(workspace, report)

    assert external.read_text(encoding="utf-8") == original
