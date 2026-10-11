"""The session scratch: line-level dedup, read-back, and session-scoped rows."""

from __future__ import annotations

import asyncio

from omicsclaw.memory import (
    SCRATCH_KIND_TRIED_SOLUTIONS,
    Database,
    SessionScratch,
    SqliteSessionStore,
    StoredSession,
)
from omicsclaw.schema import Message


def run(coro):
    return asyncio.run(coro)


def scratch() -> tuple[Database, SessionScratch]:
    db = Database()
    return db, SessionScratch(db)


# ---- merge ----------------------------------------------------------------


def test_merge_stores_one_row_per_non_blank_line() -> None:
    db, ss = scratch()
    written = run(
        ss.merge(
            "s1",
            SCRATCH_KIND_TRIED_SOLUTIONS,
            "- louvain: too slow\n\n- leiden res=2: noisy\n",
        )
    )
    db.close()
    assert written == 2


def test_replaying_a_merge_writes_nothing_new() -> None:
    db, ss = scratch()
    run(ss.merge("s1", SCRATCH_KIND_TRIED_SOLUTIONS, "- louvain: too slow"))
    second = run(
        ss.merge(
            "s1",
            SCRATCH_KIND_TRIED_SOLUTIONS,
            "- louvain: too slow\n- leiden: noisy",
        )
    )
    rows = db.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM session_scratch WHERE session_id = 's1'"
        ).fetchone()[0]
    )
    db.close()
    assert second == 1  # only the new line
    assert rows == 2


def test_identical_text_in_another_session_is_a_different_row() -> None:
    db, ss = scratch()
    run(ss.merge("s1", SCRATCH_KIND_TRIED_SOLUTIONS, "- same line"))
    written = run(ss.merge("s2", SCRATCH_KIND_TRIED_SOLUTIONS, "- same line"))
    db.close()
    assert written == 1


# ---- read -----------------------------------------------------------------


def test_read_returns_the_lines_in_order() -> None:
    db, ss = scratch()
    run(
        ss.merge(
            "s1", SCRATCH_KIND_TRIED_SOLUTIONS, "- first dead end\n- second dead end"
        )
    )
    text = run(ss.read("s1", SCRATCH_KIND_TRIED_SOLUTIONS))
    db.close()
    assert text == "- first dead end\n- second dead end"


def test_read_is_scoped_to_the_session_and_the_kind() -> None:
    db, ss = scratch()
    run(ss.merge("s1", SCRATCH_KIND_TRIED_SOLUTIONS, "- mine"))
    assert run(ss.read("s2", SCRATCH_KIND_TRIED_SOLUTIONS)) == ""
    assert run(ss.read("s1", "something else")) == ""
    db.close()


# ---- purge ----------------------------------------------------------------


def test_purge_drops_one_session_and_only_one() -> None:
    db, ss = scratch()
    run(ss.merge("s1", SCRATCH_KIND_TRIED_SOLUTIONS, "- one\n- two"))
    run(ss.merge("s2", SCRATCH_KIND_TRIED_SOLUTIONS, "- three"))
    removed = run(ss.purge("s1"))
    assert run(ss.read("s1", SCRATCH_KIND_TRIED_SOLUTIONS)) == ""
    assert run(ss.read("s2", SCRATCH_KIND_TRIED_SOLUTIONS)) == "- three"
    db.close()
    assert removed == 2


def test_deleting_a_session_takes_its_scratch_with_it() -> None:
    """The session-scoped half: ``SqliteSessionStore.delete`` cascades."""
    db = Database()
    ss = SessionScratch(db)
    store = SqliteSessionStore(db)
    run(store.save(StoredSession(session_id="s1", history=(Message.user("hi"),))))
    run(ss.merge("s1", SCRATCH_KIND_TRIED_SOLUTIONS, "- gone soon"))
    assert run(store.delete("s1")) is True
    rows = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM session_scratch").fetchone()[0]
    )
    db.close()
    assert rows == 0


# ---- the table ------------------------------------------------------------


def test_the_table_exists_the_moment_a_database_is_open() -> None:
    db, _ = scratch()
    names = {
        row[0]
        for row in db.run(
            lambda c: c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        )
    }
    db.close()
    assert "session_scratch" in names
    assert "idx_scratch_dedup" in names
