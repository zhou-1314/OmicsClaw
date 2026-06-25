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
    WORKSPACE_SUBDIRS,
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
    for name in WORKSPACE_SUBDIRS:
        assert (workspace.root / name).is_dir()
    assert json.loads((workspace.inputs_dir / "references.json").read_text()) == {
        "references": ["/data/input.h5ad"]
    }
    assert json.loads((workspace.upstream_dir / "references.json").read_text()) == {
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
