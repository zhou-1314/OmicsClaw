"""Tests for Bench onboarding + global bench preferences (Phase 5,
BE-ONBOARD-8 / BE-PREF-7).

Two layers: the pure service functions over a MemoryClient, and the REST routes
via an in-process ASGI client (httpx). Covers profile persistence to versioned
core://my_user, the skip path, graceful defaults (fresh = onboarded False /
user None / cross_thread_recall False), bench-preference set/get + default-off,
slug-key validation (traversal rejected), re-onboard versioning, and the 503 path.
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
async def test_fresh_status_defaults_gracefully(client):
    from omicsclaw.surfaces.desktop import onboarding

    st = await onboarding.onboarding_status(client)
    assert st == {"onboarded": False, "user": None, "cross_thread_recall": False}


@pytest.mark.asyncio
async def test_onboard_user_persists_profile_and_marks_onboarded(client):
    from omicsclaw.surfaces.desktop import onboarding

    profile = {"name": "Dr Z", "role": "PI", "domains": ["spatial", "singlecell"], "organism": "human"}
    st = await onboarding.onboard_user(client, profile)
    assert st["onboarded"] is True
    assert st["user"] == profile
    # Persists across a fresh status read (i.e. across launches).
    st2 = await onboarding.onboarding_status(client)
    assert st2["onboarded"] is True and st2["user"] == profile


@pytest.mark.asyncio
async def test_skip_marks_onboarded_without_profile(client):
    from omicsclaw.surfaces.desktop import onboarding

    st = await onboarding.skip_onboarding(client)
    assert st["onboarded"] is True
    assert st["user"] is None


@pytest.mark.asyncio
async def test_reonboard_updates_versioned_my_user(client):
    from omicsclaw.surfaces.desktop import onboarding

    await onboarding.onboard_user(client, {"name": "v1"})
    await onboarding.onboard_user(client, {"name": "v2"})
    st = await onboarding.onboarding_status(client)
    assert st["user"] == {"name": "v2"}  # latest version of core://my_user wins


@pytest.mark.asyncio
async def test_bench_preference_default_off_then_set(client):
    from omicsclaw.surfaces.desktop import onboarding

    assert await onboarding.get_bench_preference(client, "cross_thread_recall", default=False) is False
    out = await onboarding.set_bench_preference(client, "cross_thread_recall", True)
    assert out == {"key": "cross_thread_recall", "value": True}
    assert await onboarding.get_bench_preference(client, "cross_thread_recall", default=False) is True
    st = await onboarding.onboarding_status(client)
    assert st["cross_thread_recall"] is True


@pytest.mark.asyncio
async def test_bench_preference_rejects_traversal_key(client):
    from omicsclaw.surfaces.desktop import onboarding

    with pytest.raises(ValueError):
        await onboarding.set_bench_preference(client, "../evil", True)
    # A bad key on read returns the default (never raises).
    assert await onboarding.get_bench_preference(client, "a/b", default="d") == "d"


@pytest.mark.asyncio
async def test_status_tolerates_legacy_plain_rows(client):
    """Legacy/plain (non-JSON) rows degrade gracefully: a bare "true" onboarded
    marker reads as onboarded, and a non-dict my_user blob yields user=None
    rather than crashing."""
    from omicsclaw.surfaces.desktop import onboarding

    await client.remember(uri=onboarding.ONBOARDED_URI, content="true")
    await client.remember(uri=onboarding.MY_USER_URI, content="not-a-json-object")
    st = await onboarding.onboarding_status(client)
    assert st["onboarded"] is True
    assert st["user"] is None


# ---- REST layer ------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboarding_rest_roundtrip(client, monkeypatch):
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from omicsclaw.surfaces.desktop import server

    monkeypatch.setattr(server, "_memory_client", client)
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://t") as http:
        r = await http.get("/onboard/status")
        assert r.status_code == 200, r.text
        assert r.json() == {"onboarded": False, "user": None, "cross_thread_recall": False}

        r = await http.post("/onboard/user", json={"profile": {"name": "Dr Z", "domains": ["spatial"]}})
        assert r.status_code == 200, r.text
        assert r.json()["onboarded"] is True
        assert r.json()["user"] == {"name": "Dr Z", "domains": ["spatial"]}

        r = await http.put("/preference/bench", json={"key": "cross_thread_recall", "value": True})
        assert r.status_code == 200 and r.json() == {"key": "cross_thread_recall", "value": True}
        # The profile + onboarded flag persist into a fresh status read (across launches).
        status = (await http.get("/onboard/status")).json()
        assert status["onboarded"] is True
        assert status["user"] == {"name": "Dr Z", "domains": ["spatial"]}
        assert status["cross_thread_recall"] is True

        # invalid (traversal) key → 400, not 500
        r = await http.put("/preference/bench", json={"key": "../evil", "value": 1})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_onboard_skip_rest(client, monkeypatch):
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from omicsclaw.surfaces.desktop import server

    monkeypatch.setattr(server, "_memory_client", client)
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://t") as http:
        r = await http.post("/onboard/skip")
        assert r.status_code == 200
        body = r.json()
        assert body["onboarded"] is True and body["user"] is None


@pytest.mark.asyncio
async def test_onboarding_routes_503_when_memory_unavailable(monkeypatch):
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from omicsclaw.surfaces.desktop import server

    monkeypatch.setattr(server, "_memory_client", None)
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://t") as http:
        assert (await http.get("/onboard/status")).status_code == 503
        assert (await http.post("/onboard/user", json={"profile": {}})).status_code == 503
        assert (await http.post("/onboard/skip")).status_code == 503
        assert (await http.put("/preference/bench", json={"key": "k", "value": 1})).status_code == 503
