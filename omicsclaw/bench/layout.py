"""Where a campaign keeps its files, and how they are written.

One campaign owns two directories, both outside the repository:

.. code-block:: text

    <cases>/<case>/public/      copied into every workspace of that case
    <cases>/<case>/oracle/      read by graders only

    <out>/cells/<arm>/<model>/<case>/r<k>/   the agent's workspace
    <out>/meta/<arm>/<model>/<case>/r<k>/    prompt, output, logs, done.json
    <out>/predictions.jsonl  usage.jsonl  grades.jsonl  attempts.jsonl

A run's ``meta`` directory is not inside its workspace. An earlier attempt
that was set aside keeps its files under ``r<k>.<label><n>`` in both trees.
``predictions.jsonl``, ``usage.jsonl`` and ``grades.jsonl`` hold one row per
run; ``attempts.jsonl`` holds one row per attempt, the ones set aside
included, which is what a campaign's total spend is added up from.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import Case, RunSpec

__all__ = [
    "Campaign",
    "RunPaths",
    "read_json",
    "read_jsonl",
    "write_json",
    "write_jsonl",
]

PROMPT = "prompt.md"
STDOUT = "stdout.txt"
STDERR = "stderr.txt"
STAGED = "staged.json"
COMMAND = "command.json"
DONE = "done.json"
ACCESS = "access_audit.json"
AUDIT_LOG = "audit.jsonl"


@dataclass(frozen=True)
class RunPaths:
    """The two directories of one run and the files kept in ``meta``."""

    workspace: Path
    meta: Path

    @property
    def prompt(self) -> Path:
        return self.meta / PROMPT

    @property
    def stdout(self) -> Path:
        return self.meta / STDOUT

    @property
    def stderr(self) -> Path:
        return self.meta / STDERR

    @property
    def staged(self) -> Path:
        return self.meta / STAGED

    @property
    def command(self) -> Path:
        return self.meta / COMMAND

    @property
    def done(self) -> Path:
        return self.meta / DONE

    @property
    def access(self) -> Path:
        return self.meta / ACCESS

    @property
    def audit_log(self) -> Path:
        """Where an adapter points its agent's own tool-call log."""
        return self.meta / AUDIT_LOG


@dataclass(frozen=True)
class Campaign:
    """The directories of one campaign.

    :param out: Where workspaces, run records and result files are written.
    :param cases: The cases root holding each case's public files and oracle.
    """

    out: Path
    cases: Path

    @property
    def cells(self) -> Path:
        return self.out / "cells"

    @property
    def meta(self) -> Path:
        return self.out / "meta"

    @property
    def predictions(self) -> Path:
        return self.out / "predictions.jsonl"

    @property
    def usage(self) -> Path:
        return self.out / "usage.jsonl"

    @property
    def grades(self) -> Path:
        return self.out / "grades.jsonl"

    @property
    def attempts(self) -> Path:
        return self.out / "attempts.jsonl"

    def paths(self, run: RunSpec) -> RunPaths:
        """The workspace and ``meta`` directory of *run*."""
        return RunPaths(self.cells / run.key, self.meta / run.key)

    def public(self, case: Case) -> Path:
        """The directory whose contents are copied into the workspace."""
        return self.cases / case.id / "public"

    def oracle(self, case: Case) -> Path:
        """The directory graders read the case's truth from."""
        return self.cases / case.id / "oracle"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write *payload* to *path* so a reader sees the old file or the new one.

    The text goes to a temporary file beside *path*, which then takes its
    place. A write that fails leaves *path* as it was and removes the
    temporary file.
    """
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in *path*, or ``None`` when it is missing or unreadable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Replace *path* with one JSON object per line, keys sorted."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every row of *path*; an absent file is an empty list."""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
