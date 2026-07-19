"""Canonical, Turn-attributed Transcript Module.

The Module owns immutable provider-visible entries, an explicit active view,
and the terminal-candidate state machine used by the conversational control
plane.  It is deliberately a separate SQLite database from ``control.db``.
Surfaces never open this database directly; the Query Engine receives a
Turn-bound :class:`TurnTranscriptAdapter` instead.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from .transcript import sanitize_tool_history
from .transcript_db import dumps_message


_MIGRATION_1_SQL = r"""
CREATE TABLE transcript_schema_migrations (
    version             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    checksum_sha256     TEXT NOT NULL,
    applied_at_ms       INTEGER NOT NULL
) STRICT;

CREATE TABLE transcript_conversations (
    conversation_id     TEXT PRIMARY KEY,
    view_revision       INTEGER NOT NULL DEFAULT 0 CHECK (view_revision >= 0),
    updated_at_ms       INTEGER NOT NULL
) STRICT;

CREATE TABLE transcript_entries (
    entry_id             TEXT PRIMARY KEY,
    conversation_id      TEXT NOT NULL
                              REFERENCES transcript_conversations(conversation_id)
                              ON DELETE RESTRICT,
    turn_id              TEXT NULL,
    entry_kind           TEXT NOT NULL CHECK (entry_kind IN
                              ('provider_message','compaction_summary','terminal_message')),
    payload_json         TEXT NOT NULL,
    content_sha256       TEXT NOT NULL,
    commit_state         TEXT NOT NULL CHECK (commit_state IN
                              ('committed','terminal_candidate','abandoned')),
    created_at_ms        INTEGER NOT NULL,
    UNIQUE (conversation_id, entry_id)
) STRICT;

CREATE INDEX transcript_entries_conversation_turn_created_idx
    ON transcript_entries(conversation_id, turn_id, created_at_ms, entry_id);

CREATE UNIQUE INDEX transcript_one_live_terminal_per_turn
    ON transcript_entries(turn_id)
    WHERE turn_id IS NOT NULL
      AND entry_kind = 'terminal_message'
      AND commit_state IN ('terminal_candidate','committed');

CREATE TABLE transcript_active_order (
    conversation_id      TEXT NOT NULL
                              REFERENCES transcript_conversations(conversation_id)
                              ON DELETE RESTRICT,
    seq                  INTEGER NOT NULL CHECK (seq >= 0),
    entry_id             TEXT NOT NULL UNIQUE,
    PRIMARY KEY (conversation_id, seq),
    FOREIGN KEY (conversation_id, entry_id)
        REFERENCES transcript_entries(conversation_id, entry_id)
        ON DELETE RESTRICT
) STRICT;

CREATE TABLE transcript_import_runs (
    source_key_sha256       TEXT PRIMARY KEY,
    source_manifest_sha256  TEXT NOT NULL UNIQUE,
    source_path             TEXT NOT NULL,
    state                   TEXT NOT NULL CHECK (state IN ('committed')),
    conversation_count      INTEGER NOT NULL CHECK (conversation_count >= 0),
    entry_count             INTEGER NOT NULL CHECK (entry_count >= 0),
    backup_path             TEXT NOT NULL,
    committed_at_ms         INTEGER NOT NULL,
    cutover_at_ms           INTEGER NOT NULL
) STRICT;

CREATE TRIGGER transcript_entries_immutable
BEFORE UPDATE ON transcript_entries
WHEN NEW.entry_id != OLD.entry_id
  OR NEW.conversation_id != OLD.conversation_id
  OR OLD.turn_id IS NOT NEW.turn_id
  OR NEW.entry_kind != OLD.entry_kind
  OR NEW.payload_json != OLD.payload_json
  OR NEW.content_sha256 != OLD.content_sha256
  OR NEW.created_at_ms != OLD.created_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable transcript entry field');
END;

CREATE TRIGGER transcript_entry_state_transition
BEFORE UPDATE OF commit_state ON transcript_entries
WHEN NOT (
    OLD.commit_state = 'terminal_candidate'
    AND NEW.commit_state IN ('committed','abandoned')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid transcript entry state transition');
END;

CREATE TRIGGER transcript_active_requires_committed
BEFORE INSERT ON transcript_active_order
WHEN NOT EXISTS (
    SELECT 1 FROM transcript_entries AS e
    WHERE e.entry_id = NEW.entry_id
      AND e.conversation_id = NEW.conversation_id
      AND e.commit_state = 'committed'
)
BEGIN
  SELECT RAISE(ABORT, 'active transcript entry must be committed');
END;
"""

# Pinned after the migration text is finalized.  Runtime recomputes it before
# opening any database, so accidental edits fail closed instead of silently
# changing historical schema.
_MIGRATION_1_SHA256 = (
    "908a6fe4d5a6f283b80d9bfb3bb66eebb910eb7ae559bef56a167f423be08f85"
)

_MIGRATION_2_SQL = r"""
CREATE TABLE transcript_store_identity (
    singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_id               TEXT NOT NULL UNIQUE
) STRICT;

INSERT INTO transcript_store_identity (singleton, store_id)
VALUES (1, lower(hex(randomblob(16))));

CREATE TABLE transcript_cutover_identities (
    import_run_id             TEXT PRIMARY KEY,
    cutover_manifest_sha256   TEXT NOT NULL UNIQUE,
    source_identity           TEXT NOT NULL,
    source_key_sha256         TEXT NOT NULL UNIQUE
                                  REFERENCES transcript_import_runs(source_key_sha256)
                                  ON DELETE RESTRICT,
    source_manifest_sha256    TEXT NOT NULL,
    import_baseline_sha256    TEXT NOT NULL,
    created_at_ms             INTEGER NOT NULL
) STRICT;

CREATE TABLE transcript_import_conversations (
    import_run_id             TEXT NOT NULL
                                  REFERENCES transcript_cutover_identities(import_run_id)
                                  ON DELETE RESTRICT,
    legacy_key                TEXT NOT NULL,
    conversation_id           TEXT NOT NULL
                                  REFERENCES transcript_conversations(conversation_id)
                                  ON DELETE RESTRICT,
    initial_message_count     INTEGER NOT NULL CHECK (initial_message_count >= 0),
    PRIMARY KEY (import_run_id, legacy_key),
    UNIQUE (import_run_id, conversation_id)
) STRICT;

CREATE TABLE transcript_import_entries (
    import_run_id             TEXT NOT NULL,
    legacy_key                TEXT NOT NULL,
    source_seq                INTEGER NOT NULL CHECK (source_seq >= 0),
    conversation_id           TEXT NOT NULL,
    entry_id                  TEXT NOT NULL,
    content_sha256            TEXT NOT NULL,
    payload_json              TEXT NOT NULL,
    PRIMARY KEY (import_run_id, legacy_key, source_seq),
    UNIQUE (import_run_id, entry_id),
    FOREIGN KEY (import_run_id, legacy_key)
        REFERENCES transcript_import_conversations(import_run_id, legacy_key)
        ON DELETE RESTRICT,
    FOREIGN KEY (conversation_id, entry_id)
        REFERENCES transcript_entries(conversation_id, entry_id)
        ON DELETE RESTRICT
) STRICT;

CREATE TRIGGER transcript_import_runs_immutable_update
BEFORE UPDATE ON transcript_import_runs
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript import run');
END;

CREATE TRIGGER transcript_store_identity_immutable_update
BEFORE UPDATE ON transcript_store_identity
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript Store identity');
END;

CREATE TRIGGER transcript_store_identity_immutable_delete
BEFORE DELETE ON transcript_store_identity
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript Store identity');
END;

CREATE TRIGGER transcript_import_runs_immutable_delete
BEFORE DELETE ON transcript_import_runs
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript import run');
END;

CREATE TRIGGER transcript_cutover_identities_immutable_update
BEFORE UPDATE ON transcript_cutover_identities
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript cutover identity');
END;

CREATE TRIGGER transcript_cutover_identities_immutable_delete
BEFORE DELETE ON transcript_cutover_identities
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript cutover identity');
END;

CREATE TRIGGER transcript_import_conversations_immutable_update
BEFORE UPDATE ON transcript_import_conversations
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript import baseline');
END;

CREATE TRIGGER transcript_import_conversations_immutable_delete
BEFORE DELETE ON transcript_import_conversations
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript import baseline');
END;

CREATE TRIGGER transcript_import_entries_immutable_update
BEFORE UPDATE ON transcript_import_entries
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript import baseline');
END;

CREATE TRIGGER transcript_import_entries_immutable_delete
BEFORE DELETE ON transcript_import_entries
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript import baseline');
END;
"""

_MIGRATION_2_SHA256 = (
    "21314717608f407fe90a9fecace9451f9e763d29f113d63b011355d4180d8059"
)

_TRANSCRIPT_MIGRATIONS = (
    (
        1,
        "initial_canonical_transcript",
        _MIGRATION_1_SQL,
        _MIGRATION_1_SHA256,
    ),
    (
        2,
        "bind_import_cutover_identity",
        _MIGRATION_2_SQL,
        _MIGRATION_2_SHA256,
    ),
)

_MIGRATION_REQUIRED_TABLES = {
    1: frozenset(
        {
            "transcript_schema_migrations",
            "transcript_conversations",
            "transcript_entries",
            "transcript_active_order",
            "transcript_import_runs",
        }
    ),
    2: frozenset(
        {
            "transcript_store_identity",
            "transcript_cutover_identities",
            "transcript_import_conversations",
            "transcript_import_entries",
        }
    ),
}


class TranscriptIntegrityError(RuntimeError):
    """Canonical Transcript invariants or migration history were violated."""


class TranscriptImportConflict(RuntimeError):
    """A one-shot import cannot be applied without guessing or merging."""


@dataclass(frozen=True, slots=True)
class TranscriptEntryRef:
    entry_id: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    entry_id: str
    conversation_id: str
    turn_id: str | None
    entry_kind: str
    payload: Mapping[str, Any]
    content_sha256: str
    commit_state: str
    created_at_ms: int

    @property
    def public_text(self) -> str:
        if self.entry_kind != "terminal_message":
            return ""
        return str(self.payload.get("public_text", "") or "")


@dataclass(frozen=True, slots=True)
class LegacyTranscriptImportReport:
    state: str
    source_manifest_sha256: str
    conversation_count: int
    entry_count: int
    backup_path: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptCutoverIdentity:
    import_run_id: str
    cutover_manifest_sha256: str
    transcript_store_id: str
    import_baseline_sha256: str
    source_identity: str


@dataclass(frozen=True, slots=True)
class LegacyTranscriptStream:
    chat_key: str
    chat_id_json: str
    message_count: int


@dataclass(frozen=True, slots=True)
class _LegacyMessage:
    chat_key: str
    conversation_id: str
    chat_id_json: str
    seq: int
    payload_json: str
    payload: Mapping[str, Any]


def _default_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _new_entry_id() -> str:
    return secrets.token_hex(16)


def _digest(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _require_lower_hex(value: str, *, length: int, name: str) -> str:
    normalized = str(value)
    if len(normalized) != length or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be {length} lowercase hexadecimal characters")
    return normalized


def _import_baseline_digest(
    *,
    import_run_id: str,
    source_manifest_sha256: str,
    conversations: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "version": 1,
        "import_run_id": import_run_id,
        "source_manifest_sha256": source_manifest_sha256,
        "conversations": [dict(value) for value in conversations],
        "entries": [dict(value) for value in entries],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_migration_marker_rows(
    rows: Sequence[sqlite3.Row],
) -> tuple[int, ...]:
    versions = tuple(int(row["version"]) for row in rows)
    if not versions or versions != tuple(range(1, len(versions) + 1)) or any(
        version > len(_TRANSCRIPT_MIGRATIONS) for version in versions
    ):
        raise TranscriptIntegrityError(
            "Transcript database has unsupported migration versions"
        )
    for row in rows:
        version = int(row["version"])
        _, expected_name, _sql, expected_checksum = _TRANSCRIPT_MIGRATIONS[
            version - 1
        ]
        if (
            str(row["name"]) != expected_name
            or str(row["checksum_sha256"]) != expected_checksum
        ):
            raise TranscriptIntegrityError(
                f"Transcript migration checksum mismatch for version {version}"
            )
    return versions


def _terminal_payload(
    *,
    provider_message: Mapping[str, Any] | None,
    public_text: str,
    terminal_kind: str,
) -> dict[str, Any]:
    return {
        "provider_message": (
            dict(provider_message) if provider_message is not None else None
        ),
        "public_text": str(public_text),
        "terminal_kind": str(terminal_kind),
    }


def _provider_message(entry_kind: str, payload: Mapping[str, Any]) -> dict | None:
    if entry_kind == "terminal_message":
        value = payload.get("provider_message")
        return dict(value) if isinstance(value, Mapping) else None
    return dict(payload)


class CanonicalTranscript:
    """Deep Interface over the canonical ``transcripts.db`` authority."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        require_existing: bool = False,
        busy_timeout_ms: int = 5_000,
        clock_ms: Callable[[], int] = _default_clock_ms,
        sanitizer: Callable[[list[dict], bool], list[dict]] = sanitize_tool_history,
    ) -> None:
        self.state_root = Path(state_root).expanduser().absolute()
        if self.state_root.is_symlink():
            raise TranscriptIntegrityError("Transcript state root must not be a symlink")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root = self.state_root.resolve()
        self.database_path = self.state_root / "transcripts.db"
        if self.database_path.is_symlink():
            raise TranscriptIntegrityError("Transcript database must not be a symlink")
        database_existed = self.database_path.exists()
        if require_existing and not database_existed:
            raise TranscriptIntegrityError(
                "canonical transcripts.db is missing for existing Control state"
            )
        if database_existed:
            if not self.database_path.is_file():
                raise TranscriptIntegrityError(
                    "Transcript database path is not a regular file"
                )
            if self.database_path.stat().st_size == 0:
                raise TranscriptIntegrityError(
                    "existing Transcript database has no canonical migration marker"
                )
            preflight: sqlite3.Connection | None = None
            try:
                preflight = sqlite3.connect(
                    f"file:{self.database_path}?mode=ro",
                    uri=True,
                )
                preflight.row_factory = sqlite3.Row
                marker = preflight.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='transcript_schema_migrations'"
                ).fetchone()
                if marker is None:
                    raise TranscriptIntegrityError(
                        "existing Transcript database has no canonical migration marker"
                    )
                marker_rows = preflight.execute(
                    "SELECT version, name, checksum_sha256 "
                    "FROM transcript_schema_migrations ORDER BY version"
                ).fetchall()
                versions = _validate_migration_marker_rows(marker_rows)
                tables = {
                    str(row[0])
                    for row in preflight.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                required = set().union(
                    *(_MIGRATION_REQUIRED_TABLES[version] for version in versions)
                )
                if not required.issubset(tables):
                    raise TranscriptIntegrityError(
                        "existing Transcript database has an incomplete canonical schema"
                    )
            except sqlite3.DatabaseError as exc:
                raise TranscriptIntegrityError(
                    "existing Transcript database cannot be validated read-only"
                ) from exc
            finally:
                if preflight is not None:
                    preflight.close()
        self._created_database = not database_existed
        self._clock_ms = clock_ms
        self.sanitizer = sanitizer
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database_path,
            isolation_level=None,
            check_same_thread=False,
            timeout=max(0.001, busy_timeout_ms / 1_000),
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._apply_migrations()
            self._harden_files()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("CanonicalTranscript is closed")
        return self._connection

    def _now(self) -> int:
        return int(self._clock_ms())

    def _harden_files(self) -> None:
        with suppress(OSError):
            os.chmod(self.state_root, 0o700)
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            if path.exists():
                with suppress(OSError):
                    os.chmod(path, 0o600)

    def _apply_migrations(self) -> None:
        for version, _name, sql, checksum in _TRANSCRIPT_MIGRATIONS:
            actual = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if actual != checksum:
                raise TranscriptIntegrityError(
                    "historical Transcript migration source checksum mismatch "
                    f"for version {version}"
                )
        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='transcript_schema_migrations'"
        ).fetchone()
        applied: list[sqlite3.Row] = []
        if table is not None:
            applied = self._conn.execute(
                "SELECT version, name, checksum_sha256 "
                "FROM transcript_schema_migrations ORDER BY version"
            ).fetchall()
        if table is None and not self._created_database:
            raise TranscriptIntegrityError(
                "existing Transcript database has no canonical migration marker"
            )
        if table is not None and not applied:
            raise TranscriptIntegrityError(
                "existing Transcript database has an incomplete migration marker"
            )
        if applied:
            _validate_migration_marker_rows(applied)

        applied_versions = {int(row["version"]) for row in applied}
        for version, name, sql, checksum in _TRANSCRIPT_MIGRATIONS:
            if version in applied_versions:
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{sql}\n"
                "INSERT INTO transcript_schema_migrations "
                "(version, name, checksum_sha256, applied_at_ms) VALUES "
                f"({version}, '{name}', '{checksum}', "
                f"{self._now()});\nCOMMIT;"
            )
            try:
                self._conn.executescript(script)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        tables = {
            str(row[0])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required_tables = set().union(*_MIGRATION_REQUIRED_TABLES.values())
        if not required_tables.issubset(tables):
            raise TranscriptIntegrityError(
                "Transcript database has an incomplete canonical schema"
            )
        self._assert_integrity()

    def _assert_integrity(self) -> None:
        result = self._conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise TranscriptIntegrityError(
                f"Transcript database integrity check failed: {result}"
            )
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise TranscriptIntegrityError(
                f"Transcript database foreign-key check failed: {violations[:5]}"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._conn
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
                self._harden_files()
            except BaseException:
                connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._harden_files()
            self._connection.close()
            self._closed = True

    def bind_turn(self, conversation_id: str, turn_id: str) -> "TurnTranscriptAdapter":
        conversation = str(conversation_id).strip()
        turn = str(turn_id).strip()
        if not conversation or not turn:
            raise ValueError("conversation_id and turn_id must be non-empty")
        return TurnTranscriptAdapter(self, conversation, turn)

    def _ensure_conversation(
        self, connection: sqlite3.Connection, conversation_id: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO transcript_conversations (
                conversation_id, view_revision, updated_at_ms
            ) VALUES (?, 0, ?)
            ON CONFLICT(conversation_id) DO NOTHING
            """,
            (conversation_id, self._now()),
        )

    def _next_seq(
        self, connection: sqlite3.Connection, conversation_id: str
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 "
            "FROM transcript_active_order WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return int(row[0])

    def append_message(
        self,
        conversation_id: str,
        turn_id: str,
        payload: Mapping[str, Any],
        *,
        entry_kind: str = "provider_message",
    ) -> TranscriptEntryRef:
        if entry_kind not in {"provider_message", "compaction_summary"}:
            raise ValueError("invalid committed Transcript entry kind")
        payload_json = dumps_message(dict(payload))
        digest = _digest(payload_json)
        entry_id = _new_entry_id()
        with self._transaction() as connection:
            self._ensure_conversation(connection, conversation_id)
            connection.execute(
                """
                INSERT INTO transcript_entries (
                    entry_id, conversation_id, turn_id, entry_kind, payload_json,
                    content_sha256, commit_state, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'committed', ?)
                """,
                (
                    entry_id,
                    conversation_id,
                    turn_id,
                    entry_kind,
                    payload_json,
                    digest,
                    self._now(),
                ),
            )
            connection.execute(
                "INSERT INTO transcript_active_order "
                "(conversation_id, seq, entry_id) VALUES (?, ?, ?)",
                (conversation_id, self._next_seq(connection, conversation_id), entry_id),
            )
            connection.execute(
                "UPDATE transcript_conversations SET view_revision = view_revision + 1, "
                "updated_at_ms = ? WHERE conversation_id = ?",
                (self._now(), conversation_id),
            )
        return TranscriptEntryRef(entry_id, digest)

    def replace_active(
        self,
        conversation_id: str,
        turn_id: str,
        messages: list[Mapping[str, Any]],
    ) -> tuple[TranscriptEntryRef, ...]:
        encoded = [dumps_message(dict(message)) for message in messages]
        refs: list[TranscriptEntryRef] = []
        with self._transaction() as connection:
            self._ensure_conversation(connection, conversation_id)
            for payload_json in encoded:
                entry_id = _new_entry_id()
                digest = _digest(payload_json)
                refs.append(TranscriptEntryRef(entry_id, digest))
                connection.execute(
                    """
                    INSERT INTO transcript_entries (
                        entry_id, conversation_id, turn_id, entry_kind, payload_json,
                        content_sha256, commit_state, created_at_ms
                    ) VALUES (?, ?, ?, 'compaction_summary', ?, ?, 'committed', ?)
                    """,
                    (
                        entry_id,
                        conversation_id,
                        turn_id,
                        payload_json,
                        digest,
                        self._now(),
                    ),
                )
            connection.execute(
                "DELETE FROM transcript_active_order WHERE conversation_id = ?",
                (conversation_id,),
            )
            connection.executemany(
                "INSERT INTO transcript_active_order "
                "(conversation_id, seq, entry_id) VALUES (?, ?, ?)",
                (
                    (conversation_id, seq, ref.entry_id)
                    for seq, ref in enumerate(refs)
                ),
            )
            connection.execute(
                "UPDATE transcript_conversations SET view_revision = view_revision + 1, "
                "updated_at_ms = ? WHERE conversation_id = ?",
                (self._now(), conversation_id),
            )
        return tuple(refs)

    def stage_terminal(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        provider_message: Mapping[str, Any] | None,
        public_text: str,
        terminal_kind: str = "normal",
    ) -> TranscriptEntryRef:
        payload_json = dumps_message(
            _terminal_payload(
                provider_message=provider_message,
                public_text=public_text,
                terminal_kind=terminal_kind,
            )
        )
        digest = _digest(payload_json)
        with self._transaction() as connection:
            self._ensure_conversation(connection, conversation_id)
            existing = connection.execute(
                """
                SELECT entry_id, conversation_id, content_sha256
                FROM transcript_entries
                WHERE turn_id = ? AND entry_kind = 'terminal_message'
                  AND commit_state IN ('terminal_candidate','committed')
                """,
                (turn_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["conversation_id"]) != conversation_id:
                    raise TranscriptIntegrityError(
                        "Turn already has a terminal candidate in another Conversation"
                    )
                if str(existing["content_sha256"]) != digest:
                    raise TranscriptIntegrityError(
                        "Turn already has a different live terminal candidate digest"
                    )
                return TranscriptEntryRef(
                    str(existing["entry_id"]), str(existing["content_sha256"])
                )
            entry_id = _new_entry_id()
            connection.execute(
                """
                INSERT INTO transcript_entries (
                    entry_id, conversation_id, turn_id, entry_kind, payload_json,
                    content_sha256, commit_state, created_at_ms
                ) VALUES (?, ?, ?, 'terminal_message', ?, ?,
                          'terminal_candidate', ?)
                """,
                (
                    entry_id,
                    conversation_id,
                    turn_id,
                    payload_json,
                    digest,
                    self._now(),
                ),
            )
        return TranscriptEntryRef(entry_id, digest)

    def verify_terminal_candidate(
        self,
        entry_id: str,
        content_sha256: str,
        *,
        expected_conversation_id: str | None = None,
        expected_turn_id: str | None = None,
        expected_public_text: str | None = None,
        expected_terminal_kind: str | None = None,
        expected_commit_state: str = "terminal_candidate",
    ) -> TranscriptEntryRef:
        entry = self.get_entry(entry_id)
        if entry.entry_kind != "terminal_message":
            raise TranscriptIntegrityError("terminal reference is not a terminal entry")
        if entry.content_sha256 != content_sha256:
            raise TranscriptIntegrityError("terminal candidate digest mismatch")
        if entry.commit_state != expected_commit_state:
            raise TranscriptIntegrityError(
                f"terminal entry is not {expected_commit_state}"
            )
        if (
            expected_conversation_id is not None
            and entry.conversation_id != expected_conversation_id
        ):
            raise TranscriptIntegrityError(
                "terminal candidate belongs to a different Conversation"
            )
        if expected_turn_id is not None and entry.turn_id != expected_turn_id:
            raise TranscriptIntegrityError(
                "terminal candidate belongs to a different Turn"
            )
        if (
            expected_public_text is not None
            and entry.public_text != expected_public_text
        ):
            raise TranscriptIntegrityError(
                "terminal candidate public text does not match the Worker Final"
            )
        if (
            expected_terminal_kind is not None
            and str(entry.payload.get("terminal_kind", ""))
            != expected_terminal_kind
        ):
            raise TranscriptIntegrityError(
                "terminal candidate kind does not match the Worker Final"
            )
        actual = _digest(dumps_message(dict(entry.payload)))
        if actual != content_sha256:
            raise TranscriptIntegrityError("terminal candidate payload digest mismatch")
        return TranscriptEntryRef(entry.entry_id, entry.content_sha256)

    def verify_committed_terminal(
        self,
        entry_id: str,
        content_sha256: str,
        *,
        expected_conversation_id: str,
        expected_turn_id: str,
    ) -> TranscriptEntryRef:
        """Verify the complete cross-store identity of a terminal Receipt ref."""

        ref = self.verify_terminal_candidate(
            entry_id,
            content_sha256,
            expected_conversation_id=expected_conversation_id,
            expected_turn_id=expected_turn_id,
            expected_commit_state="committed",
        )
        entry = self.get_entry(entry_id)
        with self._lock:
            active = self._conn.execute(
                "SELECT 1 FROM transcript_active_order WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
        provider_message = _provider_message(entry.entry_kind, entry.payload)
        if (provider_message is not None) != (active is not None):
            raise TranscriptIntegrityError(
                "terminal Receipt entry active-view membership is inconsistent"
            )
        return ref

    def promote_terminal(
        self,
        entry_id: str,
        content_sha256: str,
        *,
        expected_conversation_id: str | None = None,
        expected_turn_id: str | None = None,
    ) -> TranscriptEntryRef:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM transcript_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise KeyError(entry_id)
            if str(row["content_sha256"]) != content_sha256:
                raise TranscriptIntegrityError("terminal candidate digest mismatch")
            if str(row["entry_kind"]) != "terminal_message":
                raise TranscriptIntegrityError("terminal reference is not terminal")
            if (
                expected_conversation_id is not None
                and str(row["conversation_id"]) != expected_conversation_id
            ):
                raise TranscriptIntegrityError(
                    "terminal candidate belongs to a different Conversation"
                )
            row_turn_id = (
                str(row["turn_id"]) if row["turn_id"] is not None else None
            )
            if expected_turn_id is not None and row_turn_id != expected_turn_id:
                raise TranscriptIntegrityError(
                    "terminal candidate belongs to a different Turn"
                )
            payload = json.loads(str(row["payload_json"]))
            if _digest(str(row["payload_json"])) != content_sha256:
                raise TranscriptIntegrityError("terminal candidate payload digest mismatch")
            state = str(row["commit_state"])
            if state == "abandoned":
                raise TranscriptIntegrityError("abandoned terminal candidate cannot promote")
            conversation_id = str(row["conversation_id"])
            if state == "terminal_candidate":
                connection.execute(
                    "UPDATE transcript_entries SET commit_state = 'committed' "
                    "WHERE entry_id = ?",
                    (entry_id,),
                )
            provider_message = _provider_message("terminal_message", payload)
            active = connection.execute(
                "SELECT 1 FROM transcript_active_order WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if provider_message is not None and active is None:
                connection.execute(
                    "INSERT INTO transcript_active_order "
                    "(conversation_id, seq, entry_id) VALUES (?, ?, ?)",
                    (
                        conversation_id,
                        self._next_seq(connection, conversation_id),
                        entry_id,
                    ),
                )
                connection.execute(
                    "UPDATE transcript_conversations "
                    "SET view_revision = view_revision + 1, updated_at_ms = ? "
                    "WHERE conversation_id = ?",
                    (self._now(), conversation_id),
                )
        return TranscriptEntryRef(entry_id, content_sha256)

    def abandon_terminal(
        self, entry_id: str, content_sha256: str
    ) -> TranscriptEntryRef:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT entry_kind, content_sha256, commit_state "
                "FROM transcript_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise KeyError(entry_id)
            if str(row["content_sha256"]) != content_sha256:
                raise TranscriptIntegrityError("terminal candidate digest mismatch")
            if str(row["entry_kind"]) != "terminal_message":
                raise TranscriptIntegrityError("terminal reference is not terminal")
            state = str(row["commit_state"])
            if state == "committed":
                raise TranscriptIntegrityError("committed terminal entry cannot abandon")
            if state == "terminal_candidate":
                connection.execute(
                    "UPDATE transcript_entries SET commit_state = 'abandoned' "
                    "WHERE entry_id = ?",
                    (entry_id,),
                )
        return TranscriptEntryRef(entry_id, content_sha256)

    def get_entry(self, entry_id: str) -> TranscriptEntry:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transcript_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        if row is None:
            raise KeyError(entry_id)
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise TranscriptIntegrityError("Transcript payload is not a JSON object")
        return TranscriptEntry(
            entry_id=str(row["entry_id"]),
            conversation_id=str(row["conversation_id"]),
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
            entry_kind=str(row["entry_kind"]),
            payload=payload,
            content_sha256=str(row["content_sha256"]),
            commit_state=str(row["commit_state"]),
            created_at_ms=int(row["created_at_ms"]),
        )

    def list_entries(self, conversation_id: str) -> tuple[TranscriptEntry, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_id FROM transcript_entries "
                "WHERE conversation_id = ? ORDER BY created_at_ms, entry_id",
                (conversation_id,),
            ).fetchall()
        return tuple(self.get_entry(str(row["entry_id"])) for row in rows)

    def list_entry_ids(self, conversation_id: str) -> tuple[str, ...]:
        return tuple(entry.entry_id for entry in self.list_entries(conversation_id))

    def get_history(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.entry_kind, e.payload_json
                FROM transcript_active_order AS a
                JOIN transcript_entries AS e USING (entry_id)
                WHERE a.conversation_id = ?
                ORDER BY a.seq
                """,
                (conversation_id,),
            ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise TranscriptIntegrityError("Transcript payload is not a JSON object")
            message = _provider_message(str(row["entry_kind"]), payload)
            if message is not None:
                history.append(message)
        return history

    def find_live_terminal(self, turn_id: str) -> TranscriptEntryRef | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT entry_id, content_sha256 FROM transcript_entries
                WHERE turn_id = ? AND entry_kind = 'terminal_message'
                  AND commit_state IN ('terminal_candidate','committed')
                """,
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return TranscriptEntryRef(str(row["entry_id"]), str(row["content_sha256"]))

    def list_terminal_candidates(self) -> tuple[TranscriptEntryRef, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_id, content_sha256 FROM transcript_entries "
                "WHERE commit_state = 'terminal_candidate' ORDER BY created_at_ms, entry_id"
            ).fetchall()
        return tuple(
            TranscriptEntryRef(str(row["entry_id"]), str(row["content_sha256"]))
            for row in rows
        )

    @property
    def transcript_store_id(self) -> str:
        with self._lock:
            rows = self._conn.execute(
                "SELECT store_id FROM transcript_store_identity"
            ).fetchall()
        if len(rows) != 1:
            raise TranscriptIntegrityError(
                "Transcript Store has no unique immutable store identity"
            )
        return _require_lower_hex(
            str(rows[0]["store_id"]),
            length=32,
            name="transcript_store_id",
        )

    def get_cutover_identity(
        self,
        import_run_id: str,
    ) -> TranscriptCutoverIdentity | None:
        run_id = _require_lower_hex(import_run_id, length=32, name="import_run_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transcript_cutover_identities WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return TranscriptCutoverIdentity(
            import_run_id=run_id,
            cutover_manifest_sha256=str(row["cutover_manifest_sha256"]),
            transcript_store_id=self.transcript_store_id,
            import_baseline_sha256=str(row["import_baseline_sha256"]),
            source_identity=str(row["source_identity"]),
        )

    def _verify_persisted_import_baseline(
        self,
        import_run_id: str,
    ) -> TranscriptCutoverIdentity:
        identity = self.get_cutover_identity(import_run_id)
        if identity is None:
            raise TranscriptIntegrityError(
                "Transcript Store has no matching immutable cutover identity"
            )
        with self._lock:
            run = self._conn.execute(
                """
                SELECT r.*, c.source_key_sha256 AS cutover_source_key,
                       c.source_manifest_sha256 AS cutover_source_manifest
                FROM transcript_import_runs AS r
                JOIN transcript_cutover_identities AS c USING (source_key_sha256)
                WHERE c.import_run_id = ?
                """,
                (import_run_id,),
            ).fetchone()
            conversation_rows = self._conn.execute(
                """
                SELECT legacy_key, conversation_id, initial_message_count
                FROM transcript_import_conversations
                WHERE import_run_id = ? ORDER BY legacy_key
                """,
                (import_run_id,),
            ).fetchall()
            entry_rows = self._conn.execute(
                """
                SELECT b.legacy_key, b.source_seq, b.conversation_id, b.entry_id,
                       b.content_sha256, b.payload_json,
                       e.turn_id, e.entry_kind, e.content_sha256 AS live_content_sha256,
                       e.payload_json AS live_payload_json, e.commit_state
                FROM transcript_import_entries AS b
                LEFT JOIN transcript_entries AS e
                  ON e.conversation_id = b.conversation_id
                 AND e.entry_id = b.entry_id
                WHERE b.import_run_id = ?
                ORDER BY b.legacy_key, b.source_seq
                """,
                (import_run_id,),
            ).fetchall()
        if run is None:
            raise TranscriptIntegrityError("Transcript import run identity drift")
        source_identity = str(run["source_path"])
        source_key = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
        if (
            source_key != str(run["source_key_sha256"])
            or str(run["source_key_sha256"]) != str(run["cutover_source_key"])
            or str(run["source_manifest_sha256"])
            != str(run["cutover_source_manifest"])
            or source_identity != identity.source_identity
        ):
            raise TranscriptIntegrityError("Transcript import source identity drift")

        conversations = [
            {
                "legacy_key": str(row["legacy_key"]),
                "conversation_id": str(row["conversation_id"]),
                "initial_message_count": int(row["initial_message_count"]),
            }
            for row in conversation_rows
        ]
        entries: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for row in entry_rows:
            legacy_key = str(row["legacy_key"])
            counts[legacy_key] = counts.get(legacy_key, 0) + 1
            if (
                row["turn_id"] is not None
                or str(row["entry_kind"] or "") != "provider_message"
                or str(row["commit_state"] or "") != "committed"
                or str(row["live_content_sha256"] or "")
                != str(row["content_sha256"])
                or str(row["live_payload_json"] or "") != str(row["payload_json"])
                or _digest(str(row["payload_json"])) != str(row["content_sha256"])
            ):
                raise TranscriptIntegrityError(
                    f"Transcript import baseline drift at {legacy_key}:{row['source_seq']}"
                )
            entries.append(
                {
                    "legacy_key": legacy_key,
                    "source_seq": int(row["source_seq"]),
                    "conversation_id": str(row["conversation_id"]),
                    "entry_id": str(row["entry_id"]),
                    "content_sha256": str(row["content_sha256"]),
                    "payload_json": str(row["payload_json"]),
                }
            )
        if any(
            counts.get(row["legacy_key"], 0) != row["initial_message_count"]
            for row in conversations
        ):
            raise TranscriptIntegrityError("Transcript import baseline count drift")
        if (
            int(run["conversation_count"]) != len(conversations)
            or int(run["entry_count"]) != len(entries)
        ):
            raise TranscriptIntegrityError("Transcript import run count drift")
        baseline = _import_baseline_digest(
            import_run_id=import_run_id,
            source_manifest_sha256=str(run["source_manifest_sha256"]),
            conversations=conversations,
            entries=entries,
        )
        if baseline != identity.import_baseline_sha256:
            raise TranscriptIntegrityError("Transcript import baseline digest drift")
        return identity

    def verify_cutover_identity(
        self,
        *,
        import_run_id: str,
        cutover_manifest_sha256: str,
        transcript_store_id: str,
        import_baseline_sha256: str,
        source_identity: str,
    ) -> TranscriptCutoverIdentity:
        """Cross-check Control evidence against the immutable import baseline."""

        run_id = _require_lower_hex(import_run_id, length=32, name="import_run_id")
        cutover = _require_lower_hex(
            cutover_manifest_sha256,
            length=64,
            name="cutover_manifest_sha256",
        )
        store_id = _require_lower_hex(
            transcript_store_id,
            length=32,
            name="transcript_store_id",
        )
        baseline = _require_lower_hex(
            import_baseline_sha256,
            length=64,
            name="import_baseline_sha256",
        )
        identity = self._verify_persisted_import_baseline(run_id)
        if (
            identity.cutover_manifest_sha256 != cutover
            or identity.transcript_store_id != store_id
            or identity.import_baseline_sha256 != baseline
            or identity.source_identity != str(source_identity)
        ):
            raise TranscriptIntegrityError(
                "Control and Transcript cutover identities do not match"
            )
        self._assert_integrity()
        return identity

    def _import_mapped_snapshot(
        self,
        source_path: str | Path,
        *,
        import_run_id: str,
        cutover_manifest_sha256: str,
        source_identity: str | Path,
        expected_conversations: Sequence[Mapping[str, Any]],
        backup_path: str | Path,
    ) -> LegacyTranscriptImportReport:
        """Internal Adapter seam for one already-mapped, durable snapshot."""

        run_id = _require_lower_hex(import_run_id, length=32, name="import_run_id")
        cutover_manifest = _require_lower_hex(
            cutover_manifest_sha256,
            length=64,
            name="cutover_manifest_sha256",
        )
        source = Path(source_path).expanduser().resolve()
        if source == self.database_path:
            raise ValueError("legacy source and canonical Transcript database differ")
        durable_backup = Path(backup_path).expanduser().resolve()
        if not durable_backup.is_file():
            raise FileNotFoundError(durable_backup)
        messages, manifest = _read_legacy_messages(
            source,
            require_canonical_ids=True,
        )
        streams = inspect_legacy_streams(source)
        expected: dict[str, tuple[str, int]] = {}
        for raw in expected_conversations:
            legacy_key = str(raw.get("legacy_key", "")).strip()
            if not legacy_key or legacy_key in expected:
                raise TranscriptImportConflict(
                    "mapped snapshot has duplicate or empty legacy identity"
                )
            conversation_id = _require_lower_hex(
                str(raw.get("conversation_id", "")),
                length=32,
                name="conversation_id",
            )
            message_count = int(raw.get("message_count", -1))
            if message_count < 0:
                raise TranscriptImportConflict(
                    "mapped snapshot has an invalid message count"
                )
            expected[legacy_key] = (conversation_id, message_count)

        actual_conversations: list[dict[str, Any]] = []
        for stream in streams:
            expected_value = expected.get(stream.chat_key)
            decoded = json.loads(stream.chat_id_json)
            conversation_id = str(decoded)
            if expected_value != (conversation_id, stream.message_count):
                raise TranscriptImportConflict(
                    f"mapped snapshot evidence drift for {stream.chat_key}"
                )
            actual_conversations.append(
                {
                    "legacy_key": stream.chat_key,
                    "conversation_id": conversation_id,
                    "initial_message_count": stream.message_count,
                }
            )
        if {stream.chat_key for stream in streams} != set(expected):
            raise TranscriptImportConflict("mapped snapshot Conversation set drift")
        actual_conversations.sort(key=lambda value: str(value["legacy_key"]))

        entry_baseline: list[dict[str, Any]] = []
        messages_by_key: dict[str, list[tuple[_LegacyMessage, str, str]]] = {}
        for item in messages:
            seed = f"legacy-transcript-v1\0{manifest}\0{item.chat_key}\0{item.seq}"
            entry_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
            content_sha256 = _digest(item.payload_json)
            messages_by_key.setdefault(item.chat_key, []).append(
                (item, entry_id, content_sha256)
            )
            entry_baseline.append(
                {
                    "legacy_key": item.chat_key,
                    "source_seq": item.seq,
                    "conversation_id": item.conversation_id,
                    "entry_id": entry_id,
                    "content_sha256": content_sha256,
                    "payload_json": item.payload_json,
                }
            )
        baseline_sha256 = _import_baseline_digest(
            import_run_id=run_id,
            source_manifest_sha256=manifest,
            conversations=actual_conversations,
            entries=entry_baseline,
        )
        source_identity_path = Path(source_identity).expanduser().resolve()
        source_identity_text = str(source_identity_path)
        source_key = hashlib.sha256(source_identity_text.encode("utf-8")).hexdigest()

        existing_identity = self.get_cutover_identity(run_id)
        if existing_identity is not None:
            if (
                existing_identity.cutover_manifest_sha256 != cutover_manifest
                or existing_identity.import_baseline_sha256 != baseline_sha256
                or existing_identity.source_identity != source_identity_text
            ):
                raise TranscriptImportConflict(
                    "legacy Transcript cutover identity was reused with different evidence"
                )
            self._verify_persisted_import_baseline(run_id)
            with self._lock:
                run = self._conn.execute(
                    "SELECT * FROM transcript_import_runs WHERE source_key_sha256 = ?",
                    (source_key,),
                ).fetchone()
            if run is None:
                raise TranscriptIntegrityError("Transcript import run identity drift")
            return LegacyTranscriptImportReport(
                state="committed",
                source_manifest_sha256=manifest,
                conversation_count=int(run["conversation_count"]),
                entry_count=int(run["entry_count"]),
                backup_path=str(run["backup_path"]),
            )

        conversation_ids = [
            str(value["conversation_id"]) for value in actual_conversations
        ]
        with self._lock:
            occupied = [
                conversation_id
                for conversation_id in conversation_ids
                if self._conn.execute(
                    "SELECT 1 FROM transcript_conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                is not None
            ]
        if occupied:
            raise TranscriptImportConflict(
                "canonical Transcript already owns Conversation identity for: "
                + ", ".join(occupied[:5])
            )

        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO transcript_import_runs (
                    source_key_sha256, source_manifest_sha256, source_path, state,
                    conversation_count, entry_count, backup_path,
                    committed_at_ms, cutover_at_ms
                ) VALUES (?, ?, ?, 'committed', ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    manifest,
                    source_identity_text,
                    len(actual_conversations),
                    len(messages),
                    str(durable_backup),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO transcript_cutover_identities (
                    import_run_id, cutover_manifest_sha256, source_identity,
                    source_key_sha256, source_manifest_sha256,
                    import_baseline_sha256, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    cutover_manifest,
                    source_identity_text,
                    source_key,
                    manifest,
                    baseline_sha256,
                    now,
                ),
            )
            ordinal = 0
            for conversation in actual_conversations:
                legacy_key = str(conversation["legacy_key"])
                conversation_id = str(conversation["conversation_id"])
                self._ensure_conversation(connection, conversation_id)
                connection.execute(
                    """
                    INSERT INTO transcript_import_conversations (
                        import_run_id, legacy_key, conversation_id,
                        initial_message_count
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        legacy_key,
                        conversation_id,
                        int(conversation["initial_message_count"]),
                    ),
                )
                for item, entry_id, content_sha256 in messages_by_key.get(
                    legacy_key, []
                ):
                    connection.execute(
                        """
                        INSERT INTO transcript_entries (
                            entry_id, conversation_id, turn_id, entry_kind,
                            payload_json, content_sha256, commit_state, created_at_ms
                        ) VALUES (?, ?, NULL, 'provider_message', ?, ?, 'committed', ?)
                        """,
                        (
                            entry_id,
                            conversation_id,
                            item.payload_json,
                            content_sha256,
                            now + ordinal,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO transcript_active_order "
                        "(conversation_id, seq, entry_id) VALUES (?, ?, ?)",
                        (conversation_id, item.seq, entry_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO transcript_import_entries (
                            import_run_id, legacy_key, source_seq, conversation_id,
                            entry_id, content_sha256, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            legacy_key,
                            item.seq,
                            conversation_id,
                            entry_id,
                            content_sha256,
                            item.payload_json,
                        ),
                    )
                    ordinal += 1
                connection.execute(
                    "UPDATE transcript_conversations "
                    "SET view_revision = view_revision + 1, updated_at_ms = ? "
                    "WHERE conversation_id = ?",
                    (now, conversation_id),
                )
        self._assert_integrity()
        return LegacyTranscriptImportReport(
            state="committed",
            source_manifest_sha256=manifest,
            conversation_count=len(actual_conversations),
            entry_count=len(messages),
            backup_path=str(durable_backup),
        )

    def verify_legacy_import(
        self,
        source_path: str | Path,
        *,
        import_run_id: str,
        cutover_manifest_sha256: str,
        source_identity: str | Path,
        require_initial_view: bool,
    ) -> LegacyTranscriptImportReport:
        """Verify a snapshot against its immutable import baseline."""

        source = Path(source_path).expanduser().resolve()
        messages, manifest = _read_legacy_messages(
            source,
            require_canonical_ids=True,
        )
        identity = self.get_cutover_identity(import_run_id)
        if identity is None:
            raise TranscriptImportConflict(
                "legacy Transcript has no matching committed canonical import"
            )
        source_identity_text = str(Path(source_identity).expanduser().resolve())
        if (
            identity.cutover_manifest_sha256 != cutover_manifest_sha256
            or identity.source_identity != source_identity_text
        ):
            raise TranscriptImportConflict(
                "legacy Transcript has no matching committed canonical import"
            )
        self._verify_persisted_import_baseline(import_run_id)
        with self._lock:
            run = self._conn.execute(
                """
                SELECT r.* FROM transcript_import_runs AS r
                JOIN transcript_cutover_identities AS c USING (source_key_sha256)
                WHERE c.import_run_id = ?
                """,
                (import_run_id,),
            ).fetchone()
            baseline_conversations = self._conn.execute(
                """
                SELECT legacy_key, conversation_id, initial_message_count
                FROM transcript_import_conversations
                WHERE import_run_id = ? ORDER BY legacy_key
                """,
                (import_run_id,),
            ).fetchall()
        if run is None or str(run["source_manifest_sha256"]) != manifest:
            raise TranscriptImportConflict(
                "legacy Transcript has no matching committed canonical import"
            )
        streams = inspect_legacy_streams(source)
        expected_conversations = [
            (
                str(row["legacy_key"]),
                str(row["conversation_id"]),
                int(row["initial_message_count"]),
            )
            for row in baseline_conversations
        ]
        actual_conversations = [
            (
                stream.chat_key,
                str(json.loads(stream.chat_id_json)),
                stream.message_count,
            )
            for stream in streams
        ]
        if actual_conversations != expected_conversations:
            raise TranscriptIntegrityError("legacy Transcript Conversation baseline drift")

        expected_by_conversation: dict[str, list[str]] = {
            conversation_id: []
            for _legacy_key, conversation_id, _message_count in expected_conversations
        }
        with self._lock:
            baseline_entries = {
                (str(row["legacy_key"]), int(row["source_seq"])): row
                for row in self._conn.execute(
                    """
                    SELECT * FROM transcript_import_entries
                    WHERE import_run_id = ?
                    """,
                    (import_run_id,),
                ).fetchall()
            }
        for item in messages:
            row = baseline_entries.get((item.chat_key, item.seq))
            if row is None:
                raise TranscriptIntegrityError(
                    f"legacy Transcript import drift at {item.chat_key}:{item.seq}"
                )
            entry_id = str(row["entry_id"])
            expected_by_conversation[item.conversation_id].append(entry_id)
            if (
                str(row["conversation_id"]) != item.conversation_id
                or str(row["payload_json"]) != item.payload_json
                or str(row["content_sha256"]) != _digest(item.payload_json)
            ):
                raise TranscriptIntegrityError(
                    f"legacy Transcript import drift at {item.chat_key}:{item.seq}"
                )
        if len(baseline_entries) != len(messages):
            raise TranscriptIntegrityError("legacy Transcript import count drift")

        if require_initial_view:
            with self._lock:
                for conversation_id, expected_entry_ids in expected_by_conversation.items():
                    active_entry_ids = [
                        str(row["entry_id"])
                        for row in self._conn.execute(
                            "SELECT entry_id FROM transcript_active_order "
                            "WHERE conversation_id = ? ORDER BY seq",
                            (conversation_id,),
                        ).fetchall()
                    ]
                    all_entry_ids = {
                        str(row["entry_id"])
                        for row in self._conn.execute(
                            "SELECT entry_id FROM transcript_entries "
                            "WHERE conversation_id = ?",
                            (conversation_id,),
                        ).fetchall()
                    }
                    if active_entry_ids != expected_entry_ids or all_entry_ids != set(
                        expected_entry_ids
                    ):
                        raise TranscriptIntegrityError(
                            f"legacy Transcript initial-view drift for {conversation_id}"
                        )
        self._assert_integrity()
        return LegacyTranscriptImportReport(
            state="committed",
            source_manifest_sha256=manifest,
            conversation_count=int(run["conversation_count"]),
            entry_count=int(run["entry_count"]),
            backup_path=str(run["backup_path"]),
        )


class TurnTranscriptAdapter:
    """Legacy Query Engine Interface bound to exactly one durable Turn."""

    supports_terminal_candidates = True

    def __init__(
        self,
        transcript: CanonicalTranscript,
        conversation_id: str,
        turn_id: str,
    ) -> None:
        self.transcript = transcript
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.max_history = 50
        self.max_history_chars: int | None = None
        self.max_conversations = 1000
        self.sanitizer = transcript.sanitizer
        self._accessed_at = 0.0
        self._pending_terminal_message: dict[str, Any] | None = None

    def _require_chat(self, chat_id: int | str) -> None:
        if str(chat_id) != self.conversation_id:
            raise TranscriptIntegrityError(
                "Turn Transcript Adapter cannot cross Conversation identity"
            )

    def get_history(self, chat_id: int | str) -> list[dict[str, Any]]:
        self._require_chat(chat_id)
        return self.transcript.get_history(self.conversation_id)

    def touch(self, chat_id: int | str, *, at: float | None = None) -> None:
        self._require_chat(chat_id)
        self._accessed_at = time.time() if at is None else float(at)

    def evict_lru_conversations(self) -> list[int | str]:
        # This Adapter holds no conversation cache; SQLite is already bounded by
        # durable rows, so there is nothing process-local to evict.
        return []

    def append_user_message(self, chat_id: int | str, content: Any) -> dict:
        self._require_chat(chat_id)
        message = {"role": "user", "content": content}
        self.transcript.append_message(self.conversation_id, self.turn_id, message)
        return message

    def append_assistant_message(
        self,
        chat_id: int | str,
        *,
        content: str,
        tool_calls: list[dict] | None = None,
        reasoning_content: str | None = None,
    ) -> dict:
        self._require_chat(chat_id)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        self.transcript.append_message(self.conversation_id, self.turn_id, message)
        return message

    def append_tool_message(
        self,
        chat_id: int | str,
        *,
        tool_call_id: str,
        content: str,
    ) -> dict:
        self._require_chat(chat_id)
        message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }
        self.transcript.append_message(self.conversation_id, self.turn_id, message)
        return message

    def replace_history(
        self, chat_id: int | str, messages: list[dict[str, Any]]
    ) -> None:
        self._require_chat(chat_id)
        self.transcript.replace_active(self.conversation_id, self.turn_id, messages)

    def prepare_history(
        self, chat_id: int | str, *, warn: bool = True
    ) -> list[dict[str, Any]]:
        self._require_chat(chat_id)
        history = self.transcript.get_history(self.conversation_id)
        prepared = self.sanitizer(list(history), warn=warn)
        if prepared != history:
            self.replace_history(chat_id, prepared)
        return list(prepared)

    def defer_terminal_message(
        self,
        chat_id: int | str,
        *,
        content: str,
        tool_calls: list[dict] | None = None,
        reasoning_content: str | None = None,
    ) -> dict:
        self._require_chat(chat_id)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        self._pending_terminal_message = message
        return message

    def stage_terminal(
        self,
        public_text: str,
        *,
        terminal_kind: str = "normal",
        model_visible: bool = True,
    ) -> TranscriptEntryRef:
        provider_message = self._pending_terminal_message
        if model_visible and provider_message is None:
            provider_message = {"role": "assistant", "content": str(public_text)}
        if not model_visible:
            provider_message = None
        return self.transcript.stage_terminal(
            self.conversation_id,
            self.turn_id,
            provider_message=provider_message,
            public_text=str(public_text),
            terminal_kind=terminal_kind,
        )

    def abandon_terminal(self, entry_id: str, content_sha256: str) -> None:
        self.transcript.abandon_terminal(entry_id, content_sha256)
        self._pending_terminal_message = None

    def discard_pending_terminal_message(self) -> None:
        """Drop an uncommitted provider completion before a control-owned outcome."""

        self._pending_terminal_message = None


def _read_legacy_messages(
    source_path: Path,
    *,
    require_canonical_ids: bool = False,
) -> tuple[list[_LegacyMessage], str]:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise TranscriptIntegrityError(
                f"legacy Transcript integrity check failed: {integrity}"
            )
        required = {"transcript_chats", "transcript_messages"}
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not required.issubset(tables):
            raise TranscriptImportConflict(
                "legacy Transcript schema is missing required tables"
            )
        schema_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        chat_rows = connection.execute(
            "SELECT chat_key, chat_id_json, last_seq "
            "FROM transcript_chats ORDER BY chat_key"
        ).fetchall()
        rows = connection.execute(
            """
            SELECT c.chat_key, c.chat_id_json, c.last_seq, m.seq, m.payload_json
            FROM transcript_chats AS c
            JOIN transcript_messages AS m USING (chat_key)
            ORDER BY c.chat_key, m.seq
            """
        ).fetchall()
    finally:
        connection.close()

    messages: list[_LegacyMessage] = []
    manifest = hashlib.sha256()
    for schema_row in schema_rows:
        for value in (str(schema_row["name"]), str(schema_row["sql"] or "")):
            encoded = value.encode("utf-8")
            manifest.update(len(encoded).to_bytes(8, "big"))
            manifest.update(encoded)

    expected_last_seq: dict[str, int] = {}
    canonical_ids: dict[str, str] = {}
    for row in chat_rows:
        chat_key = str(row["chat_key"])
        chat_id_json = str(row["chat_id_json"])
        decoded_chat_id = json.loads(chat_id_json)
        conversation_id = str(decoded_chat_id)
        if require_canonical_ids and (
            len(conversation_id) != 32
            or any(character not in "0123456789abcdef" for character in conversation_id)
        ):
            raise TranscriptImportConflict(
                "legacy stream has no explicit canonical Conversation mapping"
            )
        if require_canonical_ids:
            prior = canonical_ids.setdefault(conversation_id, chat_key)
            if prior != chat_key:
                raise TranscriptImportConflict(
                    "multiple legacy streams map to one canonical Conversation"
                )
        expected_last_seq[chat_key] = int(row["last_seq"])
        for value in (chat_key, chat_id_json, str(row["last_seq"])):
            encoded = value.encode("utf-8")
            manifest.update(len(encoded).to_bytes(8, "big"))
            manifest.update(encoded)

    seen_sequences: dict[str, list[int]] = {}
    for row in rows:
        chat_key = str(row["chat_key"])
        chat_id_json = str(row["chat_id_json"])
        decoded_chat_id = json.loads(chat_id_json)
        conversation_id = str(decoded_chat_id)
        seq = int(row["seq"])
        seen_sequences.setdefault(chat_key, []).append(seq)
        payload_json = str(row["payload_json"])
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise TranscriptImportConflict(
                "legacy Transcript message payload is not a JSON object"
            )
        for value in (chat_key, chat_id_json, str(seq), payload_json):
            encoded = value.encode("utf-8")
            manifest.update(len(encoded).to_bytes(8, "big"))
            manifest.update(encoded)
        messages.append(
            _LegacyMessage(
                chat_key=chat_key,
                conversation_id=conversation_id,
                chat_id_json=chat_id_json,
                seq=seq,
                payload_json=payload_json,
                payload=payload,
            )
        )
    for chat_key, last_seq in expected_last_seq.items():
        sequences = seen_sequences.get(chat_key, [])
        expected = list(range(last_seq + 1)) if last_seq >= 0 else []
        if sequences != expected:
            raise TranscriptImportConflict(
                f"legacy Transcript sequence gap for {chat_key}"
            )
    return messages, manifest.hexdigest()


def plan_legacy_import(source_path: str | Path) -> LegacyTranscriptImportReport:
    """Inspect a legacy database without creating or mutating canonical state."""

    source = Path(source_path).expanduser().resolve()
    messages, manifest = _read_legacy_messages(
        source,
        require_canonical_ids=False,
    )
    return LegacyTranscriptImportReport(
        state="planned",
        source_manifest_sha256=manifest,
        conversation_count=len(inspect_legacy_streams(source)),
        entry_count=len(messages),
    )


def inspect_legacy_streams(
    source_path: str | Path,
) -> tuple[LegacyTranscriptStream, ...]:
    """Return legacy stream identities without assigning canonical identity."""

    source = Path(source_path).expanduser().resolve()
    # Reuse the strict schema/integrity/sequence validation first.
    _read_legacy_messages(source, require_canonical_ids=False)
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT c.chat_key, c.chat_id_json, COUNT(m.seq) AS message_count
            FROM transcript_chats AS c
            LEFT JOIN transcript_messages AS m USING (chat_key)
            GROUP BY c.chat_key, c.chat_id_json
            ORDER BY c.chat_key
            """
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        LegacyTranscriptStream(
            chat_key=str(row["chat_key"]),
            chat_id_json=str(row["chat_id_json"]),
            message_count=int(row["message_count"]),
        )
        for row in rows
    )


__all__ = [
    "CanonicalTranscript",
    "LegacyTranscriptImportReport",
    "LegacyTranscriptStream",
    "TranscriptEntry",
    "TranscriptEntryRef",
    "TranscriptImportConflict",
    "TranscriptIntegrityError",
    "TurnTranscriptAdapter",
    "plan_legacy_import",
    "inspect_legacy_streams",
]
