"""Single source of truth for the autonomous run-dir layout (ADR 0032).

Every name, lifecycle (eager vs lazy), and role (deliverable / provenance /
sentinel / rerun) of a path inside an **Autonomous run workspace** is declared
once here. ``create_workspace``'s eager set, the completion-report artifact
contract, and the typed path accessors all derive from this one declaration — so
the workspace layer and the requirements contract can never drift apart again
(the clutter bug, where eager dirs and required artifacts disagreed and the
engine shipped empty placeholder dirs).

This is the *schema* ("what any run looks like"); :class:`AutonomousWorkspace`
is the *instance* ("one allocated run"). Pure data + stdlib only — no kernel,
LLM, or verification dependency — so it stays trivially testable and importable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Kind(StrEnum):
    DIR = "dir"
    FILE = "file"


class Lifecycle(StrEnum):
    EAGER = "eager"  # materialised up front by create_workspace
    LAZY = "lazy"  # created by its writer on first use, if at all


class Role(StrEnum):
    DELIVERABLE = "deliverable"  # navigable artifact a user reads
    PROVENANCE = "provenance"  # machine record (jsonl)
    SENTINEL = "sentinel"  # internal signal (answer file)
    RERUN = "rerun"  # only created if a user re-runs analysis.py


@dataclass(frozen=True, slots=True)
class LayoutEntry:
    """One declared path in a run workspace."""

    key: str  # stable identifier used by accessors
    relpath: str  # path relative to the run root
    kind: Kind
    lifecycle: Lifecycle
    role: Role
    required: bool = False  # gates the completion report (only for contract entries)


# The canonical run-dir layout. ONE place. The one-shot-era scripts/logs/artifacts
# dirs are intentionally absent — the persistent-kernel engine never writes them.
ENTRIES: tuple[LayoutEntry, ...] = (
    LayoutEntry("inputs", "inputs", Kind.DIR, Lifecycle.EAGER, Role.DELIVERABLE),
    LayoutEntry("upstream", "upstream", Kind.DIR, Lifecycle.EAGER, Role.DELIVERABLE),
    LayoutEntry("figures", "figures", Kind.DIR, Lifecycle.LAZY, Role.DELIVERABLE),
    LayoutEntry("tables", "tables", Kind.DIR, Lifecycle.LAZY, Role.DELIVERABLE),
    LayoutEntry("skill_calls", "skill_calls", Kind.DIR, Lifecycle.LAZY, Role.DELIVERABLE),
    LayoutEntry("rerun", "rerun", Kind.DIR, Lifecycle.LAZY, Role.RERUN),
    LayoutEntry("result_summary", "result_summary.md", Kind.FILE, Lifecycle.LAZY, Role.DELIVERABLE, required=True),
    LayoutEntry("result", "result.json", Kind.FILE, Lifecycle.LAZY, Role.DELIVERABLE, required=True),
    LayoutEntry("analysis", "analysis.py", Kind.FILE, Lifecycle.LAZY, Role.DELIVERABLE),
    LayoutEntry("answer", "_oc_answer.txt", Kind.FILE, Lifecycle.LAZY, Role.SENTINEL),
    LayoutEntry("skill_calls_log", "skill_calls.jsonl", Kind.FILE, Lifecycle.LAZY, Role.PROVENANCE),
)

_BY_KEY: dict[str, LayoutEntry] = {entry.key: entry for entry in ENTRIES}


def entry(key: str) -> LayoutEntry:
    return _BY_KEY[key]


def relpath(key: str) -> str:
    """The run-root-relative path of a declared entry (e.g. ``relpath('answer')``)."""
    return _BY_KEY[key].relpath


def eager_dirs() -> tuple[str, ...]:
    """Relpaths ``create_workspace`` must materialise up front."""
    return tuple(e.relpath for e in ENTRIES if e.kind is Kind.DIR and e.lifecycle is Lifecycle.EAGER)


def dir_relpaths() -> tuple[str, ...]:
    """All declared directory relpaths (back-compat for ``WORKSPACE_SUBDIRS``)."""
    return tuple(e.relpath for e in ENTRIES if e.kind is Kind.DIR)


def contract_entries() -> tuple[LayoutEntry, ...]:
    """Entries that belong in the completion-report contract.

    The deliverable artifacts a finished run should expose — never the transient
    sentinel / provenance / rerun paths. Callers map these onto their own
    ArtifactRequirement type (keeping this module verification-free).
    """
    return tuple(e for e in ENTRIES if e.role is Role.DELIVERABLE)


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Typed path accessors for one run root — the schema applied to a location."""

    root: Path

    def path(self, key: str) -> Path:
        return self.root / _BY_KEY[key].relpath

    @property
    def inputs(self) -> Path:
        return self.path("inputs")

    @property
    def upstream(self) -> Path:
        return self.path("upstream")

    @property
    def figures(self) -> Path:
        return self.path("figures")

    @property
    def tables(self) -> Path:
        return self.path("tables")

    @property
    def skill_calls(self) -> Path:
        return self.path("skill_calls")

    @property
    def rerun(self) -> Path:
        return self.path("rerun")

    @property
    def result_summary(self) -> Path:
        return self.path("result_summary")

    @property
    def result(self) -> Path:
        return self.path("result")

    @property
    def analysis(self) -> Path:
        return self.path("analysis")

    @property
    def answer(self) -> Path:
        return self.path("answer")

    @property
    def skill_calls_log(self) -> Path:
        return self.path("skill_calls_log")


__all__ = [
    "ENTRIES",
    "Kind",
    "Lifecycle",
    "LayoutEntry",
    "Role",
    "RunPaths",
    "contract_entries",
    "dir_relpaths",
    "eager_dirs",
    "entry",
    "relpath",
]
