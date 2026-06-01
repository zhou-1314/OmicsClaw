"""BE-WIRING-TEST-8 (Bench Phase 5) — full backend wiring, end to end.

Drives the seams a Bench turn actually exercises — thread_id binding (the
resolver), thread-scoped lineage capture (dataset / analysis-with-provenance /
typed consensus), thread-scoped recall + passive context injection, cross-thread
isolation, durable binding across a field-omitting turn ("reload"), the
empty-thread legacy path (no /chat regression), and graceful soft-fail when
memory is absent — over one consistent CompatMemoryStore + SessionManager.

No LLM: it proves the backend chain hangs together (Phases 0/1/1A/2/3/4/5), not
the model. The individual pieces have unit tests; this pins that they compose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from omicsclaw.memory.compat import CompatMemoryStore
from omicsclaw.runtime.agent.session import SessionManager


@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/wiring.db")
    await store.initialize()
    yield store


def _write_result_json(out_dir: Path, *, version="0.5.0", checksum="abc", params=None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps({
            "skill": "sc-de", "version": version, "input_checksum": checksum,
            "data": {"params": params or {"method": "deseq2", "padj_cutoff": 0.05}},
        }),
        encoding="utf-8",
    )
    return out_dir


def _write_consensus_plan(out_dir: Path, *, run_id: str, operator="weighted") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps({
            "run_id": run_id, "operator": operator,
            "members": [{"name": "leiden_r1"}, {"name": "leiden_r2"}],
        }),
        encoding="utf-8",
    )
    return out_dir


@pytest.mark.asyncio
async def test_bench_thread_continuity_end_to_end(store, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import omicsclaw.runtime.agent.state as state
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id
    from omicsclaw.skill import orchestration
    from omicsclaw.runtime.tools.builders import agent_executors as ae

    monkeypatch.setattr(state, "memory_store", store, raising=False)
    sm = SessionManager(store)

    # --- create + bind: a turn carrying thread_id="glioma" stamps the session ---
    resolved = await _resolve_and_bind_thread_id(sm, "u1", "chat1", "glioma")
    assert resolved == "glioma"
    sid = "app:u1:chat1"
    sess = await store.get_session(sid)
    assert sess is not None and sess.thread_id == "glioma"

    # --- turn in glioma: capture dataset + analysis (provenance) + typed consensus ---
    await orchestration._auto_capture_dataset(sid, "glioma_visium.csv", "visium", thread_id="glioma")
    de_dir = _write_result_json(tmp_path / "glioma-de")
    await orchestration._auto_capture_analysis(
        sid, "sc-de", {"method": "deseq2", "file_path": "glioma_visium.csv"}, de_dir, True, thread_id="glioma"
    )
    cons_dir = _write_consensus_plan(tmp_path / "run-glioma-cons", run_id="run-glioma-cons")
    assert await orchestration._auto_capture_consensus(
        sid, "consensus-domains", cons_dir, True, thread_id="glioma"
    ) is True

    # --- artifacts: thread-scoped, with the provenance index + typed lineage ---
    analyses = await store.get_memories(sid, "analysis", thread_id="glioma")
    assert {"sc-de", "consensus-domains"} <= {a.skill for a in analyses}
    de = next(a for a in analyses if a.skill == "sc-de")
    assert de.skill_version == "0.5.0" and de.input_checksum == "abc"
    assert de.effective_params.get("method") == "deseq2"
    client = await store._client_for_session(sid)
    assert await client.recall("analysis://glioma/typed/run-glioma-cons") is not None
    datasets = await store.get_memories(sid, "dataset", thread_id="glioma")
    assert any("glioma_visium" in d.file_path for d in datasets)

    # --- passive injection scoped to the active thread ---
    ctx = await sm.load_context(sid, thread_id="glioma")
    assert "glioma_visium" in ctx and "sc-de" in ctx

    # --- recall tool is wired with thread_id and surfaces the thread's memories ---
    out = await ae.execute_recall({}, session_id=sid, thread_id="glioma")
    assert "Found" in out  # not "No memories found." / not the disabled hint
    assert "glioma_visium.csv" in out and "sc-de" in out

    # --- thread isolation: capture a SECOND thread's dataset in the SAME session, so
    # isolation is proven by thread_id scoping, NOT by session/namespace separation ---
    await orchestration._auto_capture_dataset(sid, "pbmc_10x.csv", "10x", thread_id="pbmc")
    glioma_ds = await store.get_memories(sid, "dataset", thread_id="glioma")
    assert {d.file_path for d in glioma_ds} and all("pbmc" not in d.file_path for d in glioma_ds)
    pbmc_ds = await store.get_memories(sid, "dataset", thread_id="pbmc")
    assert {d.file_path for d in pbmc_ds} and all("glioma" not in d.file_path for d in pbmc_ds)
    # passive injection for glioma must not surface the pbmc thread's dataset
    assert "pbmc_10x" not in (await sm.load_context(sid, thread_id="glioma"))


@pytest.mark.asyncio
async def test_durable_binding_survives_field_omitting_turn(store, monkeypatch):
    """'reload' / a later turn that omits thread_id still rolls up to the bound
    thread; the binding is immutable once set."""
    pytest.importorskip("fastapi")
    import omicsclaw.runtime.agent.state as state
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id

    monkeypatch.setattr(state, "memory_store", store, raising=False)
    sm = SessionManager(store)

    await _resolve_and_bind_thread_id(sm, "u1", "chat1", "glioma")  # first turn binds
    resolved = await _resolve_and_bind_thread_id(sm, "u1", "chat1", "")  # omits field
    assert resolved == "glioma"
    sess = await store.get_session("app:u1:chat1")
    assert sess.thread_id == "glioma"  # binding immutable


@pytest.mark.asyncio
async def test_no_chat_regression_empty_thread_is_unscoped(store, tmp_path, monkeypatch):
    """Empty thread_id (legacy /chat): every Bench seam is a no-op — capture lands
    at legacy un-scoped URIs (no thread segment), recall + passive injection are
    unscoped, and load_context('') is byte-identical to the no-arg legacy load."""
    import omicsclaw.runtime.agent.state as state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(state, "memory_store", store, raising=False)
    sm = SessionManager(store)
    sess = await store.create_session("u9", "app", "legacy")  # unbound (thread_id="")
    sid = sess.session_id
    assert sess.thread_id == ""

    await orchestration._auto_capture_dataset(sid, "legacy.csv", "", thread_id="")
    de_dir = _write_result_json(tmp_path / "legacy-de")
    await orchestration._auto_capture_analysis(sid, "sc-de", {"method": "deseq2"}, de_dir, True, thread_id="")

    # Captured under the legacy un-scoped URIs (NOT under any thread).
    assert await store.get_memories(sid, "analysis", thread_id="legacy") == []
    assert [a.skill for a in await store.get_memories(sid, "analysis")] == ["sc-de"]
    client = await store._client_for_session(sid)
    child_uris = {c.uri for c in await client.list_children("analysis://")}
    # The skill node sits at the TOP level of analysis:// (no thread segment); a
    # thread-scoped capture would instead nest it under analysis://<thread_id>/.
    assert "analysis://sc-de" in child_uris

    # load_context('') is byte-identical to the legacy no-arg load.
    assert await sm.load_context(sid, thread_id="") == await sm.load_context(sid)


@pytest.mark.asyncio
async def test_soft_fail_when_memory_absent(monkeypatch, tmp_path):
    """Memory unavailable → every seam degrades to a clean no-op (no crash)."""
    pytest.importorskip("fastapi")
    import omicsclaw.runtime.agent.state as state
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(state, "memory_store", None, raising=False)

    # resolver without a session manager → returns the request value, never blocks.
    assert await _resolve_and_bind_thread_id(None, "u1", "chat1", "glioma") == "glioma"

    # capture helpers no-op without a store (no exception).
    de_dir = _write_result_json(tmp_path / "x")
    await orchestration._auto_capture_dataset("sid", "x.csv", thread_id="glioma")
    await orchestration._auto_capture_analysis("sid", "sc-de", {}, de_dir, True, thread_id="glioma")
    assert await orchestration._auto_capture_consensus(
        "sid", "consensus-domains", de_dir, True, thread_id="glioma"
    ) is False
