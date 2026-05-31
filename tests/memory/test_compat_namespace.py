"""Tests for CompatMemoryStore namespace injection (PR #4a Task 4a.2).

The plan §4a.2 contract:
  - save_memory(session_id, mem) extracts (platform, user_id) from the
    session and writes to namespace = f"{platform}/{user_id}".
  - Sessions themselves stay in __shared__ so a session_id can be
    resolved to its user/platform globally.
  - All existing CompatMemoryStore tests continue to pass.

This is the production data path for Telegram/Feishu bots — a leak here
would mean user A could see user B's memories. Tests below explicitly
prove that isolation.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa

from omicsclaw.memory.compat import (
    AnalysisMemory,
    CompatMemoryStore,
    DatasetMemory,
    PreferenceMemory,
    ProjectContextMemory,
    ThreadMemory,
    _content_to_memory,
    _memory_to_uri_path,
)
from omicsclaw.memory.models import Path


def test_dataset_memory_uri_scopes_under_thread_id():
    # Bench (ADR 0018, Phase 3.3): a set thread_id scopes a downloaded dataset
    # under its investigation thread (dataset://<thread_id>/<basename>) so Analyze
    # in that thread sees it; empty thread_id keeps the legacy flat dataset URI.
    legacy = DatasetMemory(file_path="GSE12345/matrix.h5ad")
    assert _memory_to_uri_path(legacy) == "GSE12345_matrix.h5ad"

    scoped = DatasetMemory(file_path="GSE12345/matrix.h5ad", thread_id="t-glioma")
    assert _memory_to_uri_path(scoped) == "t-glioma/GSE12345_matrix.h5ad"


def test_thread_memory_uri_is_thread_id():
    # Bench Phase 1: thread metadata is addressed by its thread_id → project://<thread_id>.
    tm = ThreadMemory(thread_id="t-glioma", name="Glioma study")
    assert _memory_to_uri_path(tm) == "t-glioma"


def test_thread_memory_roundtrips_via_content_authoritative_type():
    # ThreadMemory and ProjectContextMemory share the "project" domain. Deserialization
    # must use the content's embedded memory_type, NOT the (ambiguous) domain hint:
    # passing the WRONG hint still reconstructs the right class.
    tm = ThreadMemory(thread_id="t1", name="N", domains=["spatial"], is_deleted=False)
    back = _content_to_memory(tm.model_dump_json(), "project_context")  # wrong hint
    assert isinstance(back, ThreadMemory)
    assert back.thread_id == "t1" and back.name == "N" and back.domains == ["spatial"]

    pc = ProjectContextMemory(project_goal="map the TME")
    back2 = _content_to_memory(pc.model_dump_json(), "thread")  # wrong hint, other way
    assert isinstance(back2, ProjectContextMemory)
    assert back2.project_goal == "map the TME"


def test_project_context_memory_uri_unchanged():
    pc = ProjectContextMemory(project_goal="x")
    assert _memory_to_uri_path(pc) == pc.memory_id


def test_analysis_memory_uri_scopes_under_thread_id():
    # Bench (ADR 0018): a set thread_id scopes the analysis:// lineage under the
    # investigation thread so a thread rolls up only its own runs (BE-RECALL-6
    # reads analysis://<id>/*); empty thread_id keeps the legacy un-scoped path.
    legacy = AnalysisMemory(source_dataset_id="", skill="spatial-domains", method="leiden")
    assert _memory_to_uri_path(legacy) == f"spatial-domains/{legacy.memory_id}"

    scoped = AnalysisMemory(
        source_dataset_id="", skill="spatial-domains", method="leiden", thread_id="t-glioma"
    )
    assert _memory_to_uri_path(scoped) == f"t-glioma/spatial-domains/{scoped.memory_id}"


@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield store
    await store.close()


# ----------------------------------------------------------------------
# Session storage stays shared
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_is_stored_in_shared_namespace(store):
    """Session storage stays globally addressable so any worker can
    resolve a session_id to its (user, platform) without knowing where
    to look."""
    session = await store.create_session("user42", "telegram")

    async with store._db.session() as s:
        rows = (
            await s.execute(
                sa.select(Path).where(
                    Path.domain == "session",
                    Path.path == session.session_id,
                )
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].namespace == "__shared__"


@pytest.mark.asyncio
async def test_get_session_round_trips(store):
    session = await store.create_session("user42", "telegram")
    fetched = await store.get_session(session.session_id)
    assert fetched is not None
    assert fetched.user_id == "user42"
    assert fetched.platform == "telegram"


# ----------------------------------------------------------------------
# Memory storage uses session-derived namespace
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_memories_type_gate_with_shared_project_domain(store):
    """Bench Phase 1 regression: project_context and thread share the project://
    domain, and content-authoritative deserialization no longer drops wrong-type
    rows — so get_memories must re-apply the requested type filter. Without the
    gate: no-filter duplicates 6x, typed queries bleed across types, and
    get_memories('project_context') can return a ThreadMemory (crashing the
    production context loader on .project_goal)."""
    session = await store.create_session("user42", "telegram")
    sid = session.session_id
    await store.save_memory(sid, ProjectContextMemory(project_goal="map the TME"))
    await store.save_memory(sid, ThreadMemory(thread_id="t1", name="Glioma"))
    await store.save_memory(sid, DatasetMemory(file_path="pbmc.h5ad"))

    # No-filter: exactly one of each type, no duplication.
    everything = await store.get_memories(sid)
    types = sorted(m.memory_type for m in everything)
    assert types == ["dataset", "project_context", "thread"], types

    # Typed queries are isolated — no cross-type bleed in the shared project domain.
    pcs = await store.get_memories(sid, "project_context")
    assert [m.memory_type for m in pcs] == ["project_context"]
    assert isinstance(pcs[0], ProjectContextMemory) and pcs[0].project_goal == "map the TME"

    threads = await store.get_memories(sid, "thread")
    assert [m.memory_type for m in threads] == ["thread"]
    assert isinstance(threads[0], ThreadMemory) and threads[0].thread_id == "t1"

    datasets = await store.get_memories(sid, "dataset")
    assert [m.memory_type for m in datasets] == ["dataset"]

    # load_context must not crash (it calls get_memories('project_context') then
    # reads .project_goal) and renders the project goal exactly once.
    ctx = await store.load_context(sid)
    assert isinstance(ctx, str)
    assert ctx.count("map the TME") == 1


@pytest.mark.asyncio
async def test_save_memory_uses_session_derived_namespace(store):
    """A DatasetMemory saved under a tg session lands in tg/<user_id>."""
    session = await store.create_session("user42", "telegram")
    await store.save_memory(
        session.session_id, DatasetMemory(file_path="pbmc.h5ad")
    )

    async with store._db.session() as s:
        rows = (
            await s.execute(
                sa.select(Path).where(Path.domain == "dataset")
            )
        ).scalars().all()
        namespaces = [r.namespace for r in rows]

    assert "telegram/user42" in namespaces


@pytest.mark.asyncio
async def test_save_memory_isolates_users_on_same_platform(store):
    """The leak-prevention test: two telegram users save the same
    dataset URI; each only sees their own row."""
    sa_session = await store.create_session("alice", "telegram")
    sb_session = await store.create_session("bob", "telegram")

    await store.save_memory(
        sa_session.session_id, DatasetMemory(file_path="alpha.h5ad")
    )
    await store.save_memory(
        sb_session.session_id, DatasetMemory(file_path="beta.h5ad")
    )

    a_memories = await store.get_memories(sa_session.session_id, "dataset")
    b_memories = await store.get_memories(sb_session.session_id, "dataset")

    a_files = {m.file_path for m in a_memories}
    b_files = {m.file_path for m in b_memories}

    assert a_files == {"alpha.h5ad"}
    assert b_files == {"beta.h5ad"}


@pytest.mark.asyncio
async def test_save_memory_isolates_users_across_platforms(store):
    """A telegram user and a feishu user should not see each other's data."""
    tg = await store.create_session("user1", "telegram")
    fs = await store.create_session("user1", "feishu")

    await store.save_memory(
        tg.session_id, DatasetMemory(file_path="tg_only.h5ad")
    )
    await store.save_memory(
        fs.session_id, DatasetMemory(file_path="fs_only.h5ad")
    )

    tg_mem = await store.get_memories(tg.session_id, "dataset")
    fs_mem = await store.get_memories(fs.session_id, "dataset")

    assert {m.file_path for m in tg_mem} == {"tg_only.h5ad"}
    assert {m.file_path for m in fs_mem} == {"fs_only.h5ad"}


@pytest.mark.asyncio
async def test_save_preference_lands_in_user_namespace_versioned(store):
    """preference://* should be versioned in the user's namespace."""
    session = await store.create_session("user42", "telegram")
    await store.save_memory(
        session.session_id,
        PreferenceMemory(domain="qc", key="cutoff", value=0.5),
    )

    async with store._db.session() as s:
        rows = (
            await s.execute(
                sa.select(Path).where(
                    Path.domain == "preference",
                    Path.path == "qc/cutoff",
                )
            )
        ).scalars().all()

    # Exactly one Path at qc/cutoff (composite PK guarantees this) and
    # it lives in the user's namespace.
    assert len(rows) == 1
    assert rows[0].namespace == "telegram/user42"


@pytest.mark.asyncio
async def test_search_memories_filters_by_session_namespace(store):
    """search_memories(session_id, query) only finds the session's own
    memories — never another user's."""
    sa_session = await store.create_session("alice", "telegram")
    sb_session = await store.create_session("bob", "telegram")

    await store.save_memory(
        sa_session.session_id,
        DatasetMemory(file_path="alpha-secret.h5ad"),
    )
    await store.save_memory(
        sb_session.session_id,
        DatasetMemory(file_path="beta-secret.h5ad"),
    )

    a_hits = await store.search_memories(sa_session.session_id, "secret")
    a_files = {m.file_path for m in a_hits}

    assert "alpha-secret.h5ad" in a_files
    assert "beta-secret.h5ad" not in a_files


# ----------------------------------------------------------------------
# Lazy-init: bot/session.py constructs the store from a SYNC init()
# and cannot await initialize(), so public async methods must
# self-initialise on first use. Production bug surfaced from a CLI
# memory tool call: AssertionError "CompatMemoryStore must be
# initialised before use" deep inside execute_remember.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compat_store_lazy_init_via_get_session(tmp_path):
    """Production first-touch is `get_session` (called by
    `_assemble_chat_context → session_manager.get_or_create`), not
    `create_session`. Pin the actual prod path: a freshly-constructed
    store, never explicitly initialised, must answer get_session
    correctly (returning None for an unknown id) by lazy-init'ing."""
    from omicsclaw.memory.compat import CompatMemoryStore

    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    try:
        result = await store.get_session("nonexistent")
        assert result is None
        assert store._initialized, (
            "lazy-init didn't fire on get_session — production "
            "_assemble_chat_context path will still AssertionError"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_compat_store_concurrent_first_init_runs_body_once(
    tmp_path, monkeypatch
):
    """The asyncio.Lock around initialize()'s body must serialise
    concurrent first-time init so the body executes exactly once,
    even under N parallel callers.

    Without the lock, two coroutines both pass the
    ``if self._initialized: return`` fast-path check, both run the
    body in parallel, and the second clobbers the first's
    ``_db`` / ``_engine`` / ``_session_client`` while the first
    coroutine may still be operating against the original engine.
    Counting ``DatabaseManager.init_db`` invocations is the cleanest
    detection: under-the-lock = 1, race = N.
    """
    import asyncio as _asyncio
    from omicsclaw.memory import database as database_mod
    from omicsclaw.memory.compat import CompatMemoryStore

    init_count = 0
    real_init_db = database_mod.DatabaseManager.init_db

    async def counting_init_db(self):
        nonlocal init_count
        init_count += 1
        # Widen the race window so a missing lock is reliably detectable.
        await _asyncio.sleep(0.01)
        return await real_init_db(self)

    monkeypatch.setattr(database_mod.DatabaseManager, "init_db", counting_init_db)

    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    try:
        # 8 parallel public-method calls on a freshly-constructed store —
        # none requires a pre-existing session.
        await _asyncio.gather(
            *[store.get_session(f"missing-{i}") for i in range(5)],
            *[store.create_session(f"u{i}", "telegram") for i in range(3)],
        )
        assert init_count == 1, (
            f"initialize() body ran {init_count} times — the lock failed "
            "to serialise concurrent first-init (race condition active)"
        )
        assert store._initialized
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_compat_store_lazy_initialises_on_first_public_call(tmp_path):
    """CompatMemoryStore must auto-initialise when a public async method
    is called before someone has explicitly awaited initialize().

    bot/session.py:311 constructs the store from a synchronous init()
    function, then assigns it to omicsclaw.runtime.agent.state.memory_store. The async
    initialize() coroutine is never awaited along that path. The first
    LLM tool call (execute_remember -> memory_store.save_memory) must
    not blow up with 'CompatMemoryStore must be initialised before use'.
    """
    from omicsclaw.memory.compat import CompatMemoryStore, PreferenceMemory

    # Mirror production: construct, do NOT await initialize().
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")

    try:
        session = await store.create_session("alice", "telegram")
        assert session.platform == "telegram"
        assert session.user_id == "alice"

        pref = PreferenceMemory(
            domain="global", key="language", value="zh", is_strict=False
        )
        mem_id = await store.save_memory(session.session_id, pref)
        assert mem_id, "save_memory after lazy init returned no memory id"

        # Round-trip via search to prove the engine + indexer are alive.
        hits = await store.search_memories(session.session_id, "language")
        assert any(h.key == "language" for h in hits if hasattr(h, "key"))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_save_memory_with_unknown_session_raises(store):
    """If the session can't be resolved, the write must NOT fall back to
    ``__shared__`` (every other user could read it). Instead
    ``_client_for_session`` raises ``LookupError`` and ``save_memory``
    propagates — the caller decides whether to log-and-skip
    (``_auto_capture_dataset``) or surface the error.

    This pins the privacy fix: the prior behavior was a silent
    fall-back to the shared partition, which leaked auto-captured
    datasets across users when a session was missing or evicted."""
    with pytest.raises(LookupError):
        await store.save_memory(
            "nonexistent-session-id", DatasetMemory(file_path="orphan.h5ad")
        )

    # Nothing landed anywhere — particularly not in __shared__.
    async with store._db.session() as s:
        rows = (
            await s.execute(
                sa.select(Path).where(
                    Path.domain == "dataset", Path.path == "orphan.h5ad"
                )
            )
        ).scalars().all()
    assert rows == []
