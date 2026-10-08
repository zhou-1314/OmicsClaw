"""Looking, after a run, for signs that it reached where it should not.

The audit reads three things: the tool calls the adapter recovered, the
agent's audit log, and the text files the run left in its workspace. Each
is searched for a list of patterns. A match is recorded and the run is
flagged; nothing is blocked and no run is changed.

It finds what was written down. A path assembled at run time, or a file
reached by listing a directory, leaves nothing to match.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .outcome import Command

__all__ = ["Pattern", "audit_access", "campaign_patterns"]

MAX_FILE_BYTES = 1_048_576
"""Workspace files larger than this are not read."""

MAX_HITS = 50
"""Matches recorded per run; the report says when there were more."""

_EXCERPT = 80


@dataclass(frozen=True)
class Pattern:
    """One thing to look for.

    :param label: What a match is reported as.
    :param regex: The compiled expression.
    """

    label: str
    regex: re.Pattern[str]


def campaign_patterns(
    cases: Path, meta: Path, extra: Sequence[str] = ()
) -> tuple[Pattern, ...]:
    """The patterns of one campaign.

    :param cases: The cases root; any mention of it is a match, since the
        oracle lives there and a workspace only holds copies.
    :param meta: The campaign's ``meta`` root, where run records are kept.
    :param extra: Regular expressions from the manifest.
    """
    patterns = []
    for label, root in (("cases_root", cases), ("meta_root", meta)):
        spellings = {str(root), str(root.resolve())}
        expression = "|".join(re.escape(spelling) for spelling in sorted(spellings))
        patterns.append(Pattern(label, re.compile(expression)))
    patterns.extend(Pattern(text, re.compile(text)) for text in extra)
    return tuple(patterns)


def audit_access(
    *,
    patterns: Sequence[Pattern],
    commands: Iterable[Command],
    audit_log: Path,
    workspace: Path,
    unchanged: Mapping[str, tuple[int, int]],
    skip: Sequence[str] = (),
) -> dict[str, Any]:
    """Search one run for *patterns* and report what matched.

    :param commands: Tool calls recovered by the adapter.
    :param audit_log: The agent's audit log; a missing file is skipped.
    :param workspace: The run's workspace. Files over
        :data:`MAX_FILE_BYTES` and files containing a NUL byte are not
        read.
    :param unchanged: ``{relative path: (size, mtime_ns)}`` of the staged
        files; one that still matches is the case's own input and is not
        read.
    :param skip: Workspace-relative paths not to descend into.
    :returns: ``flagged``, ``hits`` (at most :data:`MAX_HITS`, each with
        ``source``, ``where``, ``pattern`` and ``excerpt``), ``truncated``
        and the number of files read and skipped.
    """
    hits: list[dict[str, str]] = []
    total = 0

    def search(source: str, where: str, text: str) -> None:
        nonlocal total
        for pattern in patterns:
            for match in pattern.regex.finditer(text):
                total += 1
                if len(hits) < MAX_HITS:
                    hits.append({
                        "source": source,
                        "where": where,
                        "pattern": pattern.label,
                        "excerpt": _excerpt(text, match.start(), match.end()),
                    })

    for command in commands:
        search("command", command.tool, command.text)
    try:
        lines = audit_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for number, line in enumerate(lines, start=1):
        search("audit_log", f"line {number}", line)

    read = skipped = 0
    for path in _files(workspace, skip):
        relative = path.relative_to(workspace).as_posix()
        try:
            status = path.stat()
            if unchanged.get(relative) == (status.st_size, status.st_mtime_ns):
                continue
            if status.st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            raw = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        if b"\0" in raw:
            skipped += 1
            continue
        read += 1
        search("file", relative, raw.decode("utf-8", errors="replace"))

    return {
        "flagged": total > 0,
        "matches": total,
        "hits": hits,
        "truncated": total > len(hits),
        "files_read": read,
        "files_skipped": skipped,
        "patterns": [pattern.label for pattern in patterns],
    }


def _files(workspace: Path, skip: Sequence[str]) -> list[Path]:
    excluded = [workspace / entry for entry in skip]
    found = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if any(root == path or root in path.parents for root in excluded):
            continue
        found.append(path)
    return found


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - _EXCERPT)
    right = min(len(text), end + _EXCERPT)
    return " ".join(text[left:right].split())
