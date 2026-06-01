"""Tests for session↔thread binding + envelope resolution (Phase 1, Slice 6 / ADR 0023 d3).

A conversation binds to an investigation thread at lazy session creation; the
backend resolves thread_id = request.thread_id or the bound session's thread_id,
so the binding survives turns that omit the field. An existing session's binding
is immutable (rebinding is v1.5).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from omicsclaw.memory.compat import CompatMemoryStore, Session
from omicsclaw.runtime.agent.session import SessionManager


@pytest_asyncio.fixture
async def sm(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield SessionManager(store)
    await store.close()


def test_session_model_legacy_blob_defaults_thread_id():
    # A legacy session:// blob written before Slice 6 has no thread_id field;
    # it must deserialize with the default (unbound), not error.
    s = Session.model_validate_json('{"session_id":"x","user_id":"u","platform":"app"}')
    assert s.thread_id == ""


@pytest.mark.asyncio
async def test_get_or_create_stamps_thread_id_on_new_and_is_immutable(sm):
    s = await sm.get_or_create("u", "app", "c1", thread_id="A")
    assert s.thread_id == "A"
    # round-trips through the store
    fetched = await sm.store.get_session("app:u:c1")
    assert fetched is not None and fetched.thread_id == "A"
    # an existing session keeps its binding even if a different thread is passed
    again = await sm.get_or_create("u", "app", "c1", thread_id="B")
    assert again.thread_id == "A"
    # re-fetch from the store (not the pre-touch return value) so an update_session
    # clobber of thread_id would actually be caught.
    refetched = await sm.store.get_session("app:u:c1")
    assert refetched is not None and refetched.thread_id == "A"


@pytest.mark.asyncio
async def test_get_or_create_unbound_when_no_thread_id(sm):
    s = await sm.get_or_create("u", "app", "c2")
    assert s.thread_id == ""


# ---- the server-boundary resolver -----------------------------------------


@pytest.mark.asyncio
async def test_resolve_request_thread_id_wins_and_binds_new_session(sm):
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id

    r = await _resolve_and_bind_thread_id(sm, "u", "sess1", "A")
    assert r == "A"
    bound = await sm.store.get_session("app:u:sess1")
    assert bound is not None and bound.thread_id == "A"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_bound_session_when_request_empty(sm):
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id

    # Turn 1 binds the session to A; turn 2 omits thread_id → resolves to A.
    await _resolve_and_bind_thread_id(sm, "u", "sess2", "A")
    r = await _resolve_and_bind_thread_id(sm, "u", "sess2", "")
    assert r == "A"


@pytest.mark.asyncio
async def test_resolve_request_wins_but_does_not_rebind_existing(sm):
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id

    # Session bound to A; a turn arrives with an explicit request thread_id "B".
    # Request wins for THIS turn (resolved == "B"), but the durable session binding
    # stays A — rebinding an existing conversation is v1.5 (ADR 0023 decision 3).
    await _resolve_and_bind_thread_id(sm, "u", "sessX", "A")
    r = await _resolve_and_bind_thread_id(sm, "u", "sessX", "B")
    assert r == "B"
    bound = await sm.store.get_session("app:u:sessX")
    assert bound is not None and bound.thread_id == "A"


@pytest.mark.asyncio
async def test_resolve_legacy_unbound_returns_empty(sm):
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id

    # No request thread_id and no session → empty (legacy unscoped; byte-identical
    # to Phase 0). A session is NOT eagerly created for an unbound turn.
    r = await _resolve_and_bind_thread_id(sm, "u", "sess3", "")
    assert r == ""
    assert await sm.store.get_session("app:u:sess3") is None


@pytest.mark.asyncio
async def test_resolve_no_session_manager_returns_request_value():
    from omicsclaw.surfaces.desktop.server import _resolve_and_bind_thread_id

    assert await _resolve_and_bind_thread_id(None, "u", "s", "A") == "A"
    assert await _resolve_and_bind_thread_id(None, "u", "s", "") == ""
