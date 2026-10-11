"""The messages compaction removed, kept searchable until the session ends.

:class:`SqliteCompactionArchive` is the read-back half of compaction: a
summary deliberately omits exact values — an ``h5ad`` path, a gene id, a
parameter, an error's own words — and this table is where those values
go on living after the messages that carried them have left the
conversation. ``compaction_archives`` is the source of truth;
``compaction_archive_fts`` is kept in step with it by every method that
writes, exactly as :class:`~omicsclaw.memory.store.LongTermStore` keeps
``memories_fts`` in step with ``long_term_memories``.

Session-scoped by design: rows are born from one conversation's
compactions and die with that conversation (:meth:`purge`, called from
the session store's delete). Anything that should outlive the session
belongs in the long-term store, not here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Sequence

from omicsclaw.schema import Message, Role

from .database import Database
from .sessions import _dump_tool_calls
from .store import _escape_fts, _spaced

__all__ = ["ArchivedMessage", "SqliteCompactionArchive"]


def content_hash(role: str, content: str, tool_calls: str) -> str:
    """The deduplication fingerprint of one archived message.

    ``role``/``content``/``tool_calls`` with a NUL between them, hashed
    with SHA-256. The serialisation of *tool_calls* is
    :func:`~omicsclaw.memory.sessions._dump_tool_calls`'s, so a message
    archived from a compaction and the same message archived again — a
    retried compaction, a race between two processes — hash to the same
    fingerprint and the ``(session_id, content_hash)`` unique index
    folds the second write away.
    """
    return hashlib.sha256(
        (role + "\x00" + content + "\x00" + tool_calls).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchivedMessage:
    """One row, as ``read`` and ``search`` hand it back.

    ``tool_calls`` is the stored JSON text, byte for byte: the display
    layer may parse it, the storage layer never rewrites it (ADR 0077).
    """

    id: int
    seq: int
    position: int
    role: Role
    content: str
    tool_calls: str
    tool_call_id: str
    name: str
    created_at: float


def _row_to_archived(row: sqlite3.Row) -> ArchivedMessage:
    """Rebuild one row from ``compaction_archives``."""
    return ArchivedMessage(
        id=row["id"],
        seq=row["seq"],
        position=row["position"],
        role=Role(row["role"]),
        content=row["content"],
        tool_calls=row["tool_calls"],
        tool_call_id=row["tool_call_id"],
        name=row["name"],
        created_at=row["created_at"],
    )


_COLUMNS = (
    "id, seq, position, role, content, tool_calls, tool_call_id, name, created_at"
)


class SqliteCompactionArchive:
    """What compaction removed from one database's conversations.

    :param database: Open database to read and write through.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def next_seq(self, session_id: str) -> int:
        """The compaction number the next archived batch should carry.

        One per compaction, not per message, so a session's rows group
        into batches the way the conversation's compactions happened.
        Sequence numbers only locate; identity is the
        ``(session_id, content_hash)`` index.
        """
        return await self._db.arun(lambda c: self._next_seq(c, session_id))

    async def append(
        self,
        session_id: str,
        seq: int,
        messages: Sequence[Message],
        *,
        start_position: int,
    ) -> int:
        """Store *messages* as one archived batch.

        ``INSERT OR IGNORE`` against the unique index, so replaying a
        batch — the retried compaction, the process that raced this one
        — writes nothing rather than a second copy. *start_position* is
        where the batch began in the pre-compaction history; positions
        locate, they do not identify, and a batch with an offloaded
        message left out records the positions of what it kept.

        :param session_id: Session the messages belong to.
        :param seq: Which compaction of that session this batch is.
        :param messages: The removed messages, in conversation order.
        :param start_position: Index of the first in the history as it
            was before the compaction.
        :returns: How many rows were actually written.
        """
        batch = tuple(messages)
        return await self._db.arun(
            lambda c: self._append(c, session_id, seq, batch, start_position)
        )

    async def search(
        self, session_id: str, query: str, limit: int = 10
    ) -> list[ArchivedMessage]:
        """Rows matching *query*, best match first.

        The same MATCH expression :meth:`LongTermStore.search
        <omicsclaw.memory.store.LongTermStore.search>` builds: terms
        OR-joined, Han text as adjacent pairs, everything quoted against
        operator syntax. Lexical, therefore — a synonym never matches,
        which is what the quoted-phrase fallback in the tool layer is
        for.

        :param session_id: Session whose archive is searched.
        :param query: Free text to match against message content.
        :param limit: Greatest number of rows to return.
        :returns: Matching rows, ranked by FTS5.
        """
        return await self._db.arun(lambda c: self._search(c, session_id, query, limit))

    async def search_exact(
        self, session_id: str, phrase: str, limit: int = 10
    ) -> list[ArchivedMessage]:
        """Rows whose content contains *phrase* as a substring.

        The exact-match half the lexical index cannot offer: ``LIKE
        '%phrase%'`` finds the phrase whatever stood around it. ``%`` and
        ``_`` in *phrase* keep their wildcard meaning — a phrase
        containing them matches more, never less, and the caller binds
        the value as a parameter, so there is no injection surface.

        :param session_id: Session whose archive is searched.
        :param phrase: Text to look for inside message content.
        :param limit: Greatest number of rows to return.
        :returns: Matching rows, oldest first.
        """
        return await self._db.arun(
            lambda c: self._search_exact(c, session_id, phrase, limit)
        )

    async def read(self, session_id: str, row_id: int) -> ArchivedMessage | None:
        """One row by id, inside *session_id* only.

        :param session_id: Session the row must belong to.
        :param row_id: The id ``search`` reported.
        :returns: The row, or ``None`` if no such row in that session.
        """
        return await self._db.arun(lambda c: self._read(c, session_id, row_id))

    async def purge(self, session_id: str) -> int:
        """Drop every archived row of *session_id*.

        The session is going away; its archive goes with it — the
        read-back half of "deleted with the conversation".

        :param session_id: Session to clear.
        :returns: How many rows were deleted.
        """
        return await self._db.arun(lambda c: self._purge(c, session_id))

    @staticmethod
    def _next_seq(conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM compaction_archives "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    @classmethod
    def _append(
        cls,
        conn: sqlite3.Connection,
        session_id: str,
        seq: int,
        messages: tuple[Message, ...],
        start_position: int,
    ) -> int:
        now = time.time()
        written = 0
        for offset, message in enumerate(messages):
            calls = _dump_tool_calls(message.tool_calls)
            digest = content_hash(message.role.value, message.content, calls)
            cur = conn.execute(
                """INSERT OR IGNORE INTO compaction_archives
                       (session_id, seq, position, role, content, tool_calls,
                        tool_call_id, name, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    seq,
                    start_position + offset,
                    message.role.value,
                    message.content,
                    calls,
                    message.tool_call_id,
                    message.name,
                    digest,
                    now,
                ),
            )
            if not cur.rowcount:
                continue
            # The index row carries the id just assigned, so search joins
            # back to exactly this row; _spaced gives Han content its
            # per-character tokens, as the long-term index does.
            conn.execute(
                "INSERT INTO compaction_archive_fts (id, content) VALUES (?, ?)",
                (cur.lastrowid, _spaced(message.content)),
            )
            written += 1
        return written

    @staticmethod
    def _search(
        conn: sqlite3.Connection, session_id: str, query: str, limit: int
    ) -> list[ArchivedMessage]:
        match = _escape_fts(query)
        if not match:
            return []
        rows = conn.execute(
            f"""SELECT {', '.join('a.' + c.strip() for c in _COLUMNS.split(','))}
                FROM compaction_archive_fts f
                JOIN compaction_archives a ON a.id = f.id
                WHERE compaction_archive_fts MATCH ? AND a.session_id = ?
                ORDER BY rank
                LIMIT ?""",
            (match, session_id, max(limit, 0)),
        ).fetchall()
        return [_row_to_archived(r) for r in rows]

    @staticmethod
    def _search_exact(
        conn: sqlite3.Connection, session_id: str, phrase: str, limit: int
    ) -> list[ArchivedMessage]:
        if not phrase:
            return []
        rows = conn.execute(
            f"""SELECT {_COLUMNS} FROM compaction_archives
                WHERE session_id = ? AND content LIKE ?
                ORDER BY seq, position
                LIMIT ?""",
            (session_id, f"%{phrase}%", max(limit, 0)),
        ).fetchall()
        return [_row_to_archived(r) for r in rows]

    @staticmethod
    def _read(
        conn: sqlite3.Connection, session_id: str, row_id: int
    ) -> ArchivedMessage | None:
        row = conn.execute(
            f"""SELECT {_COLUMNS} FROM compaction_archives
                WHERE session_id = ? AND id = ?""",
            (session_id, row_id),
        ).fetchone()
        return None if row is None else _row_to_archived(row)

    @staticmethod
    def _purge(conn: sqlite3.Connection, session_id: str) -> int:
        doomed = conn.execute(
            "SELECT id FROM compaction_archives WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        for row in doomed:
            conn.execute(
                "DELETE FROM compaction_archive_fts WHERE id = ?", (row["id"],)
            )
        conn.execute(
            "DELETE FROM compaction_archives WHERE session_id = ?", (session_id,)
        )
        return len(doomed)
