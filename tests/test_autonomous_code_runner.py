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

from omicsclaw.autonomous import (
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
    create_workspace,
)
from omicsclaw.autonomous.runner import write_run_records
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
