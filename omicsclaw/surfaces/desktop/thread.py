"""Bench investigation-thread CRUD service (Phase 1, BE-THREAD-CRUD-2).

Thread metadata is persisted at ``project://<thread_id>`` as a ``ThreadMemory``
(versioned — namespace_policy). These are pure service functions over a
namespace-bound ``MemoryClient`` (the desktop server passes its
``desktop_namespace()``-bound ``_memory_client``); the REST routes live in
``server.py``. ``thread_id`` is only ever a node-path lookup key, never a
namespace — the client's namespace is fixed, so a client-supplied id cannot
reach another user's data (it simply misses → not found).

Soft-delete sets ``is_deleted=True`` and re-versions (the record + history are
kept); ``list`` / ``get`` hide deleted threads. ``project://`` shares its domain
with the legacy ``ProjectContextMemory``; deserialization is content-authoritative
(``_content_to_memory``), so non-thread nodes under ``project://`` are ignored here.
"""

from __future__ import annotations

import uuid
from typing import Any

from omicsclaw.memory.compat import ThreadMemory, _content_to_memory


def _thread_uri(thread_id: str) -> str:
    return f"project://{thread_id}"


_UPDATABLE = ("name", "description", "domains", "organism", "platforms", "venue")


async def _recall_thread(
    client: Any, thread_id: str, *, include_deleted: bool = False
) -> ThreadMemory | None:
    """Fetch a ThreadMemory by id, or None if missing / not a thread / deleted."""
    rec = await client.recall(_thread_uri(thread_id))
    if rec is None or not getattr(rec, "content", None):
        return None
    obj = _content_to_memory(rec.content, "thread")
    if not isinstance(obj, ThreadMemory):
        return None
    if obj.is_deleted and not include_deleted:
        return None
    return obj


async def create_thread(
    client: Any,
    *,
    name: str,
    description: str = "",
    domains: list[str] | None = None,
    organism: str | None = None,
    platforms: list[str] | None = None,
    venue: str | None = None,
) -> ThreadMemory:
    """Create a thread with a fresh server-generated id; persist + return it."""
    tm = ThreadMemory(
        thread_id=uuid.uuid4().hex,
        name=name,
        description=description or "",
        domains=list(domains or []),
        organism=organism,
        platforms=list(platforms or []),
        venue=venue,
    )
    await client.remember(
        _thread_uri(tm.thread_id), tm.model_dump_json(), disclosure=f"Bench thread {tm.thread_id}"
    )
    return tm


async def get_thread(client: Any, thread_id: str) -> ThreadMemory | None:
    return await _recall_thread(client, thread_id)


async def list_threads(client: Any) -> list[ThreadMemory]:
    """List all non-deleted threads (ThreadMemory nodes under project://)."""
    threads: list[ThreadMemory] = []
    for ref in await client.list_children("project://"):
        rec = await client.recall(ref.uri)
        if rec is None or not getattr(rec, "content", None):
            continue
        obj = _content_to_memory(rec.content, "thread")
        if isinstance(obj, ThreadMemory) and not obj.is_deleted:
            threads.append(obj)
    return threads


async def update_thread(
    client: Any, thread_id: str, updates: dict[str, Any]
) -> ThreadMemory | None:
    """Merge metadata updates into an existing thread (re-versions). None if absent.

    Partial (PATCH-like) semantics: only the fields present in ``updates`` (and
    non-None) overwrite; omitted fields are kept. Clearing an Optional field back
    to null via this endpoint is intentionally not supported in Phase 1.
    """
    tm = await _recall_thread(client, thread_id)
    if tm is None:
        return None
    data = tm.model_dump()
    for key in _UPDATABLE:
        if key in updates and updates[key] is not None:
            data[key] = updates[key]
    merged = ThreadMemory(**data)  # preserves thread_id, created_at, preferences, is_deleted
    await client.remember(
        _thread_uri(thread_id), merged.model_dump_json(), disclosure=f"Bench thread {thread_id} (update)"
    )
    return merged


async def delete_thread(client: Any, thread_id: str) -> bool:
    """Soft-delete: set is_deleted=True + re-version. False if the thread is absent."""
    tm = await _recall_thread(client, thread_id)
    if tm is None:
        return False
    tm.is_deleted = True
    await client.remember(
        _thread_uri(thread_id), tm.model_dump_json(), disclosure=f"Bench thread {thread_id} (deleted)"
    )
    return True


async def get_thread_preferences(client: Any, thread_id: str) -> dict[str, Any] | None:
    tm = await _recall_thread(client, thread_id)
    return None if tm is None else dict(tm.preferences)


async def set_thread_preference(
    client: Any, thread_id: str, key: str, value: Any
) -> ThreadMemory | None:
    """Set one thread preference key (re-versions). None if the thread is absent."""
    tm = await _recall_thread(client, thread_id)
    if tm is None:
        return None
    prefs = dict(tm.preferences)
    prefs[key] = value
    tm.preferences = prefs
    await client.remember(
        _thread_uri(thread_id), tm.model_dump_json(), disclosure=f"Bench thread {thread_id} (pref)"
    )
    return tm
