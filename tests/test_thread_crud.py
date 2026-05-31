"""Tests for Bench thread CRUD (Phase 1, Slice 5 / BE-THREAD-CRUD-2).

Two layers: the pure service functions over a MemoryClient, and the REST routes
exercised end-to-end via an in-process ASGI client (httpx, same event loop as the
async memory DB). Covers create/list/get/update/soft-delete/preference, the 404
path, soft-delete tombstone semantics, and the static-before-dynamic route order
(/thread/list must not be captured by /thread/{thread_id}).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from omicsclaw.memory.database import DatabaseManager
from omicsclaw.memory.engine import MemoryEngine
from omicsclaw.memory.memory_client import MemoryClient
from omicsclaw.memory.search import SearchIndexer


@pytest_asyncio.fixture
async def client(tmp_path):
    db = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init_db()
    c = MemoryClient(engine=MemoryEngine(db, SearchIndexer(db)), namespace="app/userA")
    yield c
    await db.close()


# ---- service layer ---------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_service_crud_roundtrip(client):
    from omicsclaw.surfaces.desktop import thread as svc

    tm = await svc.create_thread(client, name="Glioma TME", domains=["spatial"], organism="human")
    tid = tm.thread_id
    assert tid and tm.name == "Glioma TME"

    # list shows it
    listed = await svc.list_threads(client)
    assert [t.thread_id for t in listed] == [tid]

    # get returns it
    got = await svc.get_thread(client, tid)
    assert got is not None and got.organism == "human"

    # update merges (preserves thread_id/created_at/memory_id; only sets given fields)
    upd = await svc.update_thread(client, tid, {"name": "Glioma TME v2", "venue": "Nature"})
    assert upd.name == "Glioma TME v2" and upd.venue == "Nature" and upd.thread_id == tid
    assert upd.created_at == tm.created_at and upd.memory_id == tm.memory_id
    assert upd.organism == "human"  # unspecified field kept

    # preferences round-trip
    await svc.set_thread_preference(client, tid, "default_stage", "read")
    assert (await svc.get_thread_preferences(client, tid)) == {"default_stage": "read"}

    # soft-delete hides it from list/get but the tombstone is retained
    assert await svc.delete_thread(client, tid) is True
    assert await svc.list_threads(client) == []
    assert await svc.get_thread(client, tid) is None
    tomb = await svc._recall_thread(client, tid, include_deleted=True)
    assert tomb is not None and tomb.is_deleted is True

    # delete / update / pref on a missing thread are graceful
    assert await svc.delete_thread(client, "nope") is False
    assert await svc.update_thread(client, "nope", {"name": "x"}) is None
    assert await svc.get_thread_preferences(client, "nope") is None


@pytest.mark.asyncio
async def test_list_threads_ignores_legacy_project_context(client):
    """ProjectContextMemory shares the project:// domain; it must not appear in
    the thread list (content-authoritative deserialization filters it out)."""
    from omicsclaw.memory.compat import ProjectContextMemory
    from omicsclaw.surfaces.desktop import thread as svc

    await svc.create_thread(client, name="T1")
    pc = ProjectContextMemory(project_goal="legacy global context")
    await client.remember(f"project://{pc.memory_id}", pc.model_dump_json())

    listed = await svc.list_threads(client)
    assert [t.name for t in listed] == ["T1"]  # the project_context node is excluded


# ---- REST routes (in-process ASGI) -----------------------------------------


@pytest.mark.asyncio
async def test_thread_rest_roundtrip_and_route_order(client, monkeypatch):
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from omicsclaw.surfaces.desktop import server

    monkeypatch.setattr(server, "_memory_client", client)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://t") as http:
        # create
        r = await http.post("/thread/create", json={"name": "Glioma", "domains": ["spatial"]})
        assert r.status_code == 200, r.text
        tid = r.json()["thread_id"]
        assert tid

        # list (static route is NOT captured by /thread/{thread_id})
        r = await http.get("/thread/list")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1 and body["threads"][0]["thread_id"] == tid

        # get
        r = await http.get(f"/thread/{tid}")
        assert r.status_code == 200 and r.json()["name"] == "Glioma"

        # update
        r = await http.put(f"/thread/{tid}", json={"name": "Glioma v2"})
        assert r.status_code == 200 and r.json()["name"] == "Glioma v2"

        # preference set + get
        r = await http.put(f"/thread/{tid}/preference", json={"key": "default_stage", "value": "read"})
        assert r.status_code == 200 and r.json()["preferences"] == {"default_stage": "read"}
        r = await http.get(f"/thread/{tid}/preference")
        assert r.status_code == 200 and r.json()["preferences"] == {"default_stage": "read"}

        # soft-delete → gone from list, 404 on get
        r = await http.delete(f"/thread/{tid}")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (await http.get("/thread/list")).json()["count"] == 0
        assert (await http.get(f"/thread/{tid}")).status_code == 404
        # update / preference on a missing (deleted) thread → 404 (not 500)
        assert (await http.put(f"/thread/{tid}", json={"name": "z"})).status_code == 404
        assert (await http.get(f"/thread/{tid}/preference")).status_code == 404
        assert (await http.put(f"/thread/{tid}/preference", json={"key": "k", "value": 1})).status_code == 404


@pytest.mark.asyncio
async def test_thread_routes_503_when_memory_unavailable(monkeypatch):
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from omicsclaw.surfaces.desktop import server

    monkeypatch.setattr(server, "_memory_client", None)
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://t") as http:
        assert (await http.get("/thread/list")).status_code == 503
        assert (await http.post("/thread/create", json={"name": "x"})).status_code == 503
        assert (await http.get("/thread/abc")).status_code == 503
