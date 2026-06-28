"""Per-thread KG source index (批7, ADR 0019/0021).

A thread's KG sources are recorded as independent overwrite-mode nodes at
``thread_source://<thread_id>/<slug>`` (one per ingested source), mirroring the
``dataset://<thread_id>/*`` precedent. This avoids the read-modify-write race a
mutable list on the versioned ``project://<thread_id>`` ThreadMemory node would
have with concurrent thread-metadata edits.

Write path: ``orchestration._capture_thread_source`` (called from the ingest
paths). Read path: ``thread.list_thread_source_slugs`` (used by the Read/Ideate
desktop endpoints). Writer (loop ``state.memory_store``) and reader (desktop
``_memory_client``) converge on the same namespace/DB.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from omicsclaw.memory.compat import (
    CompatMemoryStore,
    ThreadSourceMemory,
    _content_to_memory,
    _memory_to_uri_path,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield store
    await store.close()


# ---- model round-trip + URI mapping ----


def test_thread_source_memory_roundtrip():
    m = ThreadSourceMemory(thread_id="A", slug="ruan2024-atlas", source_page="wiki/sources/ruan2024-atlas.md")
    back = _content_to_memory(m.model_dump_json(), "thread_source")
    assert isinstance(back, ThreadSourceMemory)
    assert back.thread_id == "A"
    assert back.slug == "ruan2024-atlas"
    assert back.source_page == "wiki/sources/ruan2024-atlas.md"


def test_thread_source_memory_legacy_blob_defaults_source_page():
    # A minimal blob (no source_page) must deserialize with the default.
    back = _content_to_memory('{"memory_type":"thread_source","thread_id":"A","slug":"s"}', "thread_source")
    assert isinstance(back, ThreadSourceMemory)
    assert back.source_page == ""


def test_thread_source_uri_path():
    m = ThreadSourceMemory(thread_id="A", slug="s-1")
    assert _memory_to_uri_path(m) == "A/s-1"


def test_thread_source_domain_not_versioned():
    from omicsclaw.memory.namespace_policy import should_version
    from omicsclaw.memory.uri import MemoryURI

    assert should_version(MemoryURI.parse("thread_source://A/s-1")) is False


# ---- write (store) <-> read (client) convergence ----


@pytest.mark.asyncio
async def test_capture_and_list_thread_source_slugs(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration
    from omicsclaw.surfaces.desktop import thread as thread_svc

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "app")

    await orchestration._capture_thread_source(session.session_id, "A", "slug-1", "wiki/sources/slug-1.md")
    await orchestration._capture_thread_source(session.session_id, "A", "slug-2")
    # dedupe: same slug again is a no-op overwrite (one node, not two)
    await orchestration._capture_thread_source(session.session_id, "A", "slug-1")

    client = await store._client_for_session(session.session_id)
    slugs = await thread_svc.list_thread_source_slugs(client, "A")
    assert sorted(slugs) == ["slug-1", "slug-2"]


@pytest.mark.asyncio
async def test_capture_thread_source_guards(store, monkeypatch):
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration
    from omicsclaw.surfaces.desktop import thread as thread_svc

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "app")

    # empty slug / empty thread_id are no-ops
    await orchestration._capture_thread_source(session.session_id, "A", "")
    await orchestration._capture_thread_source(session.session_id, "", "slug-x")
    client = await store._client_for_session(session.session_id)
    assert await thread_svc.list_thread_source_slugs(client, "A") == []

    # memory disabled -> no crash, no write
    monkeypatch.setattr(_state, "memory_store", None, raising=False)
    await orchestration._capture_thread_source(session.session_id, "A", "slug-y")


@pytest.mark.asyncio
async def test_list_thread_source_slugs_empty_for_missing_thread(store, monkeypatch):
    from omicsclaw.surfaces.desktop import thread as thread_svc

    session = await store.create_session("u", "app")
    client = await store._client_for_session(session.session_id)
    assert await thread_svc.list_thread_source_slugs(client, "ghost") == []
    assert await thread_svc.list_thread_source_slugs(client, "") == []


@pytest.mark.asyncio
async def test_resolve_thread_dataset_path_real_client(store):
    """批8/D-3: against a REAL MemoryClient, the thread's bound dataset resolves
    (this is the test the trailing-slash bug evaded — fakes returned leaves
    regardless of the slash). Shared by route-preview + kg_build_packet routing.
    """
    from omicsclaw.memory.compat import DatasetMemory, resolve_thread_dataset_path

    session = await store.create_session("u", "app")
    await store.save_memory(session.session_id, DatasetMemory(file_path="data/glioma.h5ad", thread_id="A"))
    client = await store._client_for_session(session.session_id)
    assert await resolve_thread_dataset_path(client, "A") == "data/glioma.h5ad"
    # No dataset bound → "" (claim-only fallback), not an error.
    assert await resolve_thread_dataset_path(client, "ghost") == ""
