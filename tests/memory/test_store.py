"""Deduplicated writes, full-text search and the index that backs it."""

from __future__ import annotations

import asyncio
import threading

import pytest

from omicsclaw.memory import Category, Database, LongTermStore, MemoryEntry

DAY = 86400.0


def run(coro):
    return asyncio.run(coro)


def store() -> tuple[Database, LongTermStore]:
    db = Database()
    return db, LongTermStore(db)


def time_after_days(days: float) -> float:
    """A wall clock *days* into the future, for the clock-taking methods."""
    import time

    return time.time() + days * DAY


def test_add_then_get_round_trips_every_field() -> None:
    db, lt = store()
    eid = run(
        lt.add(
            MemoryEntry(
                title="Visium QC",
                content="min_counts=500",
                category=Category.KNOWLEDGE,
                importance=8,
                ttl_days=30,
                tags=("spatial", "qc"),
            )
        )
    )
    got = run(lt.get(eid))
    db.close()
    assert got.title == "Visium QC"
    assert got.content == "min_counts=500"
    assert got.category == "knowledge"
    assert got.importance == 8
    assert got.ttl_days == 30
    assert got.tags == ("spatial", "qc")


def test_get_of_an_unknown_id_is_none() -> None:
    db, lt = store()
    assert run(lt.get("nope")) is None
    db.close()


def test_duplicate_content_merges() -> None:
    """A repeated write updates in place instead of raising.

    Memory writes happen inside the agent loop. Letting a duplicate
    abort the write would fail a whole turn over something the store can
    resolve itself, so the existing row keeps its id and takes the higher
    importance.
    """
    db, lt = store()
    first = run(lt.add(MemoryEntry(title="a", content="min_counts=500",
                                   importance=3)))
    second = run(lt.add(MemoryEntry(title="b", content="MIN_COUNTS=500  ",
                                    importance=9)))
    db_count = db.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM long_term_memories"
        ).fetchone()[0]
    )
    merged = run(lt.get(first))
    db.close()
    assert second == first
    assert db_count == 1
    assert merged.importance == 9


def test_two_connections_may_add_the_same_content_at_once() -> None:
    """Deduplication has to hold across processes, not just threads.

    ``Database`` serialises through a lock it owns, which says nothing
    about a second ``Database`` on the same file — and that is the normal
    deployment: the desktop server, a CLI session and each channel
    process all open ``memory.db``. Two of them extracting the same fact
    in the same moment must not turn a duplicate into a crashed turn.

    The barrier is what makes this deterministic. Without it the two
    workers drift apart within a round or two and the window between the
    lookup and the insert never lines up.

    ``done`` is what stops this passing for the wrong reason. A worker
    that dies early — opening the database, say — leaves the other
    waiting on a barrier nobody joins, and an assertion that only looked
    at ``failures`` would call that a pass without two writers ever
    having raced.
    """
    import tempfile
    from pathlib import Path

    rounds = 40
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "memory.db"
        barrier = threading.Barrier(2)
        failures: list[BaseException] = []
        done = [0, 0]

        def worker(which: int) -> None:
            try:
                db = Database(path)
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
                barrier.abort()
                return
            lt = LongTermStore(db)

            async def drive() -> None:
                for i in range(rounds):
                    try:
                        barrier.wait(timeout=10)
                    except threading.BrokenBarrierError:
                        return
                    try:
                        await lt.add(
                            MemoryEntry(title=f"w{which}", content=f"body-{i}")
                        )
                    except BaseException as exc:  # noqa: BLE001
                        failures.append(exc)
                        barrier.abort()
                        return
                    done[which] += 1

            try:
                asyncio.run(drive())
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(w,)) for w in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not failures, f"raised under contention: {failures[0]!r}"
    assert done == [rounds, rounds], f"workers never raced: {done}"


def test_merging_never_lowers_importance() -> None:
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="a", content="same", importance=9)))
    run(lt.add(MemoryEntry(title="a", content="same", importance=1)))
    got = run(lt.get(eid))
    db.close()
    assert got.importance == 9


def test_add_ignores_the_id_on_the_entry_it_is_given() -> None:
    """Read, edit, add again — the shape the docstring promises to allow.

    Honouring the incoming id makes that sequence collide on the primary
    key, because the edited content no longer matches the fingerprint
    that would have routed it to the merge path.
    """
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="t", content="v1")))
    fetched = run(lt.get(eid))
    fetched.content = "v2"
    second = run(lt.add(fetched))
    both = run(lt.list())
    db.close()
    assert second != eid
    assert sorted(e.content for e in both) == ["v1", "v2"]


def test_search_finds_by_content() -> None:
    db, lt = store()
    run(lt.add(MemoryEntry(title="Visium QC", content="min_counts is 500")))
    run(lt.add(MemoryEntry(title="Clustering", content="leiden resolution 1.0")))
    hits = run(lt.search("leiden"))
    db.close()
    assert [h.title for h in hits] == ["Clustering"]


def test_search_finds_by_title() -> None:
    db, lt = store()
    run(lt.add(MemoryEntry(title="Xenium", content="unrelated body")))
    hits = run(lt.search("Xenium"))
    db.close()
    assert [h.title for h in hits] == ["Xenium"]


def test_a_natural_language_query_still_finds_its_document() -> None:
    """The caller is a model writing a sentence, not a search operator.

    Requiring every term means one word the document happens not to
    contain — "what", "threshold", "for" — takes recall to zero, and
    silently: no error, just an empty result and a model concluding it
    has no memory of something it wrote down. Any term may match;
    ``rank`` decides which document comes first.
    """
    db, lt = store()
    run(lt.add(MemoryEntry(title="Visium QC", content="min_counts is 500")))
    run(lt.add(MemoryEntry(title="Clustering", content="leiden resolution")))

    hits = run(lt.search("what is the min_counts threshold for visium"))
    db.close()
    assert hits, "a sentence-shaped query found nothing"
    assert hits[0].title == "Visium QC"


def test_a_hit_records_that_it_was_used() -> None:
    """Reading an entry back is the only evidence that it earns its place.

    ``updated_at`` deliberately does not move: the entry was consulted,
    not confirmed, so its TTL keeps running.
    """
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="t", content="leiden resolution")))
    before = run(lt.get(eid)).updated_at
    hits = run(lt.search("leiden"))
    after = run(lt.get(eid))
    db.close()
    assert [h.id for h in hits] == [eid]
    assert after.use_count == 1
    assert after.last_used_at is not None
    assert after.updated_at == before


def test_searching_for_an_entry_stops_it_looking_stale() -> None:
    """The condition ``stale_candidates`` rests on has to be reachable.

    It proposes entries with ``use_count = 0``. If nothing ever raises
    that count, the condition is always true and the set degenerates into
    "every low-rated entry", which is a deletion list nobody can trust.
    Search is what moves it, so the two belong in one test.
    """
    db, lt = store()
    run(lt.add(MemoryEntry(title="obscure", content="tangram deconvolution",
                           importance=0)))
    stale_before = run(lt.stale_candidates(now=time_after_days(61)))

    run(lt.search("tangram"))

    stale_after = run(lt.stale_candidates(now=time_after_days(61)))
    db.close()
    assert [e.title for e in stale_before] == ["obscure"]
    assert stale_after == []


def test_search_sees_updated_content() -> None:
    """The index is synchronised by the writer, not by a trigger.

    ``long_term_memories`` is the source of truth and ``memories_fts`` is
    a standalone index, so an update that skipped the reindex would leave
    search answering from the old text. A test that only writes and then
    searches cannot tell the difference.
    """
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="thresholds", content="min_counts=500")))
    run(lt.update(eid, content="min_counts=1000"))
    stale = run(lt.search("500"))
    fresh = run(lt.search("1000"))
    db.close()
    assert stale == []
    assert [h.id for h in fresh] == [eid]


def test_update_rewrites_the_fingerprint() -> None:
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="t", content="original")))
    run(lt.update(eid, content="rewritten"))
    reused = run(lt.add(MemoryEntry(title="t2", content="original")))
    db.close()
    assert reused != eid


def test_editing_an_entry_into_a_duplicate_merges_the_two() -> None:
    """Editing must not be the one path where a duplicate raises.

    ``add`` folds a repeat into the entry that already holds that
    content, so an edit that lands on existing content has to converge
    too — otherwise the store answers the same question two ways and a
    memory-editing tool crashes the turn the moment a user rewrites one
    note into another. The entry being edited is the one that survives:
    it is the id the caller is holding.
    """
    db, lt = store()
    edited = run(lt.add(MemoryEntry(title="a", content="alpha", importance=3)))
    other = run(lt.add(MemoryEntry(title="b", content="beta", importance=9)))

    assert run(lt.update(edited, content="beta")) is True

    survivor = run(lt.get(edited))
    absorbed = run(lt.get(other))
    remaining = run(lt.list())
    found = run(lt.search("beta"))
    db.close()
    assert survivor.content == "beta"
    assert survivor.importance == 9
    assert absorbed is None
    assert [e.id for e in remaining] == [edited]
    assert [e.id for e in found] == [edited]


def test_update_rejects_an_unknown_field() -> None:
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="t", content="c")))
    with pytest.raises(ValueError, match="use_count"):
        run(lt.update(eid, use_count=99))
    db.close()


def test_update_of_an_unknown_id_reports_false() -> None:
    db, lt = store()
    assert run(lt.update("nope", title="x")) is False
    db.close()


def test_list_is_ordered_by_importance() -> None:
    db, lt = store()
    run(lt.add(MemoryEntry(title="low", content="a", importance=1)))
    run(lt.add(MemoryEntry(title="high", content="b", importance=9)))
    run(lt.add(MemoryEntry(title="mid", content="c", importance=5)))
    got = run(lt.list())
    db.close()
    assert [e.title for e in got] == ["high", "mid", "low"]


def test_touch_counts_the_use_without_deferring_expiry() -> None:
    """Reading a memory is not evidence that it is still true.

    ``use_count`` and ``last_used_at`` move; ``updated_at`` does not, so
    a stale entry that keeps being read still ages out on schedule.
    """
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="t", content="c", ttl_days=7)))
    before = run(lt.get(eid)).updated_at
    assert run(lt.touch(eid)) is True
    after = run(lt.get(eid))
    db.close()
    assert after.use_count == 1
    assert after.last_used_at is not None
    assert after.updated_at == before


def test_soft_deleted_entries_leave_list_and_search() -> None:
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="gone", content="leiden resolution")))
    assert run(lt.soft_delete(eid)) is True
    listed = run(lt.list())
    found = run(lt.search("leiden"))
    still_there = run(lt.get(eid))
    db.close()
    assert listed == []
    assert found == []
    assert still_there is not None and still_there.disabled is True


def test_forgetting_an_entry_survives_re_learning_the_same_thing() -> None:
    """"Forget this" has to outlast the next extraction of the same fact.

    A soft-deleted row keeps its content, so it also keeps the
    fingerprint that routes a later ``add`` into the merge path — and
    that path clears ``disabled``. The entry the user asked to forget
    comes back, wearing its old title, and nothing reports that it
    happened. Releasing the fingerprint on delete is what separates
    "forgotten" from "temporarily hidden".
    """
    db, lt = store()
    forgotten = run(
        lt.add(MemoryEntry(title="OLD", content="same body", importance=2))
    )
    run(lt.soft_delete(forgotten))

    relearned = run(
        lt.add(MemoryEntry(title="NEW", content="same body", importance=7))
    )
    old = run(lt.get(forgotten))
    new = run(lt.get(relearned))
    found = run(lt.search("same body"))
    db.close()
    assert relearned != forgotten
    assert old.disabled is True
    assert old.title == "OLD"
    assert new.title == "NEW"
    assert [e.id for e in found] == [relearned]


def test_expired_entries_leave_list_and_search() -> None:
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="old", content="leiden", ttl_days=1)))
    db.run(
        lambda c: c.execute(
            "UPDATE long_term_memories SET updated_at = ? WHERE id = ?",
            (0.0, eid),
        )
    )
    listed = run(lt.list())
    found = run(lt.search("leiden"))
    db.close()
    assert listed == []
    assert found == []


def test_stale_candidates_are_low_value_unused_and_old() -> None:
    """All three conditions, not any of them.

    The point of the set is that it is safe to review: an entry nobody
    has read, nobody rated, and nobody has rewritten in two months. Drop
    any one condition and it starts proposing memories that are merely
    old, or merely unrated, for deletion.
    """
    db, lt = store()
    forgotten = run(lt.add(MemoryEntry(title="forgotten", content="a",
                                       importance=1)))
    valued = run(lt.add(MemoryEntry(title="valued", content="b",
                                    importance=8)))
    consulted = run(lt.add(MemoryEntry(title="consulted", content="c",
                                       importance=1)))
    run(lt.touch(consulted))
    dropped = run(lt.add(MemoryEntry(title="dropped", content="d",
                                     importance=0)))
    run(lt.soft_delete(dropped))

    later = run(lt.stale_candidates(now=time_after_days(61)))
    db.close()
    assert [e.id for e in later] == [forgotten]
    assert valued and consulted and dropped


def test_nothing_is_stale_before_the_window_closes() -> None:
    db, lt = store()
    run(lt.add(MemoryEntry(title="recent", content="a", importance=0)))
    early = run(lt.stale_candidates(now=time_after_days(59)))
    late = run(lt.stale_candidates(now=time_after_days(61)))
    db.close()
    assert early == []
    assert [e.title for e in late] == ["recent"]


def test_purge_expired_deletes_only_the_expired() -> None:
    db, lt = store()
    keep = run(lt.add(MemoryEntry(title="keep", content="fresh", ttl_days=30)))
    forever = run(lt.add(MemoryEntry(title="forever", content="eternal")))
    doomed = run(lt.add(MemoryEntry(title="doomed", content="stale",
                                    ttl_days=1)))
    db.run(
        lambda c: c.execute(
            "UPDATE long_term_memories SET updated_at = ? WHERE id = ?",
            (0.0, doomed),
        )
    )
    removed = run(lt.purge_expired())
    gone = run(lt.get(doomed))
    kept = run(lt.get(keep))
    eternal = run(lt.get(forever))
    db.close()
    assert removed == 1
    assert gone is None
    assert kept is not None
    assert eternal is not None


def test_purge_drops_the_index_row_too() -> None:
    db, lt = store()
    doomed = run(lt.add(MemoryEntry(title="doomed", content="leiden",
                                    ttl_days=1)))
    db.run(
        lambda c: c.execute(
            "UPDATE long_term_memories SET updated_at = ? WHERE id = ?",
            (0.0, doomed),
        )
    )
    run(lt.purge_expired())
    orphans = db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
    )
    db.close()
    assert orphans == 0


def test_search_treats_operator_characters_as_literals() -> None:
    """A query is text, not FTS5 syntax.

    ``NOT``, a bare ``-`` or an unbalanced quote reaching MATCH raw is a
    syntax error, which surfaces as a crashed turn rather than a result.
    Quoting every term also fixes the semantics at AND-of-terms, so the
    second query here finds nothing because the document lacks ``1000``,
    not because the query blew up.
    """
    db, lt = store()
    run(lt.add(MemoryEntry(title="t", content="min_counts=500")))
    # Raw, every one of these is an FTS5 syntax error rather than a
    # search. Quoted, each is merely a query that may or may not match —
    # which of the two is not the point; not raising is.
    #
    # The NUL cases are the ones quoting alone does not cover: SQLite
    # reads a parameter as a C string, so an embedded NUL truncates the
    # MATCH expression mid-token and the quote never gets closed. They
    # arrive from clipboard pastes, filenames and model output.
    hostile_queries = (
        '" NOT',
        "min_counts -",
        "*",
        "AND OR",
        'unbalanced"',
        "leiden\x00",
        "\x00",
        "min\x00counts",
    )
    for hostile in hostile_queries:
        assert isinstance(run(lt.search(hostile)), list), repr(hostile)
    # A term that is only special before quoting still finds its document.
    assert [h.title for h in run(lt.search("min_counts=500"))] == ["t"]
    db.close()


def test_blank_search_returns_nothing() -> None:
    db, lt = store()
    run(lt.add(MemoryEntry(title="t", content="c")))
    assert run(lt.search("   ")) == []
    db.close()


def test_latin_terms_inside_chinese_content_are_searchable() -> None:
    """Whitespace still separates, so Latin words in a Chinese note match."""
    db, lt = store()
    run(lt.add(MemoryEntry(title="空间域", content="域识别默认用 leiden 方法")))
    hits = run(lt.search("leiden"))
    db.close()
    assert [h.title for h in hits] == ["空间域"]


NOTES = (
    ("空间域识别", "这个项目的空间域识别一律用 leiden"),
    ("批次校正", "批次效应用 harmony 校正，不要用 combat"),
    ("QC 阈值", "QC 时 min_counts=500，线粒体比例上限 20%"),
    ("细胞注释", "细胞类型注释先跑 CellTypist，再人工核对 marker 基因"),
    ("差异表达", "DE 分析默认用 wilcoxon，pseudobulk 时改用 DESeq2"),
    ("聚类分辨率", "聚类分辨率从 0.5 开始试，用户偏好较粗的分群"),
    ("数据路径", "原始数据放在 /data/visium/mouse_brain，10x平台"),
    ("参考基因组", "STAR index 建在 /ref/mm10，注释用 GENCODE vM25"),
    ("去批次后的评估", "去批次以后用 LISI 和 kBET 评估混合程度"),
    ("Visium QC", "min_counts is 500 for the mouse brain sections"),
    ("Clustering", "leiden resolution 1.0 worked best; louvain was unstable"),
    ("Model choice", "the deconvolution model is cell2location"),
)
"""Notes of the kind a Chinese-speaking omics user leaves behind."""


def noted() -> tuple[Database, LongTermStore]:
    db, lt = store()
    for title, content in NOTES:
        run(lt.add(MemoryEntry(title=title, content=content)))
    return db, lt


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Two-character Han words, the commonest Chinese query.
        ("聚类", {"聚类分辨率"}),
        ("批次", {"批次校正", "去批次后的评估"}),
        ("注释", {"细胞注释", "参考基因组"}),
        # Longer substrings of a note.
        ("域识别", {"空间域识别"}),
        ("这个项目", {"空间域识别"}),
        ("线粒体比例", {"QC 阈值"}),
        # One Han character.
        ("域", {"空间域识别"}),
        # Two-letter abbreviations match as words, not inside "leiden"
        # or "model".
        ("QC", {"QC 阈值", "Visium QC"}),
        ("DE", {"差异表达"}),
        # English that matched before the index was respaced.
        ("leiden", {"空间域识别", "Clustering"}),
        ("min_counts=500", {"QC 阈值"}),
        ("cell2location", {"Model choice"}),
        ("mouse brain", {"Visium QC", "数据路径"}),
    ],
)
def test_search_finds_exactly_the_notes_that_hold_the_query(
    query: str, expected: set[str]
) -> None:
    db, lt = noted()
    hits = run(lt.search(query, limit=len(NOTES)))
    db.close()
    assert {h.title for h in hits} == expected


@pytest.mark.parametrize(
    ("query", "best"),
    [
        # Han and Latin in one term.
        ("leiden聚类", {"聚类分辨率", "空间域识别", "Clustering"}),
        ("10x平台", {"数据路径"}),
        # Several terms in two scripts.
        ("空间 聚类 leiden", {"空间域识别", "聚类分辨率", "Clustering"}),
        ("QC 阈值 min_counts", {"QC 阈值", "Visium QC"}),
        # Phrases a model writes that no note holds word for word.
        ("空间域识别方法", {"空间域识别"}),
        ("批次校正方法", {"批次校正"}),
        ("这个项目用什么方法做空间域识别", {"空间域识别"}),
        # A sentence in English, as before.
        ("what is the min_counts threshold for visium", {"Visium QC"}),
    ],
)
def test_search_ranks_the_notes_nearest_the_query_first(
    query: str, best: set[str]
) -> None:
    """Any shared word or Han pair matches, so the order carries the answer."""
    db, lt = noted()
    hits = run(lt.search(query, limit=len(NOTES)))
    db.close()
    assert {h.title for h in hits[: len(best)]} == best


def test_a_note_sharing_more_of_a_han_query_ranks_higher() -> None:
    db, lt = store()
    run(lt.add(MemoryEntry(title="part", content="去批次以后再评估")))
    run(lt.add(MemoryEntry(title="whole", content="批次校正用 harmony")))
    hits = run(lt.search("批次校正"))
    db.close()
    assert [h.title for h in hits] == ["whole", "part"]


def test_latin_words_touching_han_text_are_searchable() -> None:
    """Without a space between them, the two scripts used to be one token."""
    db, lt = store()
    run(lt.add(MemoryEntry(title="域识别", content="用leiden聚类，取前30个主成分")))
    by_word = run(lt.search("leiden"))
    by_number = run(lt.search("30"))
    by_han = run(lt.search("主成分"))
    db.close()
    assert [h.title for h in by_word] == ["域识别"]
    assert [h.title for h in by_number] == ["域识别"]
    assert [h.title for h in by_han] == ["域识别"]


def test_a_hit_carries_the_text_as_it_was_written() -> None:
    """The spaced form stays inside the index."""
    db, lt = store()
    run(lt.add(MemoryEntry(title="空间域", content="域识别默认用leiden方法")))
    (hit,) = run(lt.search("域识别"))
    db.close()
    assert hit.title == "空间域"
    assert hit.content == "域识别默认用leiden方法"


def test_search_sees_updated_han_content() -> None:
    db, lt = store()
    eid = run(lt.add(MemoryEntry(title="聚类", content="分辨率用 0.5")))
    run(lt.update(eid, content="改用层次聚类"))
    stale = run(lt.search("分辨率"))
    fresh = run(lt.search("层次"))
    db.close()
    assert stale == []
    assert [h.id for h in fresh] == [eid]


def test_han_queries_treat_operator_characters_as_literals() -> None:
    """Splitting a term into pairs must not let FTS5 syntax through."""
    db, lt = store()
    run(lt.add(MemoryEntry(title="聚类", content="聚类分辨率从 0.5 开始试")))
    hostile_queries = (
        '聚类"',
        '"聚类"',
        '聚"类',
        "聚类*",
        "聚类 NOT 分辨率",
        "(聚类 OR",
        "聚类\x00分辨率",
        "聚类，分辨率",
        "title:聚类",
        "^聚类",
        "聚类 NEAR(分辨率)",
        "-聚类",
        "聚类+分辨率",
    )
    for hostile in hostile_queries:
        hits = run(lt.search(hostile))
        assert [h.title for h in hits] == ["聚类"], repr(hostile)
    db.close()


@pytest.mark.parametrize("query", ['"', "，", "。、", "* -", "()"])
def test_a_query_of_punctuation_alone_finds_nothing(query: str) -> None:
    db, lt = noted()
    assert run(lt.search(query)) == []
    db.close()


def test_a_very_long_han_query_is_still_a_search() -> None:
    """A pasted paragraph becomes thousands of pairs, and still runs."""
    db, lt = noted()
    paragraph = "这个项目的空间域识别一律用什么方法" * 300
    hits = run(lt.search(paragraph))
    db.close()
    assert hits[0].title == "空间域识别"


@pytest.mark.parametrize(
    ("written", "indexed"),
    [
        ("这个项目的空间域识别一律用 leiden", "这 个 项 目 的 空 间 域 识 别 一 律 用 leiden"),
        ("用leiden聚类", "用 leiden 聚 类"),
        ("前30个主成分", "前 30 个 主 成 分"),
        ("批次，校正", "批 次 ， 校 正"),
        ("域", "域"),
        ("𠀀𠀁x", "𠀀 𠀁 x"),
        ("min_counts is 500; café, naïve", "min_counts is 500; café, naïve"),
        ("", ""),
    ],
)
def test_spacing_sets_each_han_character_apart(written: str, indexed: str) -> None:
    from omicsclaw.memory.store import _spaced

    assert _spaced(written) == indexed
    assert _spaced(indexed) == indexed, "spacing a spaced text must change nothing"
