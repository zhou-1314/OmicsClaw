"""User-facing scientific artifact inventory for Autonomous Code runs."""

from __future__ import annotations

from pathlib import Path

from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
)

ARTIFACT_SUFFIXES = frozenset(
    {".png", ".pdf", ".svg", ".csv", ".tsv", ".html", ".h5ad", ".xlsx"}
)
BOOKKEEPING_FILES = frozenset(
    {"completion_report.json", "manifest.json", "analysis.py"}
)
REFERENCE_DIRS = frozenset({"inputs", "upstream", "rerun"})


def list_autonomous_artifacts(
    workspace_root: str | Path,
    *,
    limit: int = 40,
) -> list[str]:
    root = Path(workspace_root)
    try:
        claim_identities = collect_output_claim_identities(root)
        artifacts: list[str] = []
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
            artifacts.append(relative.as_posix())
            if len(artifacts) >= limit:
                break
        return artifacts
    except OSError:
        return []


__all__ = ["list_autonomous_artifacts"]
