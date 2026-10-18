"""Durable notebook cells and execution records.

NotebookStore is the durable source of truth for cell state. The in-memory
TurnStream/SSE ring remains a delivery cache and is never used for recovery or
audit. Stored output is data and is never executed by this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .database import Database


@dataclass(frozen=True)
class NotebookCell:
    notebook_id: str
    cell_id: str
    source: str = ""
    revision: int = 0
    status: str = "idle"
    output: Any = None
    provenance: Any = None
    trust_label: str = ""
    session_epoch: int = 0
    client_command_id: str = ""
    execution_id: str = ""
    updated_at: float = 0.0
    language: str = "python"
    created_at: float = 0.0
    cell_index: int | None = None


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    notebook_id: str
    cell_id: str
    session_epoch: int = 0
    client_command_id: str = ""
    status: str = "queued"
    output: Any = None
    provenance: Any = None
    trust_label: str = ""
    revision: int = 0
    created_at: float = 0.0
    finished_at: float | None = None
    cell_index: int | None = None


class RevisionConflict(ValueError):
    """Raised when a cell write is based on a stale revision."""


class ExecutionConflict(ValueError):
    """Raised when an execution or command id is reused with new data."""


_ALLOWED_TRANSITIONS = {
    "queued": {"queued", "running", "completed", "failed", "interrupted"},
    "running": {"running", "completed", "failed", "interrupted"},
    "completed": {"completed"},
    "failed": {"failed"},
    "interrupted": {"interrupted"},
}
_MISSING = object()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None) -> Any:
    return json.loads(value) if value else None


def _cell(row: Any) -> NotebookCell:
    keys = row.keys() if hasattr(row, "keys") else ()
    created_at = (
        row["created_at"]
        if "created_at" in keys and row["created_at"] is not None
        else row["updated_at"]
    )
    cell_index = row["cell_index"] if "cell_index" in keys else None
    return NotebookCell(
        notebook_id=row["notebook_id"],
        cell_id=row["cell_id"],
        source=row["source"],
        revision=row["revision"],
        status=row["status"],
        output=_load(row["output_json"]),
        provenance=_load(row["provenance_json"]),
        trust_label=row["trust_label"],
        session_epoch=row["session_epoch"],
        client_command_id=row["client_command_id"],
        execution_id=row["execution_id"],
        updated_at=row["updated_at"],
        language=row["language"],
        created_at=created_at,
        cell_index=cell_index,
    )


def _execution(row: Any) -> ExecutionRecord:
    keys = row.keys() if hasattr(row, "keys") else ()
    cell_index = row["cell_index"] if "cell_index" in keys else None
    return ExecutionRecord(
        execution_id=row["execution_id"],
        notebook_id=row["notebook_id"],
        cell_id=row["cell_id"],
        session_epoch=row["session_epoch"],
        client_command_id=row["client_command_id"],
        status=row["status"],
        output=_load(row["output_json"]),
        provenance=_load(row["provenance_json"]),
        trust_label=row["trust_label"],
        revision=row["revision"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        cell_index=cell_index,
    )


def _same_execution(left: ExecutionRecord, right: ExecutionRecord) -> bool:
    return (
        left.execution_id == right.execution_id
        and left.notebook_id == right.notebook_id
        and left.cell_id == right.cell_id
        and left.session_epoch == right.session_epoch
        and left.client_command_id == right.client_command_id
        and left.status == right.status
        and left.output == right.output
        and left.provenance == right.provenance
        and left.trust_label == right.trust_label
        and left.revision == right.revision
        and left.finished_at == right.finished_at
    )


class NotebookStore:
    """SQLite-backed notebook state with CAS revisions and idempotency."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def put_cell(
        self,
        cell: NotebookCell,
        expected_revision: int | None = None,
    ) -> NotebookCell:
        return await self._db.arun(
            lambda connection: self._put_cell(connection, cell, expected_revision)
        )

    async def save_cell(
        self,
        cell: NotebookCell,
        expected_revision: int | None = None,
    ) -> NotebookCell:
        return await self.put_cell(cell, expected_revision)

    def _put_cell(
        self,
        connection: Any,
        cell: NotebookCell,
        expected_revision: int | None,
    ) -> NotebookCell:
        key = (cell.notebook_id, cell.cell_id, cell.session_epoch)
        old = connection.execute(
            "SELECT revision, created_at, cell_index, updated_at FROM notebook_cells "
            "WHERE notebook_id=? AND cell_id=? AND session_epoch=?",
            key,
        ).fetchone()
        now = time.time()
        if old is not None:
            current_revision = old["revision"]
            if expected_revision is not None and current_revision != expected_revision:
                raise RevisionConflict(
                    f"cell revision {current_revision} != expected {expected_revision}"
                )
            base_revision = (
                current_revision if expected_revision is None else expected_revision
            )
            revision = base_revision + 1
            created_at = (
                old["created_at"]
                if old["created_at"] is not None
                else (cell.created_at or old["updated_at"] or now)
            )
            cell_index = (
                old["cell_index"]
                if old["cell_index"] is not None
                else (
                    cell.cell_index
                    if cell.cell_index is not None
                    else connection.execute(
                        "SELECT COALESCE(MAX(cell_index), 0) + 1 FROM notebook_cells WHERE notebook_id=? AND session_epoch=?",
                        (cell.notebook_id, cell.session_epoch),
                    ).fetchone()[0]
                )
            )
            cursor = connection.execute(
                "UPDATE notebook_cells SET source=?, revision=?, status=?, "
                "output_json=?, provenance_json=?, trust_label=?, "
                "client_command_id=?, execution_id=?, updated_at=?, language=?, "
                "created_at=?, cell_index=? "
                "WHERE notebook_id=? AND cell_id=? AND session_epoch=? "
                "AND revision=?",
                (
                    cell.source,
                    revision,
                    cell.status,
                    _json(cell.output),
                    _json(cell.provenance),
                    cell.trust_label,
                    cell.client_command_id,
                    cell.execution_id,
                    now,
                    cell.language,
                    created_at,
                    cell_index,
                    *key,
                    base_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    f"cell {cell.notebook_id}/{cell.cell_id} changed concurrently"
                )
        else:
            if expected_revision not in (None, 0):
                raise RevisionConflict(
                    f"cell does not exist; expected {expected_revision}"
                )
            revision = cell.revision or 1
            created_at = cell.created_at or now
            cell_index = (
                cell.cell_index
                if cell.cell_index is not None
                else connection.execute(
                    "SELECT COALESCE(MAX(cell_index), 0) + 1 FROM notebook_cells WHERE notebook_id=? AND session_epoch=?",
                    (cell.notebook_id, cell.session_epoch),
                ).fetchone()[0]
            )
            try:
                connection.execute(
                    "INSERT INTO notebook_cells "
                    "(notebook_id, cell_id, session_epoch, source, revision, status, "
                    "output_json, provenance_json, trust_label, client_command_id, "
                    "execution_id, updated_at, language, created_at, cell_index) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cell.notebook_id,
                        cell.cell_id,
                        cell.session_epoch,
                        cell.source,
                        revision,
                        cell.status,
                        _json(cell.output),
                        _json(cell.provenance),
                        cell.trust_label,
                        cell.client_command_id,
                        cell.execution_id,
                        now,
                        cell.language,
                        created_at,
                        cell_index,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RevisionConflict(
                    f"cell {cell.notebook_id}/{cell.cell_id} was created concurrently"
                ) from exc
        return self._get_cell(connection, *key)

    async def get_cell(
        self,
        notebook_id: str,
        cell_id: str,
        session_epoch: int = 0,
    ) -> NotebookCell | None:
        return await self._db.arun(
            lambda connection: self._get_cell(
                connection, notebook_id, cell_id, session_epoch
            )
        )

    def _get_cell(
        self,
        connection: Any,
        notebook_id: str,
        cell_id: str,
        session_epoch: int = 0,
    ) -> NotebookCell | None:
        row = connection.execute(
            "SELECT * FROM notebook_cells "
            "WHERE notebook_id=? AND cell_id=? AND session_epoch=?",
            (notebook_id, cell_id, session_epoch),
        ).fetchone()
        return _cell(row) if row is not None else None

    async def list_cells(
        self,
        notebook_id: str,
        session_epoch: int = 0,
    ) -> list[NotebookCell]:
        return await self._db.arun(
            lambda connection: [
                _cell(row)
                for row in connection.execute(
                    "SELECT * FROM notebook_cells "
                    "WHERE notebook_id=? AND session_epoch=? ORDER BY rowid",
                    (notebook_id, session_epoch),
                ).fetchall()
            ]
        )

    async def record_execution(self, record: ExecutionRecord) -> ExecutionRecord:
        return await self._db.arun(
            lambda connection: self._record_execution(connection, record)
        )

    def _record_execution(
        self,
        connection: Any,
        record: ExecutionRecord,
    ) -> ExecutionRecord:
        existing = connection.execute(
            "SELECT * FROM notebook_executions WHERE execution_id=?",
            (record.execution_id,),
        ).fetchone()
        if existing is None and record.client_command_id:
            existing = connection.execute(
                "SELECT * FROM notebook_executions "
                "WHERE client_command_id=? AND session_epoch=? "
                "AND notebook_id=? AND cell_id=?",
                (
                    record.client_command_id,
                    record.session_epoch,
                    record.notebook_id,
                    record.cell_id,
                ),
            ).fetchone()
        if existing is not None:
            stored = _execution(existing)
            if not _same_execution(stored, record):
                raise ExecutionConflict(
                    "execution_id or client_command_id was reused with different data"
                )
            return stored

        created_at = record.created_at or time.time()
        cell_index = record.cell_index
        if cell_index is None:
            c_row = connection.execute(
                "SELECT cell_index FROM notebook_cells WHERE notebook_id=? AND cell_id=? AND session_epoch=?",
                (record.notebook_id, record.cell_id, record.session_epoch),
            ).fetchone()
            if c_row is not None and c_row[0] is not None:
                cell_index = c_row[0]
            else:
                cell_index = connection.execute(
                    "SELECT COALESCE(MAX(cell_index), 0) + 1 FROM notebook_cells WHERE notebook_id=? AND session_epoch=?",
                    (record.notebook_id, record.session_epoch),
                ).fetchone()[0]
        try:
            connection.execute(
                "INSERT INTO notebook_executions "
                "(execution_id, notebook_id, cell_id, session_epoch, "
                "client_command_id, status, output_json, provenance_json, "
                "trust_label, revision, created_at, finished_at, cell_index) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.execution_id,
                    record.notebook_id,
                    record.cell_id,
                    record.session_epoch,
                    record.client_command_id,
                    record.status,
                    _json(record.output),
                    _json(record.provenance),
                    record.trust_label,
                    record.revision,
                    created_at,
                    record.finished_at,
                    cell_index,
                ),
            )
        except sqlite3.IntegrityError:
            winner = connection.execute(
                "SELECT * FROM notebook_executions WHERE execution_id=?",
                (record.execution_id,),
            ).fetchone()
            if winner is None and record.client_command_id:
                winner = connection.execute(
                    "SELECT * FROM notebook_executions "
                    "WHERE client_command_id=? AND session_epoch=? "
                    "AND notebook_id=? AND cell_id=?",
                    (
                        record.client_command_id,
                        record.session_epoch,
                        record.notebook_id,
                        record.cell_id,
                    ),
                ).fetchone()
            if winner is None:
                raise
            stored = _execution(winner)
            if not _same_execution(stored, record):
                raise ExecutionConflict(
                    "execution or command id raced with different data"
                )
            return stored

        row = connection.execute(
            "SELECT * FROM notebook_executions WHERE execution_id=?",
            (record.execution_id,),
        ).fetchone()
        return _execution(row)

    async def update_execution(
        self,
        execution_id: str,
        *,
        status: str | None = None,
        output: Any = _MISSING,
        provenance: Any = _MISSING,
        trust_label: str | None = None,
        finished_at: float | None | object = _MISSING,
    ) -> ExecutionRecord:
        return await self._db.arun(
            lambda connection: self._update_execution(
                connection,
                execution_id,
                status,
                output,
                provenance,
                trust_label,
                finished_at,
            )
        )

    def _update_execution(
        self,
        connection: Any,
        execution_id: str,
        status: str | None,
        output: Any,
        provenance: Any,
        trust_label: str | None,
        finished_at: float | None | object,
    ) -> ExecutionRecord:
        current_row = connection.execute(
            "SELECT * FROM notebook_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if current_row is None:
            raise KeyError(f"unknown execution_id {execution_id!r}")
        current = _execution(current_row)
        values: dict[str, Any] = {}
        if status is not None:
            allowed = _ALLOWED_TRANSITIONS.get(current.status)
            if allowed is None or status not in allowed:
                raise ExecutionConflict(
                    f"invalid execution transition {current.status!r} -> {status!r}"
                )
            values["status"] = status
        if output is not _MISSING:
            values["output_json"] = _json(output)
        if provenance is not _MISSING:
            values["provenance_json"] = _json(provenance)
        if trust_label is not None:
            values["trust_label"] = trust_label
        if finished_at is not _MISSING:
            values["finished_at"] = finished_at
        if values:
            assignments = ", ".join(f"{name}=?" for name in values)
            cursor = connection.execute(
                f"UPDATE notebook_executions SET {assignments}, revision=revision+1 "
                "WHERE execution_id=? AND revision=?",
                (*values.values(), execution_id, current.revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    f"execution {execution_id!r} changed concurrently"
                )
        return _execution(
            connection.execute(
                "SELECT * FROM notebook_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        )

    async def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        def read(connection):
            row = connection.execute(
                "SELECT * FROM notebook_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            return _execution(row) if row is not None else None

        return await self._db.arun(read)

    async def list_executions(
        self,
        notebook_id: str,
        session_epoch: int = 0,
    ) -> list[ExecutionRecord]:
        return await self._db.arun(
            lambda connection: [
                _execution(row)
                for row in connection.execute(
                    "SELECT * FROM notebook_executions "
                    "WHERE notebook_id=? AND session_epoch=? ORDER BY created_at",
                    (notebook_id, session_epoch),
                ).fetchall()
            ]
        )
