"""Tests for thread-scoped recall (Bench Phase 1, Slice 4 / BE-RECALL-6).

Covers: the recall ToolSpec delivers thread_id to the executor via context_params
(the choke point), and execute_recall returns the active thread's memories first
with cross-thread hits appended (ranked lower), while an empty thread_id is the
legacy unscoped recall.
"""

from __future__ import annotations

import pytest


def test_recall_spec_delivers_thread_id_via_context_params():
    # Choke point: if context_params omits thread_id, the executor never receives
    # it (build_executor_kwargs only forwards declared params) and recall is
    # silently never thread-scoped.
    from omicsclaw.runtime.tools.builders.agent import (
        BotToolContext,
        build_bot_tool_specs,
    )
    from omicsclaw.runtime.tools.executor import build_executor_kwargs

    specs = {
        s.name: s
        for s in build_bot_tool_specs(
            BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
        )
    }
    kw = build_executor_kwargs(
        specs["recall"], {"session_id": "s", "thread_id": "A", "chat_id": 1}
    )
    assert kw.get("thread_id") == "A"
    assert kw.get("session_id") == "s"


@pytest.mark.asyncio
async def test_execute_recall_thread_first_then_cross_thread(monkeypatch, tmp_path):
    import omicsclaw.runtime.agent.state as state  # prod-order import (avoids cycle)
    import omicsclaw.runtime.tools.builders.agent_executors as ae
    from omicsclaw.memory.compat import CompatMemoryStore, DatasetMemory

    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    try:
        s = await store.create_session("u", "telegram")
        await store.save_memory(s.session_id, DatasetMemory(file_path="alpha.h5ad", thread_id="A"))
        await store.save_memory(s.session_id, DatasetMemory(file_path="beta.h5ad", thread_id="B"))
        monkeypatch.setattr(state, "memory_store", store)

        # Thread A active: A's dataset first, B's appended (cross-thread fallback).
        out = await ae.execute_recall(
            {"memory_type": "dataset"}, session_id=s.session_id, thread_id="A"
        )
        assert "alpha.h5ad" in out and "beta.h5ad" in out
        assert out.index("alpha.h5ad") < out.index("beta.h5ad")

        # Legacy unscoped recall still returns both (no thread ordering imposed).
        legacy = await ae.execute_recall(
            {"memory_type": "dataset"}, session_id=s.session_id, thread_id=""
        )
        assert "alpha.h5ad" in legacy and "beta.h5ad" in legacy
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_execute_recall_thread_does_not_starve_user_globals(monkeypatch, tmp_path):
    """Regression: a busy thread's auto-captured rows must NOT crowd the user's
    explicitly-saved globals (preference/insight/project_context) out of a no-query
    thread-scoped recall. >limit thread datasets + one saved preference → the
    preference still surfaces."""
    import omicsclaw.runtime.agent.state as state
    import omicsclaw.runtime.tools.builders.agent_executors as ae
    from omicsclaw.memory.compat import CompatMemoryStore, DatasetMemory, PreferenceMemory

    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    try:
        s = await store.create_session("u", "telegram")
        for i in range(12):  # > default limit (10), all thread-A
            await store.save_memory(s.session_id, DatasetMemory(file_path=f"d{i}.h5ad", thread_id="A"))
        await store.save_memory(s.session_id, PreferenceMemory(domain="qc", key="cutoff", value="0.5"))
        monkeypatch.setattr(state, "memory_store", store)

        out = await ae.execute_recall({}, session_id=s.session_id, thread_id="A")
        assert "cutoff" in out  # the saved preference is not starved out
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_execute_recall_query_path_thread_first(monkeypatch, tmp_path):
    """The query/search two-pass path also ranks the active thread's hits first."""
    import omicsclaw.runtime.agent.state as state
    import omicsclaw.runtime.tools.builders.agent_executors as ae
    from omicsclaw.memory.compat import CompatMemoryStore, DatasetMemory

    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    try:
        s = await store.create_session("u", "telegram")
        await store.save_memory(s.session_id, DatasetMemory(file_path="glioma_A.h5ad", thread_id="A"))
        await store.save_memory(s.session_id, DatasetMemory(file_path="glioma_B.h5ad", thread_id="B"))
        monkeypatch.setattr(state, "memory_store", store)

        out = await ae.execute_recall({"query": "glioma"}, session_id=s.session_id, thread_id="A")
        assert "glioma_A.h5ad" in out and "glioma_B.h5ad" in out
        assert out.index("glioma_A.h5ad") < out.index("glioma_B.h5ad")
    finally:
        await store.close()
