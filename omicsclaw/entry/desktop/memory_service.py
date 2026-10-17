"""Remote long-term memory adapter for Desktop and SSH Controller."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from omicsclaw.memory.store import Category, LongTermStore, MemoryEntry

from .turn_submission import DesktopIngressError

__all__ = ["dispatch_memory_request"]

_log = logging.getLogger(__name__)
import re

_CATEGORIES = (
    "core",
    "project",
    "dataset",
    "analysis",
    "finding",
    "preference",
    "insight",
    "workflow",
    "notes",
    "session",
    "general",
    *(c.value for c in Category if c.value not in {"preference"}),
)


def _resolve_store(app: Any) -> LongTermStore | None:
    memory = getattr(app, "memory", None)
    if memory is None:
        return None
    if isinstance(memory, LongTermStore):
        return memory
    store = getattr(memory, "store", None)
    if isinstance(store, LongTermStore):
        return store
    return None


async def _refresh_precis(app: Any) -> None:
    memory = getattr(app, "memory", None)
    precis = getattr(memory, "precis", None)
    if precis is not None and hasattr(precis, "regenerate"):
        try:
            await precis.regenerate()
        except OSError as exc:
            _log.warning("could not rewrite memory precis: %s", exc)


def _normalize_category(raw: Any) -> str:
    value = str(raw or "general").strip().lower()
    if value in _CATEGORIES or re.fullmatch(r"[a-z0-9_-]{1,64}", value):
        return value
    return "general"


def _entry_category(entry: MemoryEntry) -> str:
    cat = entry.category
    return cat.value if isinstance(cat, Category) else str(cat or "general")


def _bounded_int(raw: Any, default: int = 20, maximum: int = 200) -> int:
    try:
        value = int(raw if raw is not None and str(raw).strip() != "" else default)
    except (ValueError, TypeError):
        return default
    return max(1, min(value, maximum))


async def dispatch_memory_request(
    app: Any,
    method: str,
    path: str,
    query: Mapping[str, str],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Handle /memory/* requests against app.memory."""
    store = _resolve_store(app)
    workspace = str(getattr(getattr(app, "config", None), "workspace", ""))

    if method == "GET" and path == "/memory/domains":
        entries = await store.list(limit=500) if store is not None else []
        counts: dict[str, int] = {cat: 0 for cat in _CATEGORIES}
        for entry in entries:
            cat = _entry_category(entry)
            counts[cat] = counts.get(cat, 0) + 1
        domains = [
            {
                "domain": cat,
                "name": cat,
                "node_count": count,
                "count": count,
            }
            for cat, count in counts.items()
        ]
        return {"domains": domains}

    if method == "GET" and path == "/memory/namespaces":
        return {"namespaces": ["default"]}

    if method == "GET" and path in {"/memory/browse", "/memory/children"}:
        domain = str(query.get("domain") or "").strip().lower()
        target_path = str(query.get("path") or "").strip()
        if "://" in target_path:
            prefix, target_path = target_path.split("://", 1)
            if not domain:
                domain = prefix.strip().lower()
        entries = await store.list(limit=500) if store is not None else []
        if not target_path:
            filtered = [
                e
                for e in entries
                if not domain or domain == "all" or _entry_category(e) == domain
            ]
            children = [
                {
                    "id": e.id,
                    "name": e.title or e.id,
                    "title": e.title,
                    "path": e.id,
                    "domain": _entry_category(e),
                    "uri": f"{_entry_category(e)}://{e.id}",
                    "content_snippet": e.content[:200],
                    "priority": e.importance,
                    "approx_children_count": 0,
                }
                for e in filtered
            ]
            return {
                "path": "",
                "domain": domain or "general",
                "node": None,
                "children": children,
                "is_versioned": False,
            }

        matched = next(
            (e for e in entries if e.id == target_path or e.title == target_path),
            None,
        )
        if matched is None:
            return {
                "path": target_path,
                "domain": domain or "general",
                "node": None,
                "children": [],
                "is_versioned": False,
            }
        cat = _entry_category(matched)
        return {
            "path": target_path,
            "domain": cat,
            "node": {
                "id": matched.id,
                "title": matched.title,
                "content": matched.content,
                "content_snippet": matched.content[:200],
                "priority": matched.importance,
                "keywords": list(matched.tags),
                "created_at": str(matched.created_at),
                "updated_at": str(matched.updated_at),
            },
            "children": [],
            "is_versioned": False,
        }

    if method == "GET" and path == "/memory/search":
        q = str(query.get("q") or "").strip()
        limit = _bounded_int(query.get("limit"), default=20)
        entries = (
            await store.search(q, limit=limit)
            if (store is not None and q)
            else []
        )
        return {
            "results": [
                {
                    "uri": f"{_entry_category(e)}://{e.id}",
                    "id": e.id,
                    "title": e.title,
                    "snippet": e.content[:200],
                    "content": e.content,
                    "domain": _entry_category(e),
                    "score": e.importance,
                    "updated_at": e.updated_at,
                }
                for e in entries
            ]
        }

    if method == "GET" and path == "/memory/recent":
        limit = _bounded_int(query.get("limit"), default=20)
        entries = await store.list(limit=limit) if store is not None else []
        items = [
            {
                "uri": f"{_entry_category(e)}://{e.id}",
                "id": e.id,
                "title": e.title,
                "snippet": e.content[:200],
                "content": e.content,
                "domain": _entry_category(e),
                "category": _entry_category(e),
                "score": e.importance,
                "importance": e.importance,
                "updated_at": e.updated_at,
            }
            for e in entries
        ]
        return {"results": items, "items": items}

    if method == "GET" and path == "/memory/scoped":
        limit = _bounded_int(query.get("limit"), default=100)
        entries = await store.list(limit=limit) if store is not None else []
        scoped_workspace = str(query.get("workspace") or workspace)
        items = [
            {
                "id": e.id,
                "memory_id": e.id,
                "title": e.title,
                "content": e.content,
                "description": e.content,
                "category": _entry_category(e),
                "domain": _entry_category(e),
                "scope": "project",
                "owner": "agent",
                "freshness": "stable",
                "importance": e.importance,
                "tags": list(e.tags),
                "keywords": list(e.tags),
                "dataset_refs": [],
                "path": e.id,
                "relative_path": e.id,
                "updated_at": str(e.updated_at),
                "created_at": str(e.created_at),
            }
            for e in entries
        ]
        return {"workspace": scoped_workspace, "items": items, "records": items}

    if method == "POST" and path == "/memory/scoped/prune":
        pruned = 0
        if store is not None:
            pruned += await store.purge_expired()
            ids = document.get("ids")
            if isinstance(ids, list):
                for raw_id in ids:
                    if isinstance(raw_id, str) and raw_id.strip():
                        if await store.soft_delete(raw_id.strip()):
                            pruned += 1
            await _refresh_precis(app)
        return {"ok": True, "pruned": pruned}

    if method == "POST" and path == "/memory/create":
        if store is None:
            raise DesktopIngressError("memory_unavailable", status_code=503)
        title = str(document.get("title") or "").strip()
        content = str(document.get("content") or "").strip()
        if not content and not title:
            raise DesktopIngressError("content_required", status_code=422)
        body = content or title
        category = _normalize_category(
            document.get("category") or document.get("domain")
        )
        raw_imp = document.get("importance", document.get("priority", 5))
        importance = _bounded_int(raw_imp, default=5, maximum=10)
        raw_tags = document.get("tags") or document.get("keywords") or ()
        tags = (
            tuple(str(t) for t in raw_tags if isinstance(t, (str, int, float)))
            if isinstance(raw_tags, (list, tuple))
            else ()
        )
        entry_id = await store.add(
            MemoryEntry(
                title=title or body[:60],
                content=body,
                category=category,
                importance=importance,
                tags=tags,
            )
        )
        await _refresh_precis(app)
        return {"ok": True, "id": entry_id, "uri": f"{category}://{entry_id}"}

    if method == "PUT" and path == "/memory/update":
        if store is None:
            raise DesktopIngressError("memory_unavailable", status_code=503)
        entry_id = str(document.get("id") or document.get("path") or "").strip()
        if "://" in entry_id:
            entry_id = entry_id.split("://", 1)[1]
        if not entry_id:
            raise DesktopIngressError("id_required", status_code=422)
        changes: dict[str, Any] = {}
        if isinstance(document.get("title"), str) and document["title"].strip():
            changes["title"] = document["title"].strip()
        if isinstance(document.get("content"), str):
            changes["content"] = document["content"]
        if document.get("category") or document.get("domain"):
            changes["category"] = _normalize_category(
                document.get("category") or document.get("domain")
            )
        if "importance" in document or "priority" in document:
            changes["importance"] = _bounded_int(
                document.get("importance", document.get("priority")),
                default=5,
                maximum=10,
            )
        if isinstance(document.get("tags"), list):
            changes["tags"] = [str(t) for t in document["tags"]]
        updated = await store.update(entry_id, **changes) if changes else False
        await _refresh_precis(app)
        return {"ok": True, "id": entry_id, "updated": bool(updated)}

    if method == "DELETE" and path == "/memory/delete":
        if store is None:
            raise DesktopIngressError("memory_unavailable", status_code=503)
        entry_id = str(
            query.get("id")
            or query.get("path")
            or document.get("id")
            or document.get("path")
            or ""
        ).strip()
        if "://" in entry_id:
            entry_id = entry_id.split("://", 1)[1]
        if not entry_id:
            raise DesktopIngressError("id_required", status_code=422)
        deleted = await store.soft_delete(entry_id)
        await _refresh_precis(app)
        return {"ok": True, "deleted": bool(deleted), "id": entry_id}

    if method == "GET" and path == "/memory/review/changes":
        return {"changes": []}
    if method == "GET" and path == "/memory/review/orphans":
        return {"orphans": []}
    if method == "GET" and path == "/memory/review/version-chain":
        return {"versions": [], "uri": str(query.get("uri") or "")}
    if method == "POST" and path in {
        "/memory/review/approve",
        "/memory/review/rollback",
        "/memory/review/clear",
        "/memory/glossary/add",
    }:
        return {"ok": True}
    if method == "DELETE" and path == "/memory/glossary/remove":
        return {"ok": True}

    raise DesktopIngressError("unsupported_memory_route", status_code=404)
