"""Workspace creation for autonomous code runner runs."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from .contracts import (
    AUTONOMOUS_RUN_DIR_PREFIX,
    AutonomousRunRequest,
    AutonomousWorkspace,
)


WORKSPACE_SUBDIRS = (
    "scripts",
    "logs",
    "figures",
    "tables",
    "artifacts",
    "inputs",
    "upstream",
)


def _timestamp_for_path() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def build_run_dir_name(*, timestamp: str | None = None, run_id: str | None = None) -> str:
    """Return the canonical autonomous run directory name."""
    return f"{AUTONOMOUS_RUN_DIR_PREFIX}__{timestamp or _timestamp_for_path()}__{run_id or _new_run_id()}"


def create_workspace(request: AutonomousRunRequest) -> AutonomousWorkspace:
    """Create an isolated autonomous run workspace below ``output_root``."""
    run_id = request.run_id or _new_run_id()
    output_root = Path(request.output_root)
    # ADR 0035 constraint 9: a top-level autonomous run nests under its Project
    # (the nested skill-facade calls stay inside this workspace, so they never
    # surface as top-level Runs). Empty project_id keeps the legacy root shape.
    base = output_root
    if getattr(request, "project_id", ""):
        from omicsclaw.common.run_paths import resolve_project_dir

        base = resolve_project_dir(
            output_root, request.project_id, request.project_name, create=True
        )
    root = base / build_run_dir_name(run_id=run_id)
    root.mkdir(parents=True, exist_ok=False)

    dirs = {name: root / name for name in WORKSPACE_SUBDIRS}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    workspace = AutonomousWorkspace(
        run_id=run_id,
        root=root,
        scripts_dir=dirs["scripts"],
        logs_dir=dirs["logs"],
        figures_dir=dirs["figures"],
        tables_dir=dirs["tables"],
        artifacts_dir=dirs["artifacts"],
        inputs_dir=dirs["inputs"],
        upstream_dir=dirs["upstream"],
    )
    _write_reference_manifest(workspace.inputs_dir / "references.json", request.input_paths)
    _write_reference_manifest(workspace.upstream_dir / "references.json", request.upstream_paths)
    return workspace


def _write_reference_manifest(path: Path, references: list[str | Path]) -> None:
    payload = {"references": [str(item) for item in references]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
