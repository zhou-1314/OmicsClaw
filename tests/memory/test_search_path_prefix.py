"""Tests for path_prefix thread scoping in the search layer (Bench Phase 1, Slice 3).

path_prefix restricts hits to a path segment — the exact prefix OR ``<prefix>/...``
sub-paths — so a thread-scoped recall (Slice 4) can limit to
``project://<thread_id>/*`` / ``analysis://<thread_id>/*``. It scopes on the path
within a domain, is domain-agnostic, respects segment boundaries (not substring),
and is a no-op when empty. Exercises the full MemoryClient.search → engine.search →
SearchIndexer.search chain (MemoryClient.remember auto-vivifies parent nodes).
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
    engine = MemoryEngine(db, SearchIndexer(db))
    c = MemoryClient(engine=engine, namespace="app/userA")
    # Two threads (t-A, t-B) + a boundary decoy (t-AX) across two domains.
    await c.remember("analysis://t-A/sc-de/run1", "glioma differential expression")
    await c.remember("analysis://t-A/sc-de/run2", "glioma differential expression two")
    await c.remember("dataset://t-A/matrix.h5ad", "glioma dataset matrix")
    await c.remember("project://t-A", "glioma thread metadata")
    await c.remember("analysis://t-B/sc-de/run3", "glioma other thread run")
    await c.remember("analysis://t-AX/sc-de/run4", "glioma boundary decoy thread")
    yield c
    await db.close()


def _paths(results):
    # Only leaf rows carry the search content; auto-vivified parents are empty
    # containers and won't match the "glioma" query.
    return sorted(r["path"] for r in results)


@pytest.mark.asyncio
async def test_no_path_prefix_returns_all(client):
    res = await client.search("glioma", limit=50)
    assert _paths(res) == sorted(
        ["t-A/sc-de/run1", "t-A/sc-de/run2", "t-A/matrix.h5ad", "t-A",
         "t-B/sc-de/run3", "t-AX/sc-de/run4"]
    )


@pytest.mark.asyncio
async def test_path_prefix_scopes_to_thread_subtree_across_domains(client):
    res = await client.search("glioma", limit=50, path_prefix="t-A")
    # Only t-A's rows (analysis + dataset leaves + the project:// metadata node
    # itself); t-B and the t-AX boundary decoy are excluded.
    assert _paths(res) == sorted(
        ["t-A/sc-de/run1", "t-A/sc-de/run2", "t-A/matrix.h5ad", "t-A"]
    )


@pytest.mark.asyncio
async def test_path_prefix_excludes_boundary_decoy_not_substring(client):
    res = await client.search("glioma", limit=50, path_prefix="t-A")
    paths = _paths(res)
    # t-AX shares the textual prefix "t-A" but is a different segment → excluded.
    assert not any(p.startswith("t-AX") for p in paths)
    assert "t-B/sc-de/run3" not in paths


@pytest.mark.asyncio
async def test_path_prefix_other_thread(client):
    res = await client.search("glioma", limit=50, path_prefix="t-B")
    assert _paths(res) == ["t-B/sc-de/run3"]


@pytest.mark.asyncio
async def test_path_prefix_in_sqlite_like_fallback(client, monkeypatch):
    """FTS5 is normally available, so the LIKE fallback's path_clause is never
    exercised by the other tests. Force the fallback and confirm path_prefix
    still scopes correctly there too."""
    indexer = client._engine._search

    async def _boom(*a, **k):
        raise RuntimeError("force LIKE fallback")

    monkeypatch.setattr(indexer, "_search_sqlite_fts", _boom)
    res = await client.search("glioma", limit=50, path_prefix="t-A")
    assert _paths(res) == sorted(
        ["t-A/sc-de/run1", "t-A/sc-de/run2", "t-A/matrix.h5ad", "t-A"]
    )


@pytest.mark.asyncio
async def test_path_prefix_escapes_like_wildcards(tmp_path):
    """A LIKE wildcard ('_') in the prefix must match literally, not as a
    wildcard — so prefix 't_A' must not also match 'tXA'."""
    db = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init_db()
    engine = MemoryEngine(db, SearchIndexer(db))
    c = MemoryClient(engine=engine, namespace="app/userA")
    await c.remember("analysis://t_A/run", "glioma underscore literal")
    await c.remember("analysis://tXA/run", "glioma wildcard decoy")
    try:
        res = await c.search("glioma", limit=50, path_prefix="t_A")
        assert _paths(res) == ["t_A/run"]  # the '_' did not act as a wildcard
    finally:
        await db.close()
