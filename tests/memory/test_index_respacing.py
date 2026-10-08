"""A database indexed by an earlier version becomes searchable by Han substring.

Earlier versions wrote each title and body into ``memories_fts`` as it
was written, which left a run of Han characters as one token. The tests
here build such a database with ``legacy_add``, which indexes the way
those versions did, and then call ``respace_index`` on a current store.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid

import pytest

from omicsclaw.memory import Database, LongTermStore, MemoryEntry
from omicsclaw.memory import database as database_module
from omicsclaw.memory.longterm import signature

LEGACY_NOTES = (
    ("空间域识别", "这个项目的空间域识别一律用 leiden"),
    ("批次校正", "批次效应用harmony校正"),
    ("Visium QC", "min_counts is 500 for the mouse brain sections"),
)


def run(coro):
    return asyncio.run(coro)


def legacy_add(db: Database, title: str, content: str) -> str:
    """Store one entry and index it unspaced, as earlier versions did."""
    entry_id = uuid.uuid4().hex
    now = time.time()

    def write(conn: sqlite3.Connection) -> None:
        conn.execute(
            """INSERT INTO long_term_memories
               (id, title, content, signature, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entry_id, title, content, signature(content), now, now),
        )
        conn.execute(
            "INSERT INTO memories_fts (id, title, content) VALUES (?, ?, ?)",
            (entry_id, title, content),
        )

    db.run(write)
    return entry_id


def legacy_database(path=":memory:") -> Database:
    db = Database(path)
    for title, content in LEGACY_NOTES:
        legacy_add(db, title, content)
    return db


def respaced(db: Database) -> LongTermStore:
    """A store over *db* whose index has been brought up to date."""
    lt = LongTermStore(db)
    run(lt.respace_index())
    return lt


def index_rows(db: Database) -> list[tuple[str, str]]:
    return db.run(
        lambda c: [
            (row["title"], row["content"])
            for row in c.execute(
                "SELECT title, content FROM memories_fts ORDER BY rowid"
            )
        ]
    )


def entries(db: Database) -> list[tuple[str, str, str]]:
    return db.run(
        lambda c: [
            tuple(row)
            for row in c.execute(
                "SELECT id, title, content FROM long_term_memories ORDER BY id"
            )
        ]
    )


def rows_holding(db: Database, phrase: str) -> int:
    """How many index rows hold *phrase*, counted without writing anything."""
    return db.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
            (f'"{phrase}"',),
        ).fetchone()[0]
    )


def traced(db: Database, work) -> list[str]:
    """The SQL statements the connection runs while *work* is called."""
    statements: list[str] = []
    db.run(lambda c: c.set_trace_callback(statements.append))
    try:
        work()
    finally:
        db.run(lambda c: c.set_trace_callback(None))
    return statements


def test_respacing_makes_legacy_han_entries_searchable() -> None:
    db = legacy_database()
    lt = LongTermStore(db)
    before = run(lt.search("域识别"))
    rewritten = run(lt.respace_index())
    by_two = run(lt.search("批次"))
    by_three = run(lt.search("域识别"))
    by_glued_word = run(lt.search("harmony"))
    by_english = run(lt.search("mouse brain"))
    db.close()
    assert before == []
    assert rewritten == 2
    assert [h.title for h in by_two] == ["批次校正"]
    assert [h.title for h in by_three] == ["空间域识别"]
    assert [h.title for h in by_glued_word] == ["批次校正"]
    assert [h.title for h in by_english] == ["Visium QC"]


def test_building_a_store_does_not_touch_the_database() -> None:
    """A damaged index cannot stop a store being built.

    Reading the index is left to ``respace_index``, which the caller may
    guard.
    """
    db = legacy_database()
    statements = traced(db, lambda: LongTermStore(db))
    rows = index_rows(db)
    db.close()
    assert statements == []
    assert ("批次校正", "批次效应用harmony校正") in rows


def test_respacing_rewrites_the_index_and_leaves_the_entries_alone() -> None:
    db = legacy_database()
    before = entries(db)
    respaced(db)
    after = entries(db)
    rows = index_rows(db)
    db.close()
    assert after == before
    assert sorted(rows) == sorted(
        [
            ("空 间 域 识 别", "这 个 项 目 的 空 间 域 识 别 一 律 用 leiden"),
            ("批 次 校 正", "批 次 效 应 用 harmony 校 正"),
            ("Visium QC", "min_counts is 500 for the mouse brain sections"),
        ]
    )


def test_a_row_whose_title_alone_holds_han_text_is_respaced() -> None:
    db = Database()
    entry_id = legacy_add(db, "批次校正", "use harmony, never combat")
    lt = LongTermStore(db)
    rewritten = run(lt.respace_index())
    found = run(lt.search("校正"))
    rows = index_rows(db)
    db.close()
    assert rewritten == 1
    assert [h.id for h in found] == [entry_id]
    assert rows == [("批 次 校 正", "use harmony, never combat")]


def test_a_respaced_row_is_built_from_the_entry_not_from_the_old_row() -> None:
    """``long_term_memories`` is the source of truth for the index.

    The old index row here has drifted from its entry. What the entry
    says is what must be searchable afterwards.
    """
    db = Database()
    entry_id = legacy_add(db, "聚类方法", "聚类一律用louvain")
    db.run(
        lambda c: c.execute(
            "UPDATE long_term_memories SET title = ?, content = ? WHERE id = ?",
            ("分群方法", "分群改用leiden", entry_id),
        )
    )
    lt = respaced(db)
    by_entry = run(lt.search("分群"))
    by_old_row = run(lt.search("louvain"))
    rows = index_rows(db)
    db.close()
    assert [h.id for h in by_entry] == [entry_id]
    assert by_old_row == []
    assert rows == [("分 群 方 法", "分 群 改 用 leiden")]


def test_respacing_again_writes_nothing() -> None:
    db = legacy_database()
    lt = LongTermStore(db)
    first = run(lt.respace_index())
    changes = db.run(lambda c: c.total_changes)
    again = [run(lt.respace_index()), run(LongTermStore(db).respace_index())]
    after = db.run(lambda c: c.total_changes)
    db.close()
    assert first == 2
    assert again == [0, 0]
    assert after == changes


def test_a_database_with_no_han_text_is_only_read() -> None:
    db = Database()
    legacy_add(db, "Visium QC", "min_counts is 500")
    legacy_add(db, "Clustering", "leiden resolution 1.0")
    changes = db.run(lambda c: c.total_changes)
    lt = LongTermStore(db)
    statements = traced(db, lambda: run(lt.respace_index()))
    after = db.run(lambda c: c.total_changes)
    db.close()
    assert after == changes
    assert not any(s.startswith("BEGIN") for s in statements), statements


def test_with_nothing_to_respace_another_writer_is_not_waited_for(
    tmp_path, monkeypatch
) -> None:
    """A start beside a busy process must not queue for the write lock.

    The second connection holds the write lock throughout. Reading the
    index needs no write lock, so the call answers at once. Asking for
    one would fail when the shortened busy timeout runs out.
    """
    monkeypatch.setattr(database_module, "BUSY_TIMEOUT_S", 0.2)
    path = tmp_path / "memory.db"
    with Database(path) as seeded:
        respaced_store = respaced(seeded)
        run(respaced_store.add(MemoryEntry(title="降维", content="降维用UMAP")))
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Database(path) as db:
            lt = LongTermStore(db)
            rewritten = run(lt.respace_index())
            found = rows_holding(db, "降 维")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert rewritten == 0
    assert found == 1


def test_a_respacing_that_fails_part_way_changes_nothing(monkeypatch) -> None:
    """One transaction: every row is rewritten, or none is.

    The failure reaches the caller, the index is as it was, and the next
    call does the whole rewrite.
    """
    db = legacy_database()
    before = index_rows(db)
    index = LongTermStore._index
    calls = []

    def fail_on_the_second(conn, entry_id, title, content):
        calls.append(entry_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        index(conn, entry_id, title, content)

    monkeypatch.setattr(LongTermStore, "_index", staticmethod(fail_on_the_second))
    lt = LongTermStore(db)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        run(lt.respace_index())
    after_failure = index_rows(db)
    still_usable = run(lt.search("leiden"))
    monkeypatch.undo()

    retried = run(lt.respace_index())
    recovered = run(lt.search("域识别"))
    db.close()
    assert len(calls) == 2, "the failure must come after one row was rewritten"
    assert after_failure == before
    assert [h.title for h in still_usable] == ["空间域识别"]
    assert retried == 2
    assert [h.title for h in recovered] == ["空间域识别"]


def test_respacing_runs_off_the_event_loop_thread(monkeypatch) -> None:
    """Reading a large index must not hold up whatever else the loop serves."""
    db = legacy_database()
    work = LongTermStore._respace_index.__func__
    threads: list[threading.Thread] = []

    def recording(cls, conn):
        threads.append(threading.current_thread())
        return work(cls, conn)

    monkeypatch.setattr(LongTermStore, "_respace_index", classmethod(recording))

    async def scenario() -> threading.Thread:
        await LongTermStore(db).respace_index()
        return threading.current_thread()

    loop_thread = run(scenario())
    db.close()
    assert len(threads) == 1
    assert threads[0] is not loop_thread


def test_respacing_reads_the_entries_under_the_write_lock() -> None:
    """An entry another connection edits meanwhile must not be indexed stale."""
    db = legacy_database()
    lt = LongTermStore(db)
    statements = traced(db, lambda: run(lt.respace_index()))
    db.close()
    order = [
        "begin" if s.startswith("BEGIN") else "read"
        for s in statements
        if s.startswith("BEGIN") or "FROM long_term_memories" in s
    ]
    # Two of the three legacy notes hold Han text.
    assert order == ["begin", "read", "read"]
    assert "BEGIN IMMEDIATE" in statements


def test_a_stale_index_row_with_no_live_entry_is_dropped() -> None:
    db = legacy_database()
    gone = legacy_add(db, "已删除", "这条记忆已经停用")
    db.run(
        lambda c: c.execute(
            "UPDATE long_term_memories SET disabled = 1 WHERE id = ?", (gone,)
        )
    )
    lt = respaced(db)
    found = run(lt.search("停用"))
    remaining = db.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE id = ?", (gone,)
        ).fetchone()[0]
    )
    db.close()
    assert found == []
    assert remaining == 0


def test_an_entry_an_earlier_version_adds_later_is_respaced_the_next_time(
    tmp_path,
) -> None:
    """Two versions taking turns on one file lose nothing.

    The earlier version neither knows nor needs the spaced form: it adds
    its own rows unspaced, and they become searchable by Han substring
    the next time a current process brings the index up to date.
    """
    path = tmp_path / "memory.db"
    with legacy_database(path) as first:
        lt = respaced(first)
        current = run(lt.add(MemoryEntry(title="降维", content="降维用UMAP")))
        first.run(
            lambda c: c.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at) "
                "VALUES ('s1', 1.0, 1.0)"
            )
        )

    with Database(path) as earlier:
        late = legacy_add(earlier, "细胞注释", "细胞类型注释先跑CellTypist")
        # What the earlier version's own search would send.
        old_style = earlier.run(
            lambda c: [
                row["id"]
                for row in c.execute(
                    'SELECT id FROM memories_fts WHERE memories_fts MATCH \'"UMAP"\''
                )
            ]
        )
        snapshot = entries(earlier)

    with Database(path) as second:
        lt = LongTermStore(second)
        before = run(lt.search("注释"))
        rewritten = run(lt.respace_index())
        found_late = run(lt.search("注释"))
        found_current = run(lt.search("降维"))
        after = entries(second)
        sessions = second.run(
            lambda c: c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        )
        check = second.run(
            lambda c: c.execute("PRAGMA integrity_check").fetchone()[0]
        )

    assert old_style == [current]
    assert before == []
    assert rewritten == 1
    assert [h.id for h in found_late] == [late]
    assert [h.id for h in found_current] == [current]
    assert after == snapshot
    assert len(after) == len(LEGACY_NOTES) + 2
    assert sessions == 1
    assert check == "ok"


def test_two_connections_may_respace_the_same_file_at_once(tmp_path) -> None:
    path = tmp_path / "memory.db"
    with Database(path) as seeded:
        for index in range(200):
            legacy_add(seeded, f"笔记{index}", f"第{index}条：批次效应用harmony校正")
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []
    counts: list[int] = []

    def worker() -> None:
        db = Database(path)
        try:
            barrier.wait(timeout=10)
            lt = respaced(db)
            counts.append(len(run(lt.search("批次", limit=500))))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with Database(path) as db:
        rows = index_rows(db)
    assert not failures, f"raised under contention: {failures[0]!r}"
    assert counts == [200, 200]
    assert len(rows) == 200
    assert all("批 次 效 应 用 harmony 校 正" in content for _, content in rows)


def test_tags_and_counters_survive_respacing() -> None:
    """Only ``memories_fts`` is written; every other column stays as it was."""
    db = Database()
    entry_id = legacy_add(db, "空间域", "域识别默认用leiden方法")
    db.run(
        lambda c: c.execute(
            """UPDATE long_term_memories
               SET importance = 7, use_count = 3, last_used_at = 5.0,
                   ttl_days = 30, tags = ?, category = 'preference'
               WHERE id = ?""",
            (json.dumps(["空间", "聚类"], ensure_ascii=False), entry_id),
        )
    )
    row_before = db.run(
        lambda c: tuple(
            c.execute(
                "SELECT * FROM long_term_memories WHERE id = ?", (entry_id,)
            ).fetchone()
        )
    )
    lt = respaced(db)
    row_after = db.run(
        lambda c: tuple(
            c.execute(
                "SELECT * FROM long_term_memories WHERE id = ?", (entry_id,)
            ).fetchone()
        )
    )
    got = run(lt.get(entry_id))
    db.close()
    assert row_after == row_before
    assert got.tags == ("空间", "聚类")
