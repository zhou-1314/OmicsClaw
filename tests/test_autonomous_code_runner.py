"""Autonomous workspace shape.

ADR 0032 single-engine consolidation (2026-06-22) removed the legacy one-shot
engine (executor / permissions / policy / run_commands / prompt builders); their
tests went with them. The equivalent behaviours (codegen, isolation, repair,
provenance) are covered by ``tests/test_mini_agent_*.py``. What remains here is
the workspace contract shared by the surviving runner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omicsclaw.autonomous import (
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
    create_workspace,
)
from omicsclaw.autonomous.runner import write_run_records
from omicsclaw.autonomous.budget import MiniAgentBudget
from omicsclaw.autonomous.replay import emit_replay_script
from omicsclaw.common import run_paths


def test_create_workspace_uses_autonomous_shape(tmp_path: Path) -> None:
    request = AutonomousRunRequest(
        goal="summarize a dataset",
        output_root=tmp_path,
        input_paths=["/data/input.h5ad"],
        upstream_paths=["/runs/skill-output"],
        run_id="abc123",
    )

    workspace = create_workspace(request)

    assert workspace.root.parent == tmp_path
    assert workspace.root.name.startswith("autonomous-code__")
    assert workspace.root.name.endswith("__abc123")
    # Only the dirs that receive a reference manifest are materialised up front;
    # the rest are created lazily by their writers, so no empty placeholders ship.
    for name in ("inputs", "upstream"):
        assert (workspace.root / name).is_dir()
    for name in ("scripts", "logs", "figures", "tables", "artifacts"):
        assert not (workspace.root / name).exists(), f"{name} should not be pre-created"
    assert json.loads((workspace.paths.inputs / "references.json").read_text()) == {
        "references": ["/data/input.h5ad"]
    }
    assert json.loads((workspace.paths.upstream / "references.json").read_text()) == {
        "references": ["/runs/skill-output"]
    }


def test_project_scoped_autonomous_run_is_indexed(tmp_path: Path) -> None:
    request = AutonomousRunRequest(
        goal="summarize a dataset",
        output_root=tmp_path,
        run_id="abc123",
        project_id="thread-1",
        project_name="Glioma Study",
    )
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
    )

    write_run_records(workspace, request=request, result=result)

    rows = run_paths.read_index(workspace.root.parent)
    assert len(rows) == 1
    assert rows[0]["project_id"] == "thread-1"
    assert rows[0]["run_id"] == workspace.root.name
    assert rows[0]["skill"] == "autonomous-code"
    assert rows[0]["status"] == "completed"


@pytest.mark.parametrize(
    "metadata_kind",
    ["symlink", "hardlink", "missing-project-id"],
)
def test_explicit_autonomous_output_requires_authoritative_project_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_kind: str,
) -> None:
    """An autonomous output root is indexed only under a real Project."""
    explicit_parent = tmp_path / "explicit-parent"
    explicit_parent.mkdir()
    request = AutonomousRunRequest(
        goal="summarize a dataset",
        output_root=explicit_parent,
        run_id=f"metadata-{metadata_kind}",
    )
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
    )
    project_meta = explicit_parent / run_paths.PROJECT_META_FILENAME
    metadata_text = json.dumps(
        {"display_name": "Missing ID"}
        if metadata_kind == "missing-project-id"
        else {"project_id": "spoofed-project", "display_name": "Spoofed"}
    )
    victim = tmp_path / f"project-meta-victim-{metadata_kind}.json"
    if metadata_kind == "missing-project-id":
        project_meta.write_text(metadata_text, encoding="utf-8")
    elif metadata_kind == "symlink":
        victim.write_text(metadata_text, encoding="utf-8")
        project_meta.symlink_to(victim)
    else:
        victim.write_text(metadata_text, encoding="utf-8")
        project_meta.hardlink_to(victim)
    real_finalize_run = run_paths.finalize_run
    finalized: list[Path] = []

    def record_finalize(run_dir, **kwargs):
        finalized.append(Path(run_dir))
        return real_finalize_run(run_dir, **kwargs)

    monkeypatch.setattr(run_paths, "finalize_run", record_finalize)

    write_run_records(workspace, request=request, result=result)

    assert finalized == []
    assert not (explicit_parent / run_paths.RUN_INDEX_FILENAME).exists()
    assert project_meta.read_text(encoding="utf-8") == metadata_text


def test_write_run_records_emits_result_json_contract(tmp_path: Path) -> None:
    """Audit A-2: the desktop ``/outputs`` reader keys on ``result.json``. The
    autonomous runner must emit it (status/summary/output_dir) so finished runs
    are reported completed instead of mis-classified running→failed."""
    import omicsclaw.surfaces.desktop.server as server

    request = AutonomousRunRequest(goal="cluster the cells", output_root=tmp_path, run_id="ok01")
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
        metadata={"answer": "found 7 clusters"},
    )

    write_run_records(workspace, request=request, result=result)

    result_json = workspace.root / "result.json"
    assert result_json.is_file(), "autonomous run must write result.json"
    data = json.loads(result_json.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["output_dir"] == str(workspace.root)
    assert data.get("summary")
    # The desktop reader agrees.
    status, _summary = server._read_result_json(workspace.root)
    assert status == "completed"


def test_write_run_records_result_json_marks_failure(tmp_path: Path) -> None:
    import omicsclaw.surfaces.desktop.server as server

    request = AutonomousRunRequest(goal="cluster the cells", output_root=tmp_path, run_id="bad01")
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.FAILED,
        error="kernel died",
    )

    write_run_records(workspace, request=request, result=result)

    data = json.loads((workspace.root / "result.json").read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["error"] == "kernel died"
    status, _ = server._read_result_json(workspace.root)
    assert status == "failed"


def test_result_marker_failure_cannot_publish_complete_acquisition_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autonomous.runner as runner_module

    request = AutonomousRunRequest(
        goal="cluster the cells",
        output_root=tmp_path,
        run_id="marker-failure",
    )
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
    )

    def fail_result_marker(*args, **kwargs):
        raise OSError("injected result marker failure")

    monkeypatch.setattr(runner_module, "_write_result_json", fail_result_marker)
    with pytest.raises(OSError, match="injected result marker failure"):
        write_run_records(workspace, request=request, result=result)

    assert not (workspace.root / "completion_report.json").exists()
    assert not (workspace.root / "manifest.json").exists()


@pytest.mark.parametrize("alias_kind", ("symlink", "hardlink"))
def test_result_marker_refuses_unowned_destination_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    request = AutonomousRunRequest(
        goal="cluster the cells",
        output_root=tmp_path,
        run_id=f"marker-{alias_kind}",
    )
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
    )
    victim = tmp_path / f"victim-{alias_kind}.json"
    victim.write_text("do not replace\n", encoding="utf-8")
    marker = workspace.root / "result.json"
    if alias_kind == "symlink":
        marker.symlink_to(victim)
    else:
        marker.hardlink_to(victim)

    with pytest.raises(RuntimeError, match="unowned autonomous result marker"):
        write_run_records(workspace, request=request, result=result)

    assert victim.read_text(encoding="utf-8") == "do not replace\n"
    assert not (workspace.root / "completion_report.json").exists()
    assert not (workspace.root / "manifest.json").exists()


@pytest.mark.parametrize("alias_kind", ("symlink", "hardlink"))
def test_result_summary_refuses_unowned_destination_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    request = AutonomousRunRequest(
        goal="cluster the cells",
        output_root=tmp_path,
        run_id=f"summary-{alias_kind}",
    )
    workspace = create_workspace(request)
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=AutonomousRunStatus.SUCCEEDED,
    )
    victim = tmp_path / f"summary-victim-{alias_kind}.md"
    victim.write_text("do not replace\n", encoding="utf-8")
    summary_path = workspace.paths.result_summary
    if alias_kind == "symlink":
        summary_path.symlink_to(victim)
    else:
        summary_path.hardlink_to(victim)

    with pytest.raises(RuntimeError, match="unowned autonomous result summary"):
        write_run_records(workspace, request=request, result=result)

    assert victim.read_text(encoding="utf-8") == "do not replace\n"
    assert not (workspace.root / "completion_report.json").exists()
    assert not (workspace.root / "manifest.json").exists()


@pytest.mark.parametrize("alias_kind", ("symlink", "hardlink"))
def test_replay_script_refuses_unowned_destination_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    victim = tmp_path / f"analysis-victim-{alias_kind}.py"
    victim.write_text("do_not_replace = True\n", encoding="utf-8")
    script_path = workspace / "analysis.py"
    if alias_kind == "symlink":
        script_path.symlink_to(victim)
    else:
        script_path.hardlink_to(victim)

    with pytest.raises(RuntimeError, match="unowned autonomous replay script"):
        emit_replay_script(
            workspace,
            ["value = 42"],
            [],
            MiniAgentBudget(),
            replay_workspace=workspace / "rerun",
        )

    assert victim.read_text(encoding="utf-8") == "do_not_replace = True\n"
