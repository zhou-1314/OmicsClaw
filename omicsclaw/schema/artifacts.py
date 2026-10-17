"""Define shared artifact types.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_MIME_FALLBACK",
    "kind_and_mime_for",
]

ARTIFACT_KINDS: Final = frozenset(
    {"figure", "table", "h5ad", "rds", "report", "pdf", "other"}
)

ARTIFACT_MIME_FALLBACK: Final = "application/octet-stream"

_KIND_BY_SUFFIX: Final = {
    ".png": "figure",
    ".svg": "figure",
    ".jpg": "figure",
    ".jpeg": "figure",
    ".csv": "table",
    ".tsv": "table",
    ".h5ad": "h5ad",
    ".rds": "rds",
    ".html": "report",
    ".htm": "report",
    ".md": "report",
    ".pdf": "pdf",
}

_MIME_BY_SUFFIX: Final = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".h5ad": "application/x-hdf5",
    ".rds": "application/x-rds",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
}


def kind_and_mime_for(name: str) -> tuple[str, str]:
    """The ``(kind, mime)`` a filename maps to, per the extension tables.

    Never raises and never returns a kind outside :data:`ARTIFACT_KINDS`.
    """
    suffix = Path(name).suffix.lower()
    return (
        _KIND_BY_SUFFIX.get(suffix, "other"),
        _MIME_BY_SUFFIX.get(suffix, ARTIFACT_MIME_FALLBACK),
    )
