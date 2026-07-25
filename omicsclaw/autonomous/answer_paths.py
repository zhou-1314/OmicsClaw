"""Validation of the filesystem paths a mini-agent answer claims.

The mini-agent writes its own closing summary via ``ReturnAnswer(...)``, and the
outer loop treats that text as ground truth when it goes looking for artifacts.
Nothing made the model's path claims *true*: its kernel is confined to the run
workspace, but the answer is free text and can name any path at all.

A real run (2026-07-25) reported outputs under a sibling directory it had never
written; the outer agent then listed that path three times, got "Directory not
found" each time, and exhausted the turn's tool-iteration budget without ever
reading the real artifacts. Checking the claims here turns a silent lie into a
correction the outer loop can act on in one step.

Pure filesystem + string work; no kernel or LLM dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

# POSIX-style absolute paths as they appear in prose: start at "/", run until
# whitespace, and drop trailing punctuation that belongs to the sentence rather
# than the path (":" from "saved to <dir>:", a closing paren, a full stop).
_ABS_PATH_RE = re.compile(r"(?<![\w/])(/[^\s`'\"]+)")
_TRAILING_PUNCTUATION = ":,;.)]}>'\"`"


def iter_claimed_paths(answer: str) -> list[str]:
    """Absolute paths named in ``answer``, de-duplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for raw in _ABS_PATH_RE.findall(answer or ""):
        candidate = raw.rstrip(_TRAILING_PUNCTUATION).rstrip("/")
        # A bare "/" or a single segment is prose punctuation, not a real claim.
        if candidate.count("/") < 2:
            continue
        seen.setdefault(candidate, None)
    return list(seen)


def unresolved_answer_paths(answer: str, *, workspace_root: str | Path) -> list[str]:
    """Paths the answer claims that do not exist on disk.

    ``workspace_root`` itself is never reported: naming the run directory is
    always legitimate, and it is created before the loop starts.
    """
    root = Path(workspace_root).expanduser()
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    unresolved: list[str] = []
    for claimed in iter_claimed_paths(answer):
        path = Path(claimed)
        if path.exists():
            continue
        try:
            if path.resolve() == root_resolved:
                continue
        except OSError:
            pass
        unresolved.append(claimed)
    return unresolved


__all__ = ["iter_claimed_paths", "unresolved_answer_paths"]
