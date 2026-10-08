"""Preparing one run: a new workspace holding only the case's public files."""

from __future__ import annotations

import datetime as _dt
import os
import shutil
from pathlib import Path
from typing import Any

from .layout import Campaign, RunPaths, read_json, write_json
from .manifest import RunSpec

__all__ = ["StageError", "set_aside", "stage", "staged_files", "utc_now"]


class StageError(RuntimeError):
    """A run could not be staged."""


def utc_now() -> str:
    """The current time as an ISO 8601 string in UTC."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def stage(run: RunSpec, campaign: Campaign) -> RunPaths:
    """Create the workspace and ``meta`` directory of *run*.

    The workspace receives a copy of the case's ``public/`` tree, with
    symbolic links replaced by the files they point to, and the parent
    directories of the declared deliverables. ``meta`` receives the prompt
    and ``staged.json``, a list of the copied files with their size and
    modification time.

    :returns: The run's paths.
    :raises StageError: The case has no ``public/`` directory, or the
        workspace or ``meta`` directory already exists.
    """
    paths = campaign.paths(run)
    public = campaign.public(run.case)
    if not public.is_dir():
        raise StageError(f"case {run.case.id!r} has no public directory: {public}")
    for existing in (paths.workspace, paths.meta):
        if existing.exists():
            raise StageError(f"{existing} already exists; set it aside first")

    paths.workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(public, paths.workspace, symlinks=False)
    for deliverable in run.case.deliverables:
        (paths.workspace / deliverable).parent.mkdir(parents=True, exist_ok=True)

    paths.meta.mkdir(parents=True)
    paths.prompt.write_text(run.case.prompt, encoding="utf-8")
    write_json(paths.staged, {
        **run.identity(),
        "staged_at": utc_now(),
        "files": _inventory(paths.workspace),
    })
    return paths


def staged_files(paths: RunPaths) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` for the files :func:`stage` copied."""
    record = read_json(paths.staged) or {}
    return {
        str(entry["path"]): (int(entry["bytes"]), int(entry["mtime_ns"]))
        for entry in record.get("files", [])
    }


def set_aside(paths: RunPaths, label: str) -> str | None:
    """Rename an earlier attempt's directories out of the way.

    Both directories get the same new name, ``r<k>.<label><n>`` with the
    lowest unused *n*.

    :param label: Why the attempt is set aside, such as ``infra``.
    :returns: The suffix used, or ``None`` when neither directory existed.
    """
    present = [path for path in (paths.workspace, paths.meta) if path.exists()]
    if not present:
        return None
    both = (paths.workspace, paths.meta)
    index = 1
    while any(_aside(path, label, index).exists() for path in both):
        index += 1
    for path in present:
        os.replace(path, _aside(path, label, index))
    return f"{label}{index}"


def _aside(path: Path, label: str, index: int) -> Path:
    return path.with_name(f"{path.name}.{label}{index}")


def _inventory(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            status = path.stat()
            files.append({
                "path": path.relative_to(root).as_posix(),
                "bytes": status.st_size,
                "mtime_ns": status.st_mtime_ns,
            })
    return files
