"""Workspace creation for autonomous code runner runs."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from . import run_layout
from .contracts import (
    AUTONOMOUS_RUN_DIR_PREFIX,
    AutonomousRunRequest,
    AutonomousWorkspace,
)


# The subdirs create_workspace actually materialises (the historical meaning of
# this exported name: "the dirs a fresh run dir contains"). The full run-dir
# schema — every name, eager vs lazy, and role — is owned by run_layout, the
# single source of truth that create_workspace AND the artifact contract both
# derive from, so they can never drift apart.
WORKSPACE_SUBDIRS = run_layout.eager_dirs()


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

    # Only the eager dirs (those that receive a references.json) are materialised;
    # every other path is created lazily by its writer, per the run_layout schema.
    for relpath in run_layout.eager_dirs():
        (root / relpath).mkdir(parents=True, exist_ok=True)

    workspace = AutonomousWorkspace(run_id=run_id, root=root)
    _write_reference_manifest(workspace.paths.inputs / "references.json", request.input_paths)
    _write_reference_manifest(workspace.paths.upstream / "references.json", request.upstream_paths)
    return workspace


def _write_reference_manifest(path: Path, references: list[str | Path]) -> None:
    payload = {"references": [str(item) for item in references]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
