"""Reading and writing long-term memories."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Sequence

from .database import Database
from .longterm import Category, MemoryEntry, signature

_COLUMNS = (
    "id, title, content, category, importance, signature, created_at, "
    "updated_at, last_used_at, use_count, ttl_days, disabled, tags"
)

_SECONDS_PER_DAY = 86400.0

STALE_AFTER_DAYS = 60.0
"""Days without a rewrite before an unused entry may be proposed."""

STALE_MAX_IMPORTANCE = 1
"""Highest rating an entry can carry and still count as low value."""


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    """Rebuild an entry from one ``long_term_memories`` row."""
    return MemoryEntry(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        category=row["category"],
        importance=row["importance"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
        use_count=row["use_count"],
        ttl_days=row["ttl_days"] or 0,
        disabled=bool(row["disabled"]),
        tags=tuple(json.loads(row["tags"])) if row["tags"] else (),
    )


_HAN = (
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\U00020000-\U0003ffff"  # Extension B and later
)
"""The Han characters, as the inside of a regular-expression class."""

_HAN_RUN = re.compile(f"([{_HAN}]+)")
_HAN_EDGE = re.compile(f"(?<=[{_HAN}])(?=\\S)|(?<=\\S)(?=[{_HAN}])")


def _spaced(text: str) -> str:
    """*text* with a space between each Han character and its neighbours.

    The index's tokenizer splits on spaces and punctuation, so an
    unbroken run of Han characters, together with any letters or digits
    touching it, is one token and only matches a query for the whole
    run. Spaced apart, every Han character is a token of its own and a
    phrase of them matches wherever those characters stand side by side.

    Text with no Han characters comes back unchanged, and so does text
    that is already spaced.

    :param text: A title or a body as it was written.
    :returns: The form of *text* the index stores.
    """
    return _HAN_EDGE.sub(" ", text)


def _escape_fts(query: str) -> str:
    """Turn *query* into a MATCH expression FTS5 reads as literals.

    Quoting stops ``-``, ``*``, ``NOT`` and a stray quote being taken as
    operator syntax. NUL is stripped rather than quoted: SQLite reads the
    parameter as a C string, so an embedded NUL would cut the expression
    short and leave the quote open.

    Terms are joined with ``OR``, not the bare space that would mean
    ``AND``: queries arrive as sentences, and requiring every word
    reduces a question to no results whenever one of its words is absent
    from the entry. Ranking, not filtering, is what puts the best match
    on top.

    Han text has no spaces to split on, so a run of Han characters is
    searched as its adjacent pairs, each a two-character phrase, and a
    run of one character as that character. An entry that shares any
    pair with the query matches, and the more pairs it shares the higher
    it ranks. Letters and digits touching a run are a term of their own.

    :param query: Raw search text.
    :returns: A MATCH expression, or ``""`` when there is nothing to
        search for.
    """
    cleaned = query.replace("\x00", " ")
    phrases: list[str] = []
    for term in cleaned.split():
        # A split on a capturing pattern alternates: text between runs
        # at even positions, the runs themselves at odd ones.
        for position, piece in enumerate(_HAN_RUN.split(term)):
            if not piece:
                continue
            if position % 2 == 0 or len(piece) == 1:
                phrases.append(piece)
            else:
                phrases.extend(f"{a} {b}" for a, b in zip(piece, piece[1:]))
    return " OR ".join('"' + p.replace('"', '""') + '"' for p in phrases)


class LongTermStore:
    """Cross-session memories, deduplicated and full-text searchable.

    ``long_term_memories`` is the source of truth; the ``memories_fts``
    index is kept in step with it by every method that writes.

    The index holds each title and body in the form :func:`_spaced`
    gives it. Building a store reads the whole index once and rewrites
    the rows that are not in that form, which is how a database written
    by a version that indexed the text as written becomes searchable by
    Han substring. A database with no such rows is only read.

    :param database: Open database to read and write through.
    :raises sqlite3.Error: If the index rows cannot be rewritten. The
        rewrite is one transaction, so the index is then as it was.
    """

    def __init__(self, database: Database) -> None:
        self._db = database
        database.run(self._respace_index)

    async def add(self, entry: MemoryEntry) -> str:
        """Store *entry*, merging into any entry with the same content.

        A duplicate does not raise: the existing entry keeps its id, takes
        the higher of the two importances and has its ``updated_at``
        refreshed.

        :param entry: Entry to store; its ``id`` is ignored.
        :returns: The id of the stored or merged entry.
        """
        return await self._db.arun(lambda c: self._add(c, entry))

    async def get(self, entry_id: str) -> MemoryEntry | None:
        """One entry by id.

        :param entry_id: Identifier to look up.
        :returns: The entry, or ``None`` if there is no such id.
        """
        return await self._db.arun(lambda c: self._get(c, entry_id))

    async def update(self, entry_id: str, **changes: object) -> bool:
        """Change fields of one entry and reindex it.

        :param entry_id: Identifier to update.
        :param changes: Any of ``title``, ``content``, ``category``,
            ``importance``, ``ttl_days`` or ``tags``.
        :returns: ``True`` if an entry was updated.
        :raises ValueError: If *changes* names a field that cannot be
            updated.
        """
        return await self._db.arun(lambda c: self._update(c, entry_id, changes))

    async def search(self, query: str, limit: int = 10) -> Sequence[MemoryEntry]:
        """Entries matching *query*, best match first.

        Disabled and expired entries are left out. An entry matches when
        it holds any word of *query*. Han text has no word breaks, so it
        matches by any two adjacent characters of the query, and a Han
        character standing alone matches by itself.

        **This writes.** Every hit has its ``use_count`` raised and its
        ``last_used_at`` set, which is what tells
        :meth:`stale_candidates` an entry is being read. ``updated_at``
        is left alone, so a hit does not defer expiry.

        :param query: Free text to match against titles and content.
        :param limit: Greatest number of entries to return.
        :returns: Matching entries, ranked by FTS5, with the counts they
            carry after this call.
        """
        return await self._db.arun(lambda c: self._search(c, query, limit))

    async def list(self, limit: int = 30) -> Sequence[MemoryEntry]:
        """The highest-value entries, most important first.

        Disabled and expired entries are left out. Ties are broken by
        recency of update.

        :param limit: Greatest number of entries to return.
        :returns: Entries ordered by importance then update time.
        """
        return await self._db.arun(lambda c: self._list(c, limit))

    async def touch(self, entry_id: str) -> bool:
        """Record that an entry was used.

        Increments ``use_count`` and sets ``last_used_at``, without moving
        ``updated_at`` — using a memory does not defer its expiry.

        :param entry_id: Identifier to touch.
        :returns: ``True`` if an entry was touched.
        """
        return await self._db.arun(lambda c: self._touch(c, entry_id))

    async def soft_delete(self, entry_id: str) -> bool:
        """Mark one entry disabled and drop it from the search index.

        :param entry_id: Identifier to disable.
        :returns: ``True`` if an entry was disabled.
        """
        return await self._db.arun(lambda c: self._soft_delete(c, entry_id))

    async def stale_candidates(
        self,
        *,
        now: float | None = None,
        max_importance: int = STALE_MAX_IMPORTANCE,
        after_days: float = STALE_AFTER_DAYS,
    ) -> Sequence[MemoryEntry]:
        """Entries worth proposing for review, oldest first.

        An entry qualifies only by meeting every condition at once: rated
        no higher than *max_importance*, never read back, and not
        rewritten within *after_days*. Nothing is deleted here — this
        answers a question, and the caller decides.

        :param now: Epoch seconds to judge against; defaults to the
            current time.
        :param max_importance: Highest rating still considered low value.
        :param after_days: Days since the last rewrite before an entry is
            old enough to propose.
        :returns: Matching entries, least recently updated first.
        """
        return await self._db.arun(
            lambda c: self._stale(c, now, max_importance, after_days)
        )

    async def purge_expired(self, now: float | None = None) -> int:
        """Delete every entry past its TTL.

        :param now: Epoch seconds to judge against; defaults to the
            current time.
        :returns: How many entries were deleted.
        """
        return await self._db.arun(lambda c: self._purge(c, now))

    @staticmethod
    def _index(conn: sqlite3.Connection, entry_id: str, title: str,
               content: str) -> None:
        """Add an index row for one entry, its text in the spaced form."""
        conn.execute(
            "INSERT INTO memories_fts (id, title, content) VALUES (?, ?, ?)",
            (entry_id, _spaced(title), _spaced(content)),
        )

    @classmethod
    def _reindex(cls, conn: sqlite3.Connection, entry_id: str, title: str,
                 content: str) -> None:
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (entry_id,))
        cls._index(conn, entry_id, title, content)

    @staticmethod
    def _unspaced_rows(conn: sqlite3.Connection) -> list[tuple[int, str]]:
        """The index rows whose text is not in its spaced form.

        :returns: The ``rowid`` and the entry id of each such row.
        """
        return [
            (row["rowid"], row["id"])
            for row in conn.execute(
                "SELECT rowid, id, title, content FROM memories_fts"
            )
            if _spaced(row["title"]) != row["title"]
            or _spaced(row["content"]) != row["content"]
        ]

    @classmethod
    def _respace_index(cls, conn: sqlite3.Connection) -> int:
        """Rewrite every index row whose text is not in its spaced form.

        Each such row is rebuilt from its entry in ``long_term_memories``,
        or dropped when that entry is gone or disabled. Nothing is
        written when no row needs it, so calling this again is a read.

        :param conn: Connection to work on; the caller commits.
        :returns: How many index rows were rewritten or dropped.
        """
        if not cls._unspaced_rows(conn):
            return 0
        # Another connection may be rewriting the same rows. Reading them
        # again under the write lock leaves this one only the rows that
        # are still stale, and each is indexed as its entry stands when
        # this transaction commits.
        conn.execute("BEGIN IMMEDIATE")
        stale = cls._unspaced_rows(conn)
        for rowid, entry_id in stale:
            entry = conn.execute(
                """SELECT title, content FROM long_term_memories
                   WHERE id = ? AND disabled = 0""",
                (entry_id,),
            ).fetchone()
            # By rowid: ``id`` is not indexed, and a scan per row would
            # make this quadratic in the number of rows to rewrite.
            conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
            if entry is not None:
                cls._index(conn, entry_id, entry["title"], entry["content"])
        return len(stale)

    @classmethod
    def _add(cls, conn: sqlite3.Connection, entry: MemoryEntry) -> str:
        # One statement, so the lookup and the write cannot be separated by
        # another connection on the same file. A SELECT followed by an
        # INSERT is only safe against threads sharing this Database's lock,
        # and the deployment runs several processes against one file.
        now = time.time()
        category = (
            entry.category.value
            if isinstance(entry.category, Category)
            else str(entry.category)
        )
        row = conn.execute(
            f"""INSERT INTO long_term_memories ({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (signature) DO UPDATE SET
                    importance = MAX(
                        long_term_memories.importance, excluded.importance
                    ),
                    updated_at = excluded.updated_at,
                    disabled = 0
                RETURNING id, title, content""",
            (
                uuid.uuid4().hex,
                entry.title,
                entry.content,
                category,
                entry.importance,
                signature(entry.content),
                entry.created_at or now,
                now,
                entry.last_used_at,
                entry.use_count,
                entry.ttl_days or None,
                int(entry.disabled),
                json.dumps(list(entry.tags), ensure_ascii=False)
                if entry.tags
                else "",
            ),
        ).fetchone()
        cls._reindex(conn, row["id"], row["title"], row["content"])
        return str(row["id"])

    @staticmethod
    def _get(conn: sqlite3.Connection, entry_id: str) -> MemoryEntry | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM long_term_memories WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return None if row is None else _row_to_entry(row)

    _UPDATABLE = frozenset(
        {"title", "content", "category", "importance", "ttl_days", "tags"}
    )

    @classmethod
    def _update(cls, conn: sqlite3.Connection, entry_id: str,
                changes: dict[str, object]) -> bool:
        unknown = set(changes) - cls._UPDATABLE
        if unknown:
            raise ValueError(f"cannot update: {', '.join(sorted(unknown))}")
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM long_term_memories WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return False

        title = str(changes.get("title", row["title"]))
        content = str(changes.get("content", row["content"]))
        category = changes.get("category", row["category"])
        category = (
            category.value if isinstance(category, Category) else str(category)
        )
        raw_importance = changes.get("importance", row["importance"])
        raw_ttl = changes.get("ttl_days", row["ttl_days"] or 0)
        importance = int(raw_importance)  # type: ignore[arg-type]
        ttl_days = int(raw_ttl)  # type: ignore[arg-type]
        tags = changes.get("tags")
        encoded_tags = (
            json.dumps(list(tags), ensure_ascii=False)  # type: ignore[arg-type]
            if tags
            else row["tags"]
        )
        # An edit that lands on content another entry already holds folds
        # the two together rather than tripping the UNIQUE constraint, so
        # that editing answers duplicates the same way adding does. The
        # entry being edited survives because it is the id the caller
        # holds; the one it absorbs contributes its importance.
        sig = signature(content)
        twin = conn.execute(
            """SELECT id, importance FROM long_term_memories
               WHERE signature = ? AND id <> ?""",
            (sig, entry_id),
        ).fetchone()
        if twin is not None:
            importance = max(importance, twin["importance"])
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (twin["id"],))
            conn.execute(
                "DELETE FROM long_term_memories WHERE id = ?", (twin["id"],)
            )
        conn.execute(
            """UPDATE long_term_memories
               SET title = ?, content = ?, category = ?, importance = ?,
                   ttl_days = ?, tags = ?, signature = ?, updated_at = ?
               WHERE id = ?""",
            (
                title,
                content,
                category,
                importance,
                ttl_days or None,
                encoded_tags,
                sig,
                time.time(),
                entry_id,
            ),
        )
        cls._reindex(conn, entry_id, title, content)
        return True

    @staticmethod
    def _live_clause(alias: str = "") -> str:
        p = f"{alias}." if alias else ""
        return (
            f"{p}disabled = 0 AND ({p}ttl_days IS NULL OR {p}ttl_days <= 0 "
            f"OR {p}updated_at + {p}ttl_days * 86400.0 >= ?)"
        )

    @classmethod
    def _search(cls, conn: sqlite3.Connection, query: str,
                limit: int) -> list[MemoryEntry]:
        match = _escape_fts(query)
        if not match:
            return []
        rows = conn.execute(
            f"""SELECT {', '.join('m.' + c.strip() for c in _COLUMNS.split(','))}
                FROM memories_fts f
                JOIN long_term_memories m ON m.id = f.id
                WHERE memories_fts MATCH ? AND {cls._live_clause('m')}
                ORDER BY rank
                LIMIT ?""",
            (match, time.time(), max(limit, 0)),
        ).fetchall()
        found = [_row_to_entry(r) for r in rows]
        if not found:
            return found
        # Reinforce the hits. Without this nothing ever raises use_count,
        # and stale_candidates — which asks for entries nobody has read —
        # would answer with every low-rated entry in the store.
        # updated_at stays put: the entry was consulted, not confirmed.
        moment = time.time()
        conn.executemany(
            """UPDATE long_term_memories
               SET use_count = use_count + 1, last_used_at = ?
               WHERE id = ?""",
            [(moment, e.id) for e in found],
        )
        for entry in found:
            entry.use_count += 1
            entry.last_used_at = moment
        return found

    @classmethod
    def _list(cls, conn: sqlite3.Connection, limit: int) -> list[MemoryEntry]:
        rows = conn.execute(
            f"""SELECT {_COLUMNS} FROM long_term_memories
                WHERE {cls._live_clause()}
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?""",
            (time.time(), max(limit, 0)),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    @staticmethod
    def _touch(conn: sqlite3.Connection, entry_id: str) -> bool:
        cur = conn.execute(
            """UPDATE long_term_memories
               SET use_count = use_count + 1, last_used_at = ?
               WHERE id = ?""",
            (time.time(), entry_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def _soft_delete(conn: sqlite3.Connection, entry_id: str) -> bool:
        # Clearing the fingerprint releases the UNIQUE slot, so learning
        # the same thing again writes a new entry instead of reviving this
        # one through the merge path. The row itself stays for audit.
        cur = conn.execute(
            """UPDATE long_term_memories
               SET disabled = 1, signature = NULL, updated_at = ?
               WHERE id = ?""",
            (time.time(), entry_id),
        )
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (entry_id,))
        return cur.rowcount > 0

    @staticmethod
    def _stale(conn: sqlite3.Connection, now: float | None,
               max_importance: int, after_days: float) -> list[MemoryEntry]:
        moment = time.time() if now is None else now
        cutoff = moment - after_days * _SECONDS_PER_DAY
        rows = conn.execute(
            f"""SELECT {_COLUMNS} FROM long_term_memories
                WHERE disabled = 0 AND importance <= ? AND use_count = 0
                  AND updated_at < ?
                ORDER BY updated_at ASC""",
            (max_importance, cutoff),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    @staticmethod
    def _purge(conn: sqlite3.Connection, now: float | None) -> int:
        moment = time.time() if now is None else now
        doomed = conn.execute(
            """SELECT id FROM long_term_memories
               WHERE ttl_days IS NOT NULL AND ttl_days > 0
                 AND updated_at + ttl_days * 86400.0 < ?""",
            (moment,),
        ).fetchall()
        for row in doomed:
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (row["id"],))
            conn.execute(
                "DELETE FROM long_term_memories WHERE id = ?", (row["id"],)
            )
        return len(doomed)
