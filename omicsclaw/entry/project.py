"""Read historical step-runner results without importing retired skill code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MANIFEST_FILE",
    "MANIFEST_STATUS_KEY",
    "MODULE_DIR_PATTERN",
    "REPORT_FILE",
    "RESULTS_DIR",
    "REVIEW_BRIEF_FILE",
    "ModuleSummary",
    "recent_modules",
]

RESULTS_DIR = "results"
MODULE_DIR_PATTERN = r"^(\d{2})_([a-z0-9][a-z0-9_]*)$"
MANIFEST_FILE = "provenance/manifest.json"
"""A module's manifest, relative to ``results/<NN_slug>/``."""
MANIFEST_STATUS_KEY = "status"
REVIEW_BRIEF_FILE = "provenance/review_brief.md"
"""The brief a successful replay writes for the module reviewer, relative to ``results/<NN_slug>/``."""
REPORT_FILE = "M{nn}_{slug}_REPORT.md"

_MODULE = re.compile(MODULE_DIR_PATTERN)


@dataclass(frozen=True, slots=True)
class ModuleSummary:
    """One module as ``/recent`` shows it."""

    name: str
    modified: float
    """When its manifest (or, without one, its results folder) last changed."""
    status: str | None
    """The manifest's ``status``, or ``None`` without a readable manifest."""
    headline: str | None
    """The first level-one heading of its REPORT, or ``None`` without one."""


def _status(manifest: Path) -> str | None:
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")).get(MANIFEST_STATUS_KEY)
    except (OSError, ValueError, AttributeError):
        return None
    return str(value) if value else None


def _headline(report: Path) -> str | None:
    try:
        lines = report.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def recent_modules(workspace: Path, limit: int = 3) -> tuple[ModuleSummary, ...]:
    """The most recently changed modules under ``<workspace>/results/``, newest first."""
    results = Path(workspace) / RESULTS_DIR
    try:
        candidates = [child for child in results.iterdir() if child.is_dir() and _MODULE.match(child.name)]
    except OSError:
        return ()
    summaries = []
    for folder in candidates:
        match = _MODULE.match(folder.name)
        assert match is not None
        manifest = folder / MANIFEST_FILE
        try:
            modified = (manifest if manifest.is_file() else folder).stat().st_mtime
        except OSError:
            continue
        report = folder / REPORT_FILE.format(nn=match.group(1), slug=match.group(2))
        summaries.append(ModuleSummary(folder.name, modified, _status(manifest), _headline(report)))
    summaries.sort(key=lambda item: item.modified, reverse=True)
    return tuple(summaries[:limit])
