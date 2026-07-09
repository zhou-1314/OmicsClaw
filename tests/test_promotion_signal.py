"""P4 (docs/proposals/skill-acquisition-plan.md §P4) — adaptive promotion
signal: after a successful autonomous-analysis run, notice when a similar
goal has already succeeded before in the same thread and proactively
suggest promoting it to a reusable skill.

These tests drive ``_auto_capture_autonomous_run``/``_compute_promotion_suggestion``
in ``omicsclaw.skill.orchestration`` against a real ``CompatMemoryStore``,
mirroring ``tests/test_auto_capture_provenance.py``'s style (no store
mocking), plus pure-function tests for the ``_goal_similarity`` heuristic.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from omicsclaw.memory.compat import CompatMemoryStore
from omicsclaw.skill.orchestration import (
    _auto_capture_autonomous_run,
    _compute_promotion_suggestion,
    _goal_similarity,
    _ordinal,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield store


# --------------------------------------------------------------------------- #
# _goal_similarity — pure Jaccard token-overlap heuristic                      #
# --------------------------------------------------------------------------- #


def test_goal_similarity_scores_a_genuine_reword_high():
    a = "cluster the cells by cell type and annotate"
    b = "Cluster cells by type and annotate."
    assert _goal_similarity(a, b) >= 0.5


def test_goal_similarity_scores_an_unrelated_goal_low():
    a = "cluster the cells by cell type and annotate"
    b = "detect spatially variable genes in the tumor region"
    assert _goal_similarity(a, b) < 0.5


def test_goal_similarity_is_one_for_identical_goals():
    goal = "detect spatially variable genes in the tumor region"
    assert _goal_similarity(goal, goal) == 1.0


def test_goal_similarity_is_zero_for_empty_input():
    assert _goal_similarity("", "cluster the cells") == 0.0
    assert _goal_similarity("cluster the cells", "") == 0.0
    assert _goal_similarity("", "") == 0.0


def test_ordinal_handles_teen_exception():
    assert [_ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 101)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "101st",
    ]


# --------------------------------------------------------------------------- #
# _auto_capture_autonomous_run — records lineage for success AND failure      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_capture_autonomous_run_records_a_success(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    await _auto_capture_autonomous_run(
        sid, "thread-A", "cluster the cells", "run-1", "/tmp/out/run-1", "succeeded"
    )

    recs = await store.get_memories(sid, "autonomous_run", thread_id="thread-A")
    assert len(recs) == 1
    assert recs[0].goal == "cluster the cells"
    assert recs[0].run_id == "run-1"
    assert recs[0].workspace_root == "/tmp/out/run-1"
    assert recs[0].status == "succeeded"
    assert recs[0].raw_status == "succeeded"


@pytest.mark.asyncio
async def test_auto_capture_autonomous_run_records_a_failure(store, monkeypatch):
    """Failures are captured too (status='failed') — only successes count
    toward the promotion threshold, but capturing both avoids a future
    backfill if a richer signal (e.g. "N failures then a success") is added."""
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    await _auto_capture_autonomous_run(
        sid, "thread-A", "cluster the cells", "run-1", "/tmp/out/run-1", "timed_out"
    )

    recs = await store.get_memories(sid, "autonomous_run", thread_id="thread-A")
    assert len(recs) == 1
    assert recs[0].status == "failed"
    assert recs[0].raw_status == "timed_out"


@pytest.mark.asyncio
async def test_auto_capture_autonomous_run_noop_without_memory_store(monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", None, raising=False)
    # Must not raise even though there's no store to write to.
    await _auto_capture_autonomous_run("sid", "thread-A", "goal", "run-1", "/tmp/out", "succeeded")


# --------------------------------------------------------------------------- #
# _compute_promotion_suggestion — threshold, self-exclusion, thread-scoping   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_promotion_suggestion_is_none_below_threshold(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    await _auto_capture_autonomous_run(sid, "t", "cluster the cells by type", "run-1", "/tmp/1", "succeeded")
    suggestion = await _compute_promotion_suggestion(sid, "t", "cluster the cells by type", "run-1", "/tmp/1")
    assert suggestion is None

    await _auto_capture_autonomous_run(sid, "t", "cluster cells by type", "run-2", "/tmp/2", "succeeded")
    suggestion = await _compute_promotion_suggestion(sid, "t", "cluster cells by type", "run-2", "/tmp/2")
    assert suggestion is None  # only 1 PRIOR success so far — below threshold


@pytest.mark.asyncio
async def test_promotion_suggestion_fires_on_third_similar_success(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    await _auto_capture_autonomous_run(sid, "t", "cluster the cells by type", "run-1", "/tmp/1", "succeeded")
    await _auto_capture_autonomous_run(sid, "t", "cluster cells by type", "run-2", "/tmp/2", "succeeded")
    await _auto_capture_autonomous_run(sid, "t", "cluster cells by cell type please", "run-3", "/tmp/3", "succeeded")

    suggestion = await _compute_promotion_suggestion(
        sid, "t", "cluster cells by cell type please", "run-3", "/tmp/3"
    )
    assert suggestion is not None
    assert "3rd time" in suggestion
    assert "/tmp/3" in suggestion
    assert "source_analysis_dir='/tmp/3'" in suggestion
    # Must never suggest the code actually PASS promote_from_latest=True (the
    # mtime-scan path this feature exists to avoid reintroducing) — mentioning
    # the term in passing, to explain why source_analysis_dir is used instead,
    # is fine and expected.
    assert "promote_from_latest=True" not in suggestion


@pytest.mark.asyncio
async def test_promotion_suggestion_excludes_the_current_run_itself(store, monkeypatch):
    """A run that was somehow already captured under its own run_id must not
    double-count itself as one of its own "prior" successes."""
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    await _auto_capture_autonomous_run(sid, "t", "cluster the cells", "run-1", "/tmp/1", "succeeded")
    await _auto_capture_autonomous_run(sid, "t", "cluster the cells", "run-1", "/tmp/1", "succeeded")

    suggestion = await _compute_promotion_suggestion(sid, "t", "cluster the cells", "run-1", "/tmp/1")
    assert suggestion is None  # only itself is on record — 0 genuine priors


@pytest.mark.asyncio
async def test_promotion_suggestion_ignores_dissimilar_goals(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    for i in range(3):
        await _auto_capture_autonomous_run(
            sid, "t", f"detect spatially variable genes run {i}", f"unrelated-{i}", f"/tmp/u{i}", "succeeded"
        )

    suggestion = await _compute_promotion_suggestion(sid, "t", "cluster the cells by type", "run-x", "/tmp/x")
    assert suggestion is None


@pytest.mark.asyncio
async def test_promotion_suggestion_ignores_failed_prior_runs(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    for i in range(3):
        await _auto_capture_autonomous_run(
            sid, "t", "cluster the cells by type", f"failed-{i}", f"/tmp/f{i}", "failed"
        )

    suggestion = await _compute_promotion_suggestion(sid, "t", "cluster the cells by type", "run-x", "/tmp/x")
    assert suggestion is None


@pytest.mark.asyncio
async def test_promotion_suggestion_is_thread_scoped(store, monkeypatch):
    """A similar goal succeeding in a DIFFERENT thread must not count —
    concurrency/isolation safety, matching Bench's per-thread lineage scoping."""
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    for i in range(3):
        await _auto_capture_autonomous_run(
            sid, "other-thread", "cluster the cells by type", f"run-{i}", f"/tmp/{i}", "succeeded"
        )

    suggestion = await _compute_promotion_suggestion(sid, "my-thread", "cluster the cells by type", "run-x", "/tmp/x")
    assert suggestion is None


@pytest.mark.asyncio
async def test_promotion_suggestion_noop_without_memory_store(monkeypatch):
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", None, raising=False)
    suggestion = await _compute_promotion_suggestion("sid", "t", "goal", "run-1", "/tmp/1")
    assert suggestion is None


@pytest.mark.asyncio
async def test_promotion_suggestion_declines_when_thread_id_is_empty(store, monkeypatch):
    """Regression (adversarial codex review): CompatMemoryStore.get_memories
    treats an empty thread_id as "no filter" (by design, for its general
    listing use), which would otherwise let this feature cross-contaminate
    suggestions across UNRELATED threads for any caller with no Bench thread
    context. Without a real thread_id there's no safe scope to count within —
    decline rather than guess, even if plenty of "similar" successes exist
    scattered across other threads."""
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    await _auto_capture_autonomous_run(sid, "thread-A", "cluster PBMC cells by type", "run-a", "/tmp/a", "succeeded")
    await _auto_capture_autonomous_run(sid, "thread-B", "cluster PBMC cells by type", "run-b", "/tmp/b", "succeeded")

    suggestion = await _compute_promotion_suggestion(sid, "", "cluster PBMC cells by type", "run-c", "/tmp/c")
    assert suggestion is None


@pytest.mark.asyncio
async def test_promotion_suggestion_escapes_quotes_in_the_goal(store, monkeypatch):
    """Regression (adversarial codex review): a goal containing a double
    quote (e.g. a gene/marker name in scare quotes) must not produce a
    syntactically broken suggested call — repr(), not raw interpolation."""
    import omicsclaw.runtime.agent.state as _state

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id
    goal = 'cluster "tumor" cells and annotate'

    await _auto_capture_autonomous_run(sid, "t", goal, "run-1", "/tmp/1", "succeeded")
    await _auto_capture_autonomous_run(sid, "t", goal, "run-2", "/tmp/2", "succeeded")
    suggestion = await _compute_promotion_suggestion(sid, "t", goal, "run-3", "/tmp/3")

    assert suggestion is not None
    assert repr(goal) in suggestion
    # The exact broken shape a naive f'...="{goal}"...' would have produced.
    assert f'request="{goal}"' not in suggestion
