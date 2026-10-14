"""SQLite connection management and schema for the memory layer.

One :class:`Database` owns one connection and serialises every statement
through a lock, so callers may reach it from any thread — which is what
``asyncio.to_thread`` does on their behalf.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Self, TypeVar

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_authority_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    authority_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_ingress (
    session_id TEXT NOT NULL,
    source_request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, source_request_id)
);

CREATE INDEX IF NOT EXISTS idx_turn_ingress_history ON turn_ingress(session_id,created_at,source_request_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    anchors     TEXT NOT NULL DEFAULT '',
    values_json TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    reasoning   TEXT NOT NULL DEFAULT '',
    tool_calls  TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    is_error    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions (session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages (session_id, position);

CREATE TABLE IF NOT EXISTS long_term_memories (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '',
    importance   INTEGER NOT NULL DEFAULT 0,
    signature    TEXT UNIQUE,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_used_at REAL,
    use_count    INTEGER NOT NULL DEFAULT 0,
    ttl_days     INTEGER,
    disabled     INTEGER NOT NULL DEFAULT 0,
    tags         TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(id UNINDEXED, title, content);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'skill_run',
    skill       TEXT NOT NULL DEFAULT '',
    inputs_json TEXT NOT NULL DEFAULT '{}',
    runtime     TEXT NOT NULL DEFAULT 'local',
    lifetime    TEXT NOT NULL DEFAULT '',
    target_alias TEXT NOT NULL DEFAULT '',
    target_workdir TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'queued',
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_session
    ON jobs (session_id, created_at);

CREATE TABLE IF NOT EXISTS target_migration_state (
    scope TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at REAL,
    completed_at REAL,
    error_code TEXT NOT NULL DEFAULT '',
    unresolved_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS target_migration_unresolved (
    scope TEXT NOT NULL,
    record_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (scope, record_key)
);

CREATE TABLE IF NOT EXISTS job_events (
    job_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    PRIMARY KEY (job_id, seq),
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turn_stream_owners (
    stream_id   TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    epoch       INTEGER NOT NULL,
    lease_until REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_events (
    stream_id    TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    turn_id      TEXT NOT NULL DEFAULT '',
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    PRIMARY KEY (stream_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_turn_events_stream_seq
    ON turn_events (stream_id, seq);

CREATE TABLE IF NOT EXISTS turn_projection_checkpoints (
    stream_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    authority_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    owner_generation INTEGER NOT NULL,
    receipt_hash TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    last_subframe INTEGER NOT NULL,
    source_digest TEXT NOT NULL,
    frame_digest TEXT NOT NULL,
    frames_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    job_id          TEXT,
    session_id      TEXT,
    kind            TEXT NOT NULL DEFAULT 'other',
    path            TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    mime            TEXT,
    thumb_path      TEXT,
    sha256          TEXT NOT NULL DEFAULT '',
    size            INTEGER NOT NULL DEFAULT 0,
    produced_by     TEXT,
    parent_ids_json TEXT,
    meta_json       TEXT,
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job
    ON artifacts (job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_session
    ON artifacts (session_id, created_at);

-- UNIQUE is the database-level half of scanner idempotency: the
-- checked insert below guards one Database instance, and this index
-- guards the case of two connections (the job-plane store and the
-- save_artifact sink) racing on the same (job_id, path).
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_job_path
    ON artifacts (job_id, path);

CREATE TABLE IF NOT EXISTS remote_hosts (
    alias         TEXT PRIMARY KEY,
    probed_json   TEXT NOT NULL DEFAULT '{}',
    last_probed_at REAL NOT NULL DEFAULT 0,
    notes_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS remote_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    host_alias   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    job_id       TEXT,
    pgid         INTEGER,
    workdir      TEXT NOT NULL,
    submitted_at REAL NOT NULL,
    last_seen    REAL,
    status       TEXT NOT NULL DEFAULT 'pending',
    local_job_id TEXT NOT NULL DEFAULT '',
    target_alias TEXT NOT NULL DEFAULT '',
    target_workdir TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    source_request_id TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL DEFAULT '',
    proof_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_remote_jobs_local
    ON remote_jobs (local_job_id);

CREATE TABLE IF NOT EXISTS compaction_archives (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,   -- which compaction of the session
    position     INTEGER NOT NULL,   -- index in the history before compaction
    role         TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    tool_calls   TEXT NOT NULL DEFAULT '',   -- JSON as written, byte-preserved
    tool_call_id TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    created_at   REAL NOT NULL
);

-- Content-level dedup, not batch-level: a retried compaction replays the
-- same messages, and (session_id, content_hash) folds the replay away
-- where a (session, seq) key would not. seq/position locate, they do not
-- identify.
CREATE UNIQUE INDEX IF NOT EXISTS idx_compaction_archives_dedup
    ON compaction_archives (session_id, content_hash);

CREATE INDEX IF NOT EXISTS idx_compaction_archives_session
    ON compaction_archives (session_id, seq, position);

CREATE VIRTUAL TABLE IF NOT EXISTS compaction_archive_fts
    USING fts5(id UNINDEXED, content);

CREATE TABLE IF NOT EXISTS session_scratch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'tried_solutions',
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- The same content-level dedup as the archive: merging the same anchor
-- text again writes nothing rather than a second copy.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scratch_dedup
    ON session_scratch (session_id, kind, content);

CREATE TABLE IF NOT EXISTS notebook_cells (
    notebook_id TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',
    output_json TEXT NOT NULL DEFAULT 'null',
    provenance_json TEXT NOT NULL DEFAULT 'null',
    trust_label TEXT NOT NULL DEFAULT '',
    client_command_id TEXT NOT NULL DEFAULT '',
    execution_id TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (notebook_id, cell_id, session_epoch)
);

CREATE INDEX IF NOT EXISTS idx_notebook_cells_notebook
    ON notebook_cells (notebook_id, session_epoch, cell_id);

CREATE TABLE IF NOT EXISTS notebook_executions (
    execution_id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    client_command_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    output_json TEXT NOT NULL DEFAULT 'null',
    provenance_json TEXT NOT NULL DEFAULT 'null',
    trust_label TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    finished_at REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notebook_execution_command
    ON notebook_executions (
        client_command_id, session_epoch, notebook_id, cell_id
    )
    WHERE client_command_id <> '';
"""
"""The schema of every :class:`Database`, on open, forever additive.

``artifacts`` is P2's results tray (one row per registered artifact,
its kind and checksum, and the ``(job_id, path)`` uniqueness the
scanner's idempotency leans on). ``remote_hosts`` and ``remote_jobs``
are the remote execution plane's (C1/C2): one row per probed SSH alias
— its probe JSON, when, and the answers ``ask_about_host`` collected —
and one row per submitted remote job, the durable handle a restarted
backend re-estimates from. The ``jobs.runtime`` column (``local`` |
``remote:<alias>``) is carried by the same additive rule: new files get
it from this script, and old ones get it from :func:`_migrate`, the
first column addition this schema has needed — ``CREATE TABLE IF NOT
EXISTS`` creates, it does not alter.

``compaction_archives`` is the compaction plane's read-back half: one
row per message a compaction removed, session-scoped and deleted with
its session, searchable through ``compaction_archive_fts`` the way
``memories_fts`` covers ``long_term_memories``. No foreign key on
purpose, the ``jobs``/``remote_jobs`` precedent: archive rows are
written from a compaction, which may run in a deployment whose
sessions never save, and an FK would refuse exactly that write.

``session_scratch`` is the session-domain draft store: one row per line
of the ``tried_solutions`` anchor a written-back compaction recorded, so
the dead ends stay findable after an emergency truncation drops the
compaction message that used to carry them. Session-scoped and deleted
with its session, no foreign key for the same reason as the archive."""


BUSY_TIMEOUT_S = 15.0
"""How long a statement waits for another connection to let go."""


PRIVATE_FILE_MODE = 0o600
"""Mode of a database file this class creates, and of its ``-wal`` and ``-shm`` files."""


class Database:
    """A SQLite connection shared safely across threads.

    A database file that does not exist yet is created with mode 0600, and
    its ``-wal`` and ``-shm`` files get the same mode. The mode of an
    existing file is left alone.

    :param path: File to open, or ``":memory:"`` for a transient database.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        created = False
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            created = _create_private(self.path)
        # check_same_thread=False plus the lock below: one connection
        # reached from many worker threads, rather than one connection per
        # thread, which would give ":memory:" a separate empty database per
        # thread.
        # ``timeout`` is the busy timeout: a statement blocked by another
        # connection waits this long before giving up, which is what makes
        # several processes on one file workable.
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=BUSY_TIMEOUT_S
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            # Switching the journal mode needs the database to itself, and
            # SQLite answers SQLITE_BUSY without consulting the busy
            # handler rather than waiting. Another process opening the same
            # file at the same moment is therefore expected, not an error:
            # whichever connection gets there first converts the file, and
            # the mode is a property of the file, not of this connection.
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            _migrate(self._conn)
        if created:
            for suffix in ("-wal", "-shm"):
                try:
                    os.chmod(self.path + suffix, PRIVATE_FILE_MODE)
                except FileNotFoundError:
                    pass

    def run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Call *work* with the connection held, committing on success.

        :param work: Receives the connection; its return value is passed
            through.
        :returns: Whatever *work* returned.
        :raises sqlite3.Error: Propagated after the transaction is rolled
            back.
        """
        with self._lock:
            try:
                result = work(self._conn)
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()
            return result

    async def arun(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Await :meth:`run` on a worker thread.

        :param work: Receives the connection; its return value is passed
            through.
        :returns: Whatever *work* returned.
        """
        return await asyncio.to_thread(self.run, work)

    def close(self) -> None:
        """Close the connection. Calling this twice is harmless."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive columns to databases created by older releases."""
    ingress_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(turn_ingress)")
    }
    if "payload_json" not in ingress_columns:
        try:
            conn.execute(
                "ALTER TABLE turn_ingress ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}' "
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "runtime" not in columns:
        try:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN runtime TEXT NOT NULL DEFAULT 'local'"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    for table, additions in {
        "jobs": {
            "lifetime": "TEXT NOT NULL DEFAULT ''",
            "target_alias": "TEXT NOT NULL DEFAULT ''",
            "target_workdir": "TEXT NOT NULL DEFAULT ''",
            "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
        },
        "remote_jobs": {
            "source_request_id": "TEXT NOT NULL DEFAULT ''",
            "payload_hash": "TEXT NOT NULL DEFAULT ''",
            "proof_json": "TEXT NOT NULL DEFAULT '{}'",
            "target_alias": "TEXT NOT NULL DEFAULT ''",
            "target_workdir": "TEXT NOT NULL DEFAULT ''",
            "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    }.items():
        columns = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
        for name, declaration in additions.items():
            if name in columns:
                continue
            try:
                conn.execute(
                    "ALTER TABLE " + table + " ADD COLUMN " + name + " " + declaration
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    # Row backfills belong to migrate_remote_targets; dry-run reports
    # candidates without mutating user data.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_job_request "
        "ON remote_jobs(source_request_id) WHERE source_request_id != ''"
    )
    conn.commit()


def _create_private(path: str) -> bool:
    """Create an empty file at *path* with mode 0600 unless one exists.

    :returns: ``True`` when this call created the file.
    """
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE
        )
    except FileExistsError:
        return False
    os.close(descriptor)
    return True
