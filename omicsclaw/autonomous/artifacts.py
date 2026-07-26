"""User-facing scientific artifact inventory for Autonomous Code runs."""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass
import os
from pathlib import Path

from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
    stat_is_filesystem_alias,
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
    complete: bool
    scan_error: str | None


def inventory_autonomous_artifacts(
    workspace_root: str | Path,
    *,
    limit: int = 40,
) -> AutonomousArtifactInventory:
    root = Path(workspace_root)
    max_paths = max(0, limit)
    try:
        if not root.exists():
            return AutonomousArtifactInventory(
                paths=(),
                total=0,
                truncated=False,
                complete=False,
                scan_error="workspace_not_found",
            )
        if not root.is_dir():
            return AutonomousArtifactInventory(
                paths=(),
                total=0,
                truncated=False,
                complete=False,
                scan_error="workspace_not_directory",
            )
    except OSError:
        return AutonomousArtifactInventory(
            paths=(),
            total=0,
            truncated=False,
            complete=False,
            scan_error="filesystem_scan_failed",
        )
    artifacts: list[str] = []
    total = 0
    scan_errors: list[OSError] = []
    try:
        claim_identities = collect_output_claim_identities(
            root,
            on_error=scan_errors.append,
        )

        def record_walk_error(exc: OSError) -> None:
            scan_errors.append(exc)

        for directory_name, dirnames, filenames in os.walk(
            root,
            topdown=True,
            onerror=record_walk_error,
            followlinks=False,
        ):
            directory = Path(directory_name)
            relative_directory = directory.relative_to(root)
            safe_dirnames: list[str] = []
            for dirname in sorted(dirnames):
                if relative_directory == Path() and dirname in REFERENCE_DIRS:
                    continue
                candidate = directory / dirname
                try:
                    if stat_is_filesystem_alias(os.lstat(candidate)):
                        continue
                except OSError as exc:
                    scan_errors.append(exc)
                    continue
                safe_dirnames.append(dirname)
            dirnames[:] = safe_dirnames

            for filename in sorted(filenames):
                path = directory / filename
                relative = path.relative_to(root)
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
                if max_paths > 0:
                    insort(artifacts, relative.as_posix())
                    if len(artifacts) > max_paths:
                        artifacts.pop()
        return AutonomousArtifactInventory(
            paths=tuple(artifacts),
            total=total,
            truncated=total > len(artifacts),
            complete=not scan_errors,
            scan_error="filesystem_scan_failed" if scan_errors else None,
        )
    except OSError as exc:
        scan_errors.append(exc)
        return AutonomousArtifactInventory(
            paths=tuple(artifacts),
            total=total,
            truncated=total > len(artifacts),
            complete=False,
            scan_error="filesystem_scan_failed",
        )


__all__ = ["AutonomousArtifactInventory", "inventory_autonomous_artifacts"]
