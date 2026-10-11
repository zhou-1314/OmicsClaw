"""Session-domain draft state: one session's dead ends, kept findable.

:class:`SessionScratch` holds what a conversation has *tried and ruled
out*, written there by the compaction path and read back by the
augmentor chain when the compaction message that usually carries it is
gone — an emergency truncation drops that message, and the scratch is
what keeps ``tried_solutions`` visible until the next summary restates
it.

Append-only by design: :meth:`merge` only ever adds lines, and the
``(session_id, kind, content)`` unique index folds repeats away. Rows
die with their session — :meth:`purge` runs in the same transaction as
the session store's delete.
"""

from __future__ import annotations

import sqlite3
import time

from .database import Database

__all__ = ["SCRATCH_KIND_TRIED_SOLUTIONS", "SessionScratch"]

SCRATCH_KIND_TRIED_SOLUTIONS = "tried_solutions"
"""The one kind this store exists for: compaction's tried_solutions anchor."""


class SessionScratch:
    """One database's session-scoped scratch rows.

    :param database: Open database to read and write through.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def merge(self, session_id: str, kind: str, text: str) -> int:
        """Add *text*'s non-blank lines to *kind* for *session_id*.

        Lines are the unit on purpose: the source text is a
        ``tried_solutions`` anchor — a bulleted list — and storing one
        row per line makes a replayed merge write nothing and a
        partially-known anchor merge only what is new.

        :param session_id: Session the text belongs to.
        :param kind: Which draft the text is — the store does not
            interpret kinds, but ``tried_solutions`` is the one written
            today.
        :param text: Free text; split on newlines, blank lines dropped.
        :returns: How many rows were actually written.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return await self._db.arun(
            lambda c: self._merge(c, session_id, kind, lines)
        )

    async def read(self, session_id: str, kind: str) -> str:
        """The rows of *kind* for *session_id*, one per line, in order.

        :param session_id: Session to read.
        :param kind: Which draft to read.
        :returns: The lines joined with newlines, or ``""`` when empty.
        """
        return await self._db.arun(lambda c: self._read(c, session_id, kind))

    async def purge(self, session_id: str) -> int:
        """Drop every scratch row of *session_id*.

        :param session_id: Session to clear.
        :returns: How many rows were deleted.
        """
        return await self._db.arun(lambda c: self._purge(c, session_id))

    @staticmethod
    def _merge(
        conn: sqlite3.Connection,
        session_id: str,
        kind: str,
        lines: list[str],
    ) -> int:
        now = time.time()
        written = 0
        for line in lines:
            cur = conn.execute(
                """INSERT OR IGNORE INTO session_scratch
                       (session_id, kind, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, kind, line, now),
            )
            written += cur.rowcount
        return written

    @staticmethod
    def _read(conn: sqlite3.Connection, session_id: str, kind: str) -> str:
        rows = conn.execute(
            """SELECT content FROM session_scratch
               WHERE session_id = ? AND kind = ?
               ORDER BY id""",
            (session_id, kind),
        ).fetchall()
        return "\n".join(row["content"] for row in rows)

    @staticmethod
    def _purge(conn: sqlite3.Connection, session_id: str) -> int:
        cur = conn.execute(
            "DELETE FROM session_scratch WHERE session_id = ?", (session_id,)
        )
        return cur.rowcount
