"""AN-ROUTER-10 (Bench Phase 4): a successful typed-consensus run records its
lineage at the canonical, thread-scoped consensus namespace
``analysis://<thread_id>/typed/<run_id>`` (``consensus_namespace`` — ADR 0010:
meta-analysis reads ``typed/*``; ADR 0018: thread scoping), instead of the
generic ``<skill>/<uuid>`` capture. These tests drive the in-process capture
helper ``_auto_capture_consensus`` against a real ``CompatMemoryStore`` so the
URI shape, the global<->thread visibility, and the fall-back contract are all
exercised end-to-end (no mocking of the memory layer)."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from omicsclaw.memory.compat import CompatMemoryStore


@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield store


def _write_plan(out_dir, *, run_id="run-XYZ", operator="weighted"):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps({"run_id": run_id, "operator": operator}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_consensus_lineage_lands_at_thread_scoped_typed_uri(store, tmp_path, monkeypatch):
    """A successful consensus run lands ONE AnalysisMemory at
    analysis://<thread_id>/typed/<run_id>, carrying the real consensus skill name
    and the operator from plan.json; it is visible to thread-scoped recall and
    the helper returns True (so the caller skips the generic capture)."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill.orchestration import _auto_capture_consensus

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-XYZ"
    _write_plan(out_dir, run_id="run-XYZ", operator="weighted")

    captured = await _auto_capture_consensus(
        sid, "consensus-domains", out_dir, True, thread_id="t-glioma"
    )
    assert captured is True

    analyses = await store.get_memories(sid, "analysis", thread_id="t-glioma")
    assert len(analyses) == 1
    rec = analyses[0]
    assert rec.skill == "consensus-domains"
    assert rec.method == "weighted"
    assert rec.thread_id == "t-glioma"
    assert rec.memory_id == "run-XYZ"

    client = await store._client_for_session(sid)
    got = await client.recall("analysis://t-glioma/typed/run-XYZ")
    assert got is not None and got.content


@pytest.mark.asyncio
async def test_consensus_lineage_empty_thread_id_is_legacy_unscoped(store, tmp_path, monkeypatch):
    """Empty thread_id keeps the legacy un-scoped analysis://typed/<run_id> URI
    (byte-identical to the pre-Bench consensus_namespace)."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill.orchestration import _auto_capture_consensus

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-LEGACY"
    _write_plan(out_dir, run_id="run-LEGACY")

    captured = await _auto_capture_consensus(
        sid, "sc-consensus-clustering", out_dir, True, thread_id=""
    )
    assert captured is True

    client = await store._client_for_session(sid)
    assert await client.recall("analysis://typed/run-LEGACY") is not None


@pytest.mark.asyncio
async def test_consensus_lineage_skips_non_consensus_skill(store, tmp_path, monkeypatch):
    """A regular skill is not a registered consensus flavour → returns False so
    the caller falls back to the generic _auto_capture_analysis, and the helper
    writes nothing itself."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill.orchestration import _auto_capture_consensus

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id
    out_dir = tmp_path / "run-1"
    _write_plan(out_dir)

    captured = await _auto_capture_consensus(sid, "sc-de", out_dir, True, thread_id="t-A")
    assert captured is False
    assert await store.get_memories(sid, "analysis", thread_id="t-A") == []


@pytest.mark.asyncio
async def test_consensus_lineage_failed_run_falls_back(store, tmp_path, monkeypatch):
    """A failed consensus run has no verified typed/ lineage → returns False
    (failures stay on the generic per-skill capture) and writes nothing here."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill.orchestration import _auto_capture_consensus

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id
    out_dir = tmp_path / "run-FAIL"
    _write_plan(out_dir)

    captured = await _auto_capture_consensus(
        sid, "consensus-domains", out_dir, False, thread_id="t-A"
    )
    assert captured is False
    assert await store.get_memories(sid, "analysis", thread_id="t-A") == []


@pytest.mark.asyncio
async def test_consensus_lineage_run_id_falls_back_to_output_dir_name(store, tmp_path, monkeypatch):
    """When plan.json is absent the run_id falls back to output_dir.name and the
    operator defaults to kmode — the lineage still lands at its canonical URI."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill.orchestration import _auto_capture_consensus

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-NOPLAN"
    out_dir.mkdir()  # deliberately no plan.json

    captured = await _auto_capture_consensus(
        sid, "consensus-domains", out_dir, True, thread_id="t-A"
    )
    assert captured is True

    client = await store._client_for_session(sid)
    assert await client.recall("analysis://t-A/typed/run-NOPLAN") is not None
    analyses = await store.get_memories(sid, "analysis", thread_id="t-A")
    assert analyses[0].memory_id == "run-NOPLAN"  # run_id == output_dir.name
    assert analyses[0].method == "kmode"
