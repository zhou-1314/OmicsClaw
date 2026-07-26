"""User-facing scientific artifact inventory for Autonomous Code runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
)

ARTIFACT_SUFFIXES = frozenset(
    {
        ".png",
        ".pdf",
        ".svg",
        ".csv",
        ".tsv",
        ".html",
        ".h5ad",
        ".xlsx",
        ".parquet",
        ".npz",
        ".loom",
        ".rds",
    }
)
BOOKKEEPING_FILES = frozenset(
    {"completion_report.json", "manifest.json", "analysis.py"}
)
REFERENCE_DIRS = frozenset({"inputs", "upstream", "rerun"})


@dataclass(frozen=True, slots=True)
class AutonomousArtifactInventory:
    paths: tuple[str, ...]
    total: int
    truncated: bool


def inventory_autonomous_artifacts(
    workspace_root: str | Path,
    *,
    limit: int = 40,
) -> AutonomousArtifactInventory:
    root = Path(workspace_root)
    max_paths = max(0, limit)
    try:
        claim_identities = collect_output_claim_identities(root)
        artifacts: list[str] = []
        total = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] in REFERENCE_DIRS:
                continue
            if (
                path.name in BOOKKEEPING_FILES
                or path.suffix.lower() not in ARTIFACT_SUFFIXES
            ):
                continue
            if not is_scientific_output_file(
                path,
                output_root=root,
                claim_identities=claim_identities,
            ):
                continue
            total += 1
            if len(artifacts) < max_paths:
                artifacts.append(relative.as_posix())
        return AutonomousArtifactInventory(
            paths=tuple(artifacts),
            total=total,
            truncated=total > len(artifacts),
        )
    except OSError:
        return AutonomousArtifactInventory(paths=(), total=0, truncated=False)


__all__ = ["AutonomousArtifactInventory", "inventory_autonomous_artifacts"]
