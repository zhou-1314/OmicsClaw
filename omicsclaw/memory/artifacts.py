"""The ``artifacts`` table over one :class:`~omicsclaw.memory.database.Database`.

P2 of the front-back plan. An *artifact* is one file a run produced that
is worth showing a person as a result — a figure, a table, the processed
``.h5ad`` — registered with a stable id, a kind, a checksum and the job
or session that produced it. The schema lives in
:mod:`omicsclaw.memory.database` (created on open, like ``jobs``), this
module owns the reads and writes, and both halves of the desktop entry
layer — the jobs-plane scanner and the ``save_artifact`` tool — reach the
same table through it. It deliberately sits in the memory layer rather
than in ``entry.desktop`` because the tools layer may not import the
entry layer, and ``save_artifact`` needs a store.

Kinds are a closed starter vocabulary, the one the plan fixed:
``figure | table | h5ad | rds | report | pdf | other``. The words and
the extension tables live in :mod:`omicsclaw.schema.artifacts` — the
shared vocabulary package — because the tools layer's ``save_artifact``
offers the same enum to the model and may not import this module. The
mapping from a filename is by extension, and every unrecognized
extension lands on ``other`` rather than being refused — a file the
scanner found is a fact about the run, not an argument to validate.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from omicsclaw.memory.database import Database
from omicsclaw.schema.artifacts import ARTIFACT_KINDS, kind_and_mime_for

__all__ = [
    "ARTIFACT_KINDS",
    "ArtifactRecord",
    "ArtifactStore",
    "HASH_CHUNK_BYTES",
    "kind_and_mime_for",
]

HASH_CHUNK_BYTES: Final = 1024 * 1024
"""Bytes read per chunk when streaming a file through sha256. A whole
h5ad can be hundreds of megabytes; reading it in one gulp would pin that
much memory per artifact at exactly the moment a job has just finished
allocating the most."""

HASH_MAX_BYTES: Final = 512 * 1024 * 1024
"""Largest file the registrar will hash. Above it the row is registered
with an empty ``sha256`` and ``meta["sha256_skipped"]`` naming the size:
the artifact stays visible and servable, and the checksum that would
have cost a half-gigabyte read is the thing that gives."""

_COLUMNS: Final = (
    "id, job_id, session_id, kind, path, title, mime, thumb_path, sha256,"
    " size, produced_by, parent_ids_json, meta_json, created_at"
)


def hash_and_size(path: Path) -> tuple[str, int]:
    """Stream *path* through sha256, answering ``(hex, bytes)``.

    A file larger than :data:`HASH_MAX_BYTES` answers ``("", size)`` —
    the size is still cheap and true, and the checksum is the part that
    can be skipped. Raises propagate: the caller names the file in the
    error, this only reads it.
    """
    size = path.stat().st_size
    if size > HASH_MAX_BYTES:
        return "", size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One row of the ``artifacts`` table.

    ``job_id`` is ``""`` for an artifact the ``save_artifact`` tool
    registered outside any job (the session-level case); the payload
    projects that as ``None`` so the wire stays honest about the
    distinction. ``path`` is the absolute filesystem path — the artifact
    always lives inside the workspace, which is what lets ``/files/serve``
    serve it with no new file-serving surface.
    """

    id: str
    job_id: str = ""
    session_id: str = ""
    kind: str = "other"
    path: str = ""
    title: str = ""
    mime: str = ""
    thumb_path: str = ""
    sha256: str = ""
    size: int = 0
    produced_by: str = ""
    parent_ids: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def as_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.id,
            "job_id": self.job_id or None,
            "session_id": self.session_id or None,
            "kind": self.kind,
            "path": self.path,
            "title": self.title,
            "mime": self.mime or None,
            "thumb_path": self.thumb_path or None,
            "sha256": self.sha256,
            "size": self.size,
            "produced_by": self.produced_by or None,
            "parent_ids": list(self.parent_ids),
            "meta": dict(self.meta),
            "created_at": self.created_at,
        }


class ArtifactStore:
    """The ``artifacts`` table over one SQLite connection, in the shape
    :class:`~omicsclaw.entry.desktop.jobs_manager.JobStore` established:
    synchronous single-row writes under the ``Database`` lock, no web
    framework anywhere in sight.

    Idempotency is by ``(job_id, path)``: the same job scanned twice
    registers nothing the second time, which is what makes a re-scan (or
    a scan racing a retry) a no-op rather than a duplicate tray. A
    session-level artifact (empty ``job_id``) is checked the same way, so
    ``save_artifact`` called twice on one file also answers the same row
    rather than a second one.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def insert_artifact(self, record: ArtifactRecord) -> ArtifactRecord | None:
        """Insert one row unless ``(job_id, path)`` is already there.

        :returns: the record as inserted, or ``None`` when the pair
            already exists (the idempotent no-op).
        """
        inserted: list[bool] = []

        def _write(conn: Any) -> None:
            cursor = conn.execute(
                "INSERT INTO artifacts (id, job_id, session_id, kind, path,"
                " title, mime, thumb_path, sha256, size, produced_by,"
                " parent_ids_json, meta_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO NOTHING",
                _as_row(record),
            )
            inserted.append(cursor.rowcount == 1)

        # The (job_id, path) check under the same lock as the insert is
        # what makes two concurrent scanners agree on one row: the second
        # one to arrive sees the first one's row and answers None.
        def _checked(conn: Any) -> None:
            rows = conn.execute(
                "SELECT 1 FROM artifacts WHERE job_id = ? AND path = ? LIMIT 1",
                (record.job_id, record.path),
            ).fetchall()
            if rows:
                inserted.append(False)
                return
            _write(conn)

        self._db.run(_checked)
        return record if (inserted and inserted[0]) else None

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT " + _COLUMNS + " FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchall()
        )
        return _row_to_record(rows[0]) if rows else None

    def list_artifacts(
        self,
        *,
        job_id: str = "",
        session_id: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> list[ArtifactRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT " + _COLUMNS + " FROM artifacts " + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        )
        return [_row_to_record(row) for row in rows]

    def count_for_job(self, job_id: str) -> int:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE job_id = ?",
                (job_id,),
            ).fetchall()
        )
        return int(rows[0]["n"] or 0) if rows else 0

    def latest_for_path(self, path: str) -> ArtifactRecord | None:
        """The newest row registered for one filesystem path, if any.

        The attribution ledger's read half: a scanner deciding whether a
        file belongs to the job it is scanning for looks here first, and
        leaves the file with its earlier job when nothing changed it
        since that job registered it.
        """
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT " + _COLUMNS + " FROM artifacts WHERE path = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (path,),
            ).fetchall()
        )
        return _row_to_record(rows[0]) if rows else None


def new_artifact_id() -> str:
    """A fresh artifact id, hex like a job id and never confused with one
    on the wire (the payload carries the field names)."""
    return "art_" + secrets.token_hex(12)


def _as_row(record: ArtifactRecord) -> tuple[Any, ...]:
    return (
        record.id or new_artifact_id(),
        record.job_id,
        record.session_id,
        record.kind,
        record.path,
        record.title,
        record.mime,
        record.thumb_path,
        record.sha256,
        record.size,
        record.produced_by,
        json.dumps(list(record.parent_ids)),
        json.dumps(dict(record.meta), ensure_ascii=False, default=str),
        record.created_at or time.time(),
    )


def _row_to_record(row: Any) -> ArtifactRecord:
    try:
        parent_ids = json.loads(row["parent_ids_json"]) if row["parent_ids_json"] else []
    except (TypeError, ValueError):
        parent_ids = []
    if not isinstance(parent_ids, list):
        parent_ids = []
    try:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return ArtifactRecord(
        id=str(row["id"]),
        job_id=str(row["job_id"] or ""),
        session_id=str(row["session_id"] or ""),
        kind=str(row["kind"] or "other"),
        path=str(row["path"] or ""),
        title=str(row["title"] or ""),
        mime=str(row["mime"] or ""),
        thumb_path=str(row["thumb_path"] or ""),
        sha256=str(row["sha256"] or ""),
        size=int(row["size"] or 0),
        produced_by=str(row["produced_by"] or ""),
        parent_ids=tuple(str(value) for value in parent_ids),
        meta=meta,
        created_at=float(row["created_at"] or 0.0),
    )
