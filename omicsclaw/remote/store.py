"""The local knowledge: probed hosts and job handles, in SQLite.

The persistence half of C1/C2. Two tables live in
:mod:`omicsclaw.memory.database`'s schema (created by every
:class:`~omicsclaw.memory.database.Database`, which is this codebase's
migration mechanism — ``CREATE TABLE IF NOT EXISTS`` on open):

* ``remote_hosts`` — one row per alias: the probe's JSON, when it ran,
  and a JSON list of notes (the answers ``ask_about_host`` collected).
  This is the "host knowledge base" the plan names: the resource card a
  probe produces plus the facts only the user knows (partition, account,
  module activation), kept together so the next session starts from
  both.
* ``remote_jobs`` — one row per submitted job: the durable handle
  (:class:`~omicsclaw.remote.jobs.RemoteJobHandle` flattened), the
  last-observed status, and ``local_job_id`` linking a job the P1 plane
  started to its handle row. This is what survives a backend restart:
  the reconciler walks the in-flight rows and re-estimates them, so
  "close the laptop, the job keeps running" needs no daemon on either
  side.

**Rows are never deleted on doubt.** A host that cannot be reached
moves its jobs to ``unknown``; a job that left no trace stays
``unknown``; only an explicit terminal answer moves a row to ``done`` /
``failed`` / ``canceled``. The plan is explicit that an unreachable host
must not cost the handle, and the cheap way to guarantee that is to
have no code path that drops rows on error at all.

Both stores are synchronous like their neighbours in
``entry/desktop``: they are reached from one event loop via SQLite's
own busy handling, and the callers that can block off-loop use
``asyncio.to_thread`` themselves.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from omicsclaw.memory.database import Database

from .jobs import IN_FLIGHT_STATES, RemoteJobHandle

_JOB_COLUMNS = (
    "id,host_alias,kind,job_id,pgid,workdir,submitted_at,last_seen,status,"
    "local_job_id,target_alias,target_workdir,capabilities_json,source_request_id,"
    "payload_hash,proof_json"
)


@dataclass(frozen=True, slots=True)
class HostRecord:
    """One ``remote_hosts`` row."""

    alias: str
    probed_json: str = "{}"
    last_probed_at: float = 0.0
    notes_json: str = "[]"

    def probe_dict(self) -> dict[str, Any]:
        try:
            decoded = json.loads(self.probed_json)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def notes(self) -> list[dict[str, Any]]:
        try:
            decoded = json.loads(self.notes_json)
        except ValueError:
            return []
        return decoded if isinstance(decoded, list) else []


@dataclass(frozen=True, slots=True)
class RemoteJobRow:
    """One ``remote_jobs`` row — a handle plus what we last learned."""

    id: int
    host_alias: str
    kind: str
    job_id: str | None
    pgid: int | None
    workdir: str
    submitted_at: float
    last_seen: float | None
    status: str
    local_job_id: str = ""
    target_alias: str = ""
    target_workdir: str = ""
    capabilities: tuple[str, ...] = ()
    source_request_id: str = ""
    payload_hash: str = ""
    proof_json: str = "{}"

    def handle(self) -> RemoteJobHandle:
        proof = json.loads(self.proof_json)
        if not isinstance(proof, dict) or set(proof) - {"process_identity", "cluster"}:
            raise ValueError("invalid_handle_proof")
        return RemoteJobHandle(
            kind=self.kind,
            host=self.host_alias,
            workdir=self.workdir,
            job_id=self.job_id,
            pgid=self.pgid,
            submitted_at=self.submitted_at,
            local_job_id=self.local_job_id,
            target_alias=self.target_alias or self.host_alias,
            target_workdir=self.target_workdir or self.workdir,
            capabilities=self.capabilities,
            process_identity=proof.get("process_identity"),
            cluster=str(proof.get("cluster") or ""),
            request_id=self.source_request_id,
            payload_hash=self.payload_hash,
        )


class RemoteHostStore:
    """Reads and writes ``remote_hosts`` over one :class:`Database`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def save_probe(self, alias: str, probed_json: str) -> None:
        """Insert or refresh the probe of *alias*, keeping existing notes."""
        now = time.time()
        self._db.run(
            lambda conn: conn.execute(
                "INSERT INTO remote_hosts (alias, probed_json, last_probed_at,"
                " notes_json) VALUES (?, ?, ?, '[]')"
                " ON CONFLICT(alias) DO UPDATE SET probed_json = excluded.probed_json,"
                " last_probed_at = excluded.last_probed_at",
                (alias, probed_json, now),
            )
        )

    def get(self, alias: str) -> HostRecord | None:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT alias, probed_json, last_probed_at, notes_json"
                " FROM remote_hosts WHERE alias = ?",
                (alias,),
            ).fetchall()
        )
        return self._record(rows[0]) if rows else None

    def list_records(self) -> list[HostRecord]:
        """Every host row, alias-ordered — the REST face's list view."""
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT alias, probed_json, last_probed_at, notes_json"
                " FROM remote_hosts ORDER BY alias"
            ).fetchall()
        )
        return [self._record(row) for row in rows]

    def delete(self, alias: str) -> bool:
        """Forget one host. Whether a row was actually removed.

        The row-leaving call of this store, and it is this explicit one
        only: probes overwrite, notes append, and jobs never touch host
        rows — "removed a host" is a user action, not a doubt the store
        resolved on its own.
        """
        cursor = self._db.run(
            lambda conn: conn.execute(
                "DELETE FROM remote_hosts WHERE alias = ?", (alias,)
            )
        )
        return bool(getattr(cursor, "rowcount", 0))

    @staticmethod
    def _record(row: Any) -> HostRecord:
        return HostRecord(
            alias=row["alias"],
            probed_json=row["probed_json"],
            last_probed_at=row["last_probed_at"],
            notes_json=row["notes_json"],
        )

    def list_aliases(self) -> list[str]:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT alias FROM remote_hosts ORDER BY alias"
            ).fetchall()
        )
        return [row["alias"] for row in rows]

    def add_note(self, alias: str, question: str, answer: str) -> None:
        """Append one answered question to *alias*'s notes.

        A no-op that still upserts the row when the alias was never
        probed: the first fact about a host may well arrive before the
        first probe (the user names the partition before we can reach
        the scheduler), and losing it because no probe had run yet is
        the kind of silent drop this store exists to prevent.
        """
        record = self.get(alias)
        notes = record.notes() if record is not None else []
        notes.append({"question": question, "answer": answer, "at": time.time()})
        payload = json.dumps(notes, ensure_ascii=False)
        now = time.time()
        self._db.run(
            lambda conn: conn.execute(
                "INSERT INTO remote_hosts (alias, probed_json, last_probed_at,"
                " notes_json) VALUES (?, '{}', ?, ?)"
                " ON CONFLICT(alias) DO UPDATE SET notes_json = excluded.notes_json",
                (alias, now, payload),
            )
        )


class RemoteJobStore:
    """Reads and writes ``remote_jobs`` over one :class:`Database`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def record(self, handle: RemoteJobHandle, *, status: str = "pending") -> int:
        """Persist a fresh handle; returns the row id that becomes the job_ref."""
        return self._db.run(lambda conn: self._insert_handle(conn, handle, status))

    @staticmethod
    def _insert_handle(conn, handle, status):
        cursor = conn.execute(
            "INSERT INTO remote_jobs (host_alias,kind,job_id,pgid,workdir,submitted_at,"
            "last_seen,status,local_job_id,target_alias,target_workdir,capabilities_json,"
            "source_request_id,payload_hash,proof_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                handle.host,
                handle.kind,
                handle.job_id,
                handle.pgid,
                handle.workdir,
                handle.submitted_at,
                time.time(),
                status,
                handle.local_job_id,
                handle.target_alias,
                handle.target_workdir,
                json.dumps(list(handle.capabilities), ensure_ascii=False),
                handle.request_id,
                handle.payload_hash,
                json.dumps(
                    {
                        "process_identity": dict(handle.process_identity)
                        if handle.process_identity
                        else None,
                        "cluster": handle.cluster,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        return int(cursor.lastrowid or 0)

    def claim_submission(
        self, request_id: str, payload_hash: str, handle: RemoteJobHandle
    ) -> RemoteJobRow:
        """Persist an unlaunched intent; duplicate callers only observe its row."""
        if (
            not re.fullmatch(r"[A-Za-z0-9:._-]{1,256}", request_id)
            or not re.fullmatch(r"[0-9a-f]{64}", payload_hash)
            or handle.pgid is not None
            or handle.job_id is not None
        ):
            raise ValueError("invalid_submission_claim")

        def write(conn):
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT "
                + _JOB_COLUMNS
                + " FROM remote_jobs WHERE source_request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                from dataclasses import replace

                identifier = self._insert_handle(
                    conn,
                    replace(handle, request_id=request_id, payload_hash=payload_hash),
                    "unknown",
                )
                row = conn.execute(
                    "SELECT " + _JOB_COLUMNS + " FROM remote_jobs WHERE id=?",
                    (identifier,),
                ).fetchone()
            current = self._row(row)
            if current.payload_hash != payload_hash or (
                current.host_alias,
                current.kind,
                current.workdir,
                current.local_job_id,
                current.target_alias,
                current.target_workdir,
                current.capabilities,
            ) != (
                handle.host,
                handle.kind,
                handle.workdir,
                handle.local_job_id,
                handle.target_alias,
                handle.target_workdir,
                handle.capabilities,
            ):
                raise ValueError("submission_payload_conflict")
            return current

        return self._db.run(write)

    def get(self, job_ref: str) -> RemoteJobRow | None:
        key = job_ref.strip()
        if not key.isdigit():
            return None
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT " + _JOB_COLUMNS + " FROM remote_jobs WHERE id = ?",
                (int(key),),
            ).fetchall()
        )
        return self._row(rows[0]) if rows else None

    def by_local_job(self, local_job_id: str) -> RemoteJobRow | None:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT "
                + _JOB_COLUMNS
                + " FROM remote_jobs WHERE local_job_id = ? ORDER BY id DESC"
                " LIMIT 1",
                (local_job_id,),
            ).fetchall()
        )
        return self._row(rows[0]) if rows else None

    def in_flight(self, *, include_unknown: bool = False) -> list[RemoteJobRow]:
        """Every row not yet terminal — the reconciler's work list."""
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT " + _JOB_COLUMNS + " FROM remote_jobs WHERE status IN (?, ?, ?)"
                " ORDER BY submitted_at",
                (*IN_FLIGHT_STATES, "unknown" if include_unknown else "pending"),
            ).fetchall()
        )
        return [self._row(row) for row in rows]

    def update_status(
        self, row_id: int, status: str, *, last_seen: float | None = None
    ) -> None:
        """Record what the host said; never deletes, whatever *status* is."""
        self._db.run(
            lambda conn: conn.execute(
                "UPDATE remote_jobs SET status = ?, last_seen = ? WHERE id = ?",
                (status, time.time() if last_seen is None else last_seen, row_id),
            )
        )

    @staticmethod
    def _row(row: Any) -> RemoteJobRow:
        return RemoteJobRow(
            id=row["id"],
            host_alias=row["host_alias"],
            kind=row["kind"],
            job_id=row["job_id"],
            pgid=row["pgid"],
            workdir=row["workdir"],
            submitted_at=row["submitted_at"],
            last_seen=row["last_seen"],
            status=row["status"],
            local_job_id=row["local_job_id"] or "",
            target_alias=row["target_alias"] or row["host_alias"],
            target_workdir=row["target_workdir"] or row["workdir"],
            capabilities=tuple(json.loads(row["capabilities_json"] or "[]")),
            source_request_id=row["source_request_id"] or "",
            payload_hash=row["payload_hash"] or "",
            proof_json=row["proof_json"] or "{}",
        )


__all__ = [
    "HostRecord",
    "RemoteHostStore",
    "RemoteJobRow",
    "RemoteJobStore",
]
