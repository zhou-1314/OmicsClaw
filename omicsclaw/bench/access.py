"""Looking, after a run, for signs that it reached where it should not.

The audit reads three things: the tool calls the adapter recovered, the
agent's audit log, and the text files the run left in its workspace. Each
is searched for:

``cases_root``
    Any mention of the cases root, where the oracle lives.
``out_root``
    Any mention of the output root other than the run's own workspace:
    another arm's workspace, an earlier attempt kept beside this one, the
    run records, the result files.
``leaves_workspace``
    A relative path that climbs out of the workspace with ``..``.
patterns from the manifest
    Regular expressions, reported under their own text.

A match is recorded and the run is flagged; nothing is blocked and no run
is changed.

It finds what was written down. A path assembled at run time, or a file
reached by listing a directory, leaves nothing to match. ``leaves_workspace``
takes a command's paths as relative to the workspace root, so a command
that first changes into a subdirectory and then uses ``..`` is matched
though it stays inside.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .outcome import Command

__all__ = ["LEAVES_WORKSPACE", "Pattern", "audit_access", "run_patterns"]

MAX_FILE_BYTES = 1_048_576
"""Workspace files larger than this are not read."""

MAX_HITS = 50
"""Matches recorded per run; the report says when there were more."""

LEAVES_WORKSPACE = "leaves_workspace"

_EXCERPT = 80
_NAME_END = r"(?![\w-]|\.[\w-])"
"""What must follow a directory's path for the text to name that directory
and not a sibling whose name merely starts the same way. A full stop that
ends a sentence does not continue a name; one followed by a letter does."""

_PATHISH = re.compile(r"[^\s\"'`;|&<>(){}\[\]$=,:\\]+")
"""A run of characters that can be one path in a command or in source text."""


@dataclass(frozen=True)
class Pattern:
    """One thing to look for.

    :param label: What a match is reported as.
    :param regex: The compiled expression.
    """

    label: str
    regex: re.Pattern[str]


def run_patterns(
    cases: Path, out: Path, workspace: Path, extra: Sequence[str] = ()
) -> tuple[Pattern, ...]:
    """The patterns one run is searched for.

    :param cases: The cases root.
    :param out: The campaign's output root.
    :param workspace: The run's own workspace, the one place under *out*
        it may name.
    :param extra: Regular expressions from the manifest.

    A root is matched as a whole path, under the spelling given and under
    its resolved one: ``/x/bench`` does not match inside ``/x/bench-out``.
    """
    try:
        relative = workspace.resolve().relative_to(out.resolve())
        own = f"(?!/{re.escape(relative.as_posix())}{_NAME_END})"
    except ValueError:
        own = ""
    patterns = [
        Pattern("cases_root", re.compile(_spellings(cases) + _NAME_END)),
        Pattern("out_root", re.compile(_spellings(out) + _NAME_END + own)),
    ]
    patterns.extend(Pattern(text, re.compile(text)) for text in extra)
    return tuple(patterns)


def audit_access(
    *,
    patterns: Sequence[Pattern],
    commands: Iterable[Command] | None,
    audit_log: Path,
    workspace: Path,
    unchanged: Mapping[str, tuple[int, int]],
    skip: Sequence[str] = (),
) -> dict[str, Any]:
    """Search one run and report what matched.

    :param patterns: What :func:`run_patterns` returned for the run.
    :param commands: Tool calls recovered by the adapter, or ``None`` when
        it could recover none. The report then says the commands were not
        scanned, which is not the same as finding nothing in them.
    :param audit_log: The agent's audit log; a missing file is skipped.
    :param workspace: The run's workspace. Files over
        :data:`MAX_FILE_BYTES` and files containing a NUL byte are not
        read.
    :param unchanged: ``{relative path: (size, mtime_ns)}`` of the staged
        files; one that still matches is the case's own input and is not
        read.
    :param skip: Workspace-relative paths not to descend into.
    :returns: ``flagged``, ``matches``, ``by_pattern`` (every label
        searched for with its number of matches, zero included), ``hits``
        (at most :data:`MAX_HITS`, each with ``source``, ``where``,
        ``pattern`` and ``excerpt``), ``truncated``, ``commands_scanned``
        (``None`` when *commands* was), and the number of files read and
        skipped. ``cases_root`` and ``out_root`` match a path that was
        written out; ``leaves_workspace`` is a judgement about a relative
        path and can be wrong, so the counts are kept apart.
    """
    hits: list[dict[str, str]] = []
    total = 0
    counts = {pattern.label: 0 for pattern in patterns} | {LEAVES_WORKSPACE: 0}
    home = {str(workspace), str(workspace.resolve())}

    def record(source: str, where: str, label: str, text: str, span: range) -> None:
        nonlocal total
        total += 1
        counts[label] += 1
        if len(hits) < MAX_HITS:
            hits.append({
                "source": source,
                "where": where,
                "pattern": label,
                "excerpt": _excerpt(text, span.start, span.stop),
            })

    def search(source: str, where: str, text: str, depth: int = 0) -> None:
        for pattern in patterns:
            for match in pattern.regex.finditer(text):
                record(source, where, pattern.label, text, range(*match.span()))
        for span in _climbs(text, depth, home):
            record(source, where, LEAVES_WORKSPACE, text, span)

    scanned: int | None = None
    if commands is not None:
        scanned = 0
        for command in commands:
            scanned += 1
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
        depth = len(PurePosixPath(relative).parent.parts)
        search("file", relative, raw.decode("utf-8", errors="replace"), depth)

    return {
        "flagged": total > 0,
        "matches": total,
        "by_pattern": counts,
        "hits": hits,
        "truncated": total > len(hits),
        "commands_scanned": scanned,
        "files_read": read,
        "files_skipped": skipped,
        "patterns": list(counts),
    }


def _spellings(root: Path) -> str:
    """An expression for *root* as given and as resolved."""
    spellings = sorted({str(root), str(root.resolve())})
    return "(?:" + "|".join(re.escape(spelling) for spelling in spellings) + ")"


def _climbs(text: str, depth: int, home: set[str]) -> Iterator[range]:
    """Where *text* holds a path that climbs out of the workspace.

    :param depth: How many directories below the workspace root a relative
        path starts from: ``0`` for a command, the depth of its own
        directory for a file.
    :param home: Spellings of the workspace's absolute path. An absolute
        path is judged only when it starts with one of them, on the part
        after it.
    """
    for match in _PATHISH.finditer(text):
        token = match.group()
        if ".." not in token:
            continue
        level = depth
        if token.startswith("/"):
            inside = [
                token[len(prefix):]
                for prefix in home
                if token.startswith(prefix + "/")
            ]
            if not inside:
                continue
            token, level = inside[0], 0
        for segment in token.split("/"):
            if segment in ("", "."):
                continue
            level += -1 if segment == ".." else 1
            if level < 0:
                yield range(*match.span())
                break


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
