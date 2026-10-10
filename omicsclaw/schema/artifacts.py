"""The artifact vocabulary: the kinds every layer names artifacts by.

P2 of the front-back plan gave this vocabulary exactly seven words —
``figure | table | h5ad | rds | report | pdf | other`` — and two layers
have to agree on it without either importing the other: the tools layer
(``save_artifact`` offers the enum to the model) and the memory layer
(the store persists it). That is precisely what this package is — the
one vocabulary the layers share — so the words live here rather than in
either of them, and the extension tables that pick a kind come with
them because two of those tables would be two places the same
``.h5ad`` could stop meaning ``h5ad``.

``.md`` reads as ``report``: ``report.md`` is the convention every
curated skill's CLI writes for its summary. An unrecognized extension
lands on ``other`` and ``application/octet-stream`` — never an error,
because the kind of a file that exists is a fact to record, not an
argument to validate. Additive-only within a schema version: a client
that meets an unknown kind falls back to a generic icon, exactly as it
falls back to ``other``.
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
