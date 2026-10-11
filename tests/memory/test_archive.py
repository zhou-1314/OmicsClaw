"""The compaction archive: session-scoped rows, dedup, FTS, and purge."""

from __future__ import annotations

import asyncio
import json

from omicsclaw.memory import ArchivedMessage, Database, SqliteCompactionArchive
from omicsclaw.schema import Message, Role, ToolCall


def run(coro):
    return asyncio.run(coro)


def archive() -> tuple[Database, SqliteCompactionArchive]:
    db = Database()
    return db, SqliteCompactionArchive(db)


def _batch(*texts: str) -> tuple[Message, ...]:
    return tuple(Message.user(text) for text in texts)


# ---- the table comes with the database ----------------------------------


def test_the_tables_exist_the_moment_a_database_is_open() -> None:
    db, _ = archive()
    names = {
        row[0]
        for row in db.run(
            lambda c: c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        )
    }
    db.close()
    assert "compaction_archives" in names
    assert "compaction_archive_fts" in names
    assert "idx_compaction_archives_dedup" in names


# ---- append and read -----------------------------------------------------


def test_append_then_read_round_trips_every_field() -> None:
    db, ca = archive()
    calls = (
        ToolCall(id="call_1", name="bash", arguments='{"command": "ls -lh"}'),
    )
    message = Message(
        role=Role.TOOL,
        content="data/pbmc3k_raw.h5ad  2.4M",
        tool_calls=calls,
        tool_call_id="call_1",
        name="bash",
    )
    run(ca.append("s1", 1, (message,), start_position=3))
    row = run(ca.read("s1", 1))
    db.close()
    assert isinstance(row, ArchivedMessage)
    assert row.id == 1
    assert row.seq == 1
    assert row.position == 3
    assert row.role is Role.TOOL
    assert row.content == "data/pbmc3k_raw.h5ad  2.4M"
    # The stored bytes are the dumped JSON, not a re-serialisation.
    assert json.loads(row.tool_calls) == [
        {"id": "call_1", "name": "bash", "arguments": '{"command": "ls -lh"}'}
    ]
    assert row.tool_call_id == "call_1"
    assert row.name == "bash"


def test_positions_run_on_from_the_start_position() -> None:
    db, ca = archive()
    run(ca.append("s1", 1, _batch("one", "two", "three"), start_position=7))
    rows = run(ca.search("s1", "two"))
    db.close()
    assert rows[0].position == 8


def test_read_is_scoped_to_the_session() -> None:
    db, ca = archive()
    run(ca.append("s1", 1, _batch("mine"), start_position=0))
    row_id = run(ca.search("s1", "mine"))[0].id
    assert run(ca.read("s2", row_id)) is None
    db.close()


def test_read_of_an_unknown_id_is_none() -> None:
    db, ca = archive()
    assert run(ca.read("s1", 999)) is None
    db.close()


# ---- deduplication -------------------------------------------------------


def test_replaying_a_batch_writes_nothing() -> None:
    """A retried compaction replays the same messages.

    ``(session_id, content_hash)`` folds the replay away at the database
    level, which is what makes the archiver safe to call again after a
    retry — no advisory, no double rows.
    """
    db, ca = archive()
    batch = _batch("the path is /data/raw.h5ad", "second")
    first = run(ca.append("s1", 1, batch, start_position=0))
    second = run(ca.append("s1", 2, batch, start_position=0))
    count = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM compaction_archives").fetchone()[0]
    )
    fts_count = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM compaction_archive_fts").fetchone()[0]
    )
    db.close()
    assert first == 2
    assert second == 0
    assert count == 2
    assert fts_count == 2


def test_identical_content_in_another_session_is_a_different_row() -> None:
    db, ca = archive()
    batch = _batch("same words")
    run(ca.append("s1", 1, batch, start_position=0))
    written = run(ca.append("s2", 1, batch, start_position=0))
    db.close()
    assert written == 1


# ---- search --------------------------------------------------------------


def test_search_finds_english_words() -> None:
    db, ca = archive()
    run(ca.append("s1", 1, _batch("saved to data/pbmc3k_raw.h5ad"), start_position=0))
    rows = run(ca.search("s1", "pbmc3k"))
    db.close()
    assert [r.content for r in rows] == ["saved to data/pbmc3k_raw.h5ad"]


def test_search_matches_han_text_by_adjacent_pairs() -> None:
    db, ca = archive()
    run(
        ca.append(
            "s1", 1, _batch("我们这个项目一律用 leiden 做域识别"), start_position=0
        )
    )
    rows = run(ca.search("s1", "域识别"))
    db.close()
    assert len(rows) == 1


def test_search_is_scoped_to_the_session() -> None:
    db, ca = archive()
    run(ca.append("s1", 1, _batch("needle"), start_position=0))
    rows = run(ca.search("s2", "needle"))
    db.close()
    assert rows == []


def test_search_of_emptiness_answers_nothing() -> None:
    db, ca = archive()
    run(ca.append("s1", 1, _batch("haystack"), start_position=0))
    assert run(ca.search("s1", "   ")) == []
    db.close()


def test_exact_search_finds_a_substring_the_lexicon_cannot() -> None:
    """The quoted-phrase fallback's store half.

    FTS matches terms — and, as a phrase, any run of its tokens — so a
    query must cut a token in half to be beyond it: ``abc123_out/mer``
    is the tokens ``abc123 out mer``, and the content's ``merged`` is
    not the token ``mer``. ``LIKE`` finds the substring either way.
    """
    db, ca = archive()
    run(
        ca.append(
            "s1",
            1,
            _batch("wrote results to /work/abc123_out/merged.h5ad"),
            start_position=0,
        )
    )
    fts_hits = run(ca.search("s1", "abc123_out/mer"))
    like_hits = run(ca.search_exact("s1", "c123_out/mer", 10))
    db.close()
    assert fts_hits == []
    assert [r.content for r in like_hits] == [
        "wrote results to /work/abc123_out/merged.h5ad"
    ]


# ---- next_seq ------------------------------------------------------------


def test_next_seq_starts_at_one_and_follows_the_rows() -> None:
    db, ca = archive()
    assert run(ca.next_seq("s1")) == 1
    run(ca.append("s1", 1, _batch("a"), start_position=0))
    assert run(ca.next_seq("s1")) == 2
    assert run(ca.next_seq("s2")) == 1
    db.close()


# ---- purge ---------------------------------------------------------------


def test_purge_drops_the_rows_and_their_index_entries() -> None:
    db, ca = archive()
    run(ca.append("s1", 1, _batch("one", "two"), start_position=0))
    run(ca.append("s2", 1, _batch("three"), start_position=0))
    removed = run(ca.purge("s1"))
    left = db.run(
        lambda c: c.execute(
            "SELECT session_id, content FROM compaction_archives"
        ).fetchall()
    )
    fts_left = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM compaction_archive_fts").fetchone()[0]
    )
    still_findable = run(ca.search("s2", "three"))
    gone = run(ca.search("s1", "one"))
    db.close()
    assert removed == 2
    assert [(r["session_id"], r["content"]) for r in left] == [("s2", "three")]
    assert fts_left == 1
    assert len(still_findable) == 1
    assert gone == []


def test_deleting_a_session_takes_its_archive_with_it() -> None:
    """The session-scoped half: ``SqliteSessionStore.delete`` cascades."""
    from omicsclaw.memory import SqliteSessionStore, StoredSession

    db = Database()
    ca = SqliteCompactionArchive(db)
    store = SqliteSessionStore(db)
    run(store.save(StoredSession(session_id="s1", history=(Message.user("hello"),))))
    run(ca.append("s1", 1, _batch("archived words"), start_position=0))
    assert run(store.delete("s1")) is True
    rows = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM compaction_archives").fetchone()[0]
    )
    fts = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM compaction_archive_fts").fetchone()[0]
    )
    db.close()
    assert rows == 0
    assert fts == 0
