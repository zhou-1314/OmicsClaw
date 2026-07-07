"""ADR 0040 — restart-resilient transcript persistence (stdlib ``sqlite3``, sync).

A dedicated, write-through mirror of the in-process ``TranscriptStore``'s derived
state (P-state). Uses the standard-library ``sqlite3`` driver (sync — matching the
synchronous ``TranscriptStore`` mutation API; no new dependency, no async/await
plumbing). Message-per-row with an **opaque JSON payload**; a synchronous
per-mutation commit under WAL + a per-store write lock; byte-identical rehydrate.

This module is the durability + cold-start backstop only — the in-memory store
remains the source of truth for building requests (ADR 0024 byte-stable prefix).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_chats (
    chat_key     TEXT PRIMARY KEY,
    chat_id_json TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    last_seq     INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE IF NOT EXISTS transcript_messages (
    chat_key     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (chat_key, seq),
    FOREIGN KEY (chat_key) REFERENCES transcript_chats(chat_key) ON DELETE CASCADE
);
"""


def _chat_key(chat_id: int | str) -> str:
    """Type-tag the key so int ``7`` and str ``"7"`` never collide (ADR 0040)."""
    return f"i:{chat_id}" if isinstance(chat_id, int) else f"s:{chat_id}"


def dumps_message(message: dict[str, Any]) -> str:
    """Canonical JSON for one message (ADR 0040 S4 byte-identity).

    Preserve key insertion order (no ``sort_keys``), keep non-ASCII, use compact
    separators — so ``json.loads`` reconstructs a dict that re-serializes to the
    same request bytes as the original.
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


class TranscriptDB:
    """Synchronous write-through mirror of each chat's message list (ADR 0040)."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    def append(self, chat_id: int | str, message: dict[str, Any]) -> None:
        """Append one message at the next ``seq`` (synchronous write-through)."""
        key = _chat_key(chat_id)
        payload = dumps_message(message)
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT last_seq FROM transcript_chats WHERE chat_key=?", (key,)
                ).fetchone()
                if row is None:
                    seq = 0
                    self._conn.execute(
                        "INSERT INTO transcript_chats(chat_key, chat_id_json, updated_at, last_seq)"
                        " VALUES (?,?,?,?)",
                        (key, json.dumps(chat_id), self._now(), 0),
                    )
                else:
                    seq = int(row[0]) + 1
                    self._conn.execute(
                        "UPDATE transcript_chats SET last_seq=?, updated_at=? WHERE chat_key=?",
                        (seq, self._now(), key),
                    )
                self._conn.execute(
                    "INSERT INTO transcript_messages(chat_key, seq, payload_json) VALUES (?,?,?)",
                    (key, seq, payload),
                )
                self._conn.commit()
            except Exception:
                # append() does two writes (last_seq bump + message INSERT); if the
                # second fails, roll back so the connection isn't left mid-transaction
                # (a later commit would otherwise finalize the orphaned last_seq bump).
                self._conn.rollback()
                raise

    def replace(self, chat_id: int | str, messages: list[dict[str, Any]]) -> None:
        """Replace the whole chat with ``messages`` in one atomic transaction.

        Used for a context collapse (``[summary, *survivors]``) and for a
        sanitize-writeback that actually changed the list.
        """
        key = _chat_key(chat_id)
        rows = [(key, i, dumps_message(m)) for i, m in enumerate(messages)]
        last = len(messages) - 1
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM transcript_messages WHERE chat_key=?", (key,)
                )
                self._conn.execute(
                    "INSERT INTO transcript_chats(chat_key, chat_id_json, updated_at, last_seq)"
                    " VALUES (?,?,?,?)"
                    " ON CONFLICT(chat_key) DO UPDATE SET"
                    " last_seq=excluded.last_seq, updated_at=excluded.updated_at",
                    (key, json.dumps(chat_id), self._now(), last),
                )
                if rows:
                    self._conn.executemany(
                        "INSERT INTO transcript_messages(chat_key, seq, payload_json)"
                        " VALUES (?,?,?)",
                        rows,
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def rehydrate(self, chat_id: int | str) -> list[dict[str, Any]]:
        """Return the chat's messages in ``seq`` order (empty list if absent)."""
        key = _chat_key(chat_id)
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload_json FROM transcript_messages WHERE chat_key=? ORDER BY seq",
                (key,),
            )
            return [json.loads(row[0]) for row in cur.fetchall()]

    def has(self, chat_id: int | str) -> bool:
        """True if the chat has a persisted row (used to decide rehydrate-on-miss)."""
        key = _chat_key(chat_id)
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM transcript_chats WHERE chat_key=? LIMIT 1", (key,)
                ).fetchone()
                is not None
            )

    def clear(self, chat_id: int | str) -> None:
        """Delete the chat's persisted rows (user ``/clear`` / ``/forget`` only —
        NOT LRU eviction, which keeps the durable rows for rehydrate; ADR 0040 D6)."""
        key = _chat_key(chat_id)
        with self._lock:
            try:
                # ON DELETE CASCADE removes the messages, but delete both explicitly so
                # the store is correct even if foreign_keys is somehow off.
                self._conn.execute("DELETE FROM transcript_messages WHERE chat_key=?", (key,))
                self._conn.execute("DELETE FROM transcript_chats WHERE chat_key=?", (key,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["TranscriptDB", "dumps_message"]
