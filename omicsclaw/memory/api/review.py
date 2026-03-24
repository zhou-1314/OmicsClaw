"""
Review API — Endpoints for reviewing AI changes and performing rollbacks.

Ported from nocturne_memory with OmicsClaw adaptations.
"""

from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..snapshot import get_changeset_store, _rows_equal

router = APIRouter(prefix="/api/review", tags=["review"])


class RollbackRequest(BaseModel):
    """Request to rollback specific changes."""
    keys: List[str]


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.get("/changes")
async def get_changes():
    """Get all pending AI changes grouped by affected node."""
    store = get_changeset_store()

    # Use get_all_rows_dict to get key→entry mapping
    all_rows = store.get_all_rows_dict()
    # Filter to only net-changed rows
    changed_keys = set()
    for key, entry in all_rows.items():
        table = entry.get("table", "")
        before = entry.get("before")
        after = entry.get("after")
        if not _rows_equal(table, before, after):
            changed_keys.add(key)

    change_count = len(changed_keys)

    # Group by node_uuid, including key in each entry
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for key in changed_keys:
        entry = all_rows[key]
        table = entry.get("table", "unknown")
        before = entry.get("before")
        after = entry.get("after")
        ref = after or before

        # Determine node_uuid from the row
        node_uuid = None
        if ref:
            if table == "nodes":
                node_uuid = ref.get("uuid")
            elif table == "memories":
                node_uuid = ref.get("node_uuid")
            elif table == "edges":
                node_uuid = ref.get("child_uuid")
            elif table == "paths":
                # For paths, we need to find node_uuid via the edge
                node_uuid = ref.get("node_uuid")
                if not node_uuid:
                    edge_id = ref.get("edge_id")
                    if edge_id:
                        edge_key = f"edges:{edge_id}"
                        edge_entry = all_rows.get(edge_key)
                        if edge_entry:
                            edge_ref = edge_entry.get("after") or edge_entry.get("before")
                            if edge_ref:
                                node_uuid = edge_ref.get("child_uuid")
            elif table == "glossary_keywords":
                node_uuid = ref.get("node_uuid")

        if not node_uuid:
            node_uuid = "_unknown_"

        if node_uuid not in groups:
            groups[node_uuid] = []

        # Include the key so the frontend can reference individual changes
        entry_with_key = {**entry, "key": key}
        groups[node_uuid].append(entry_with_key)

    return {
        "change_count": change_count,
        "groups": groups,
    }


@router.get("/change-count")
async def get_change_count():
    """Get the number of pending changes."""
    store = get_changeset_store()
    return {"count": store.get_change_count()}


@router.get("/diff")
async def get_diff(key: str = Query(..., description="Change key to diff")):
    """Get before/after diff for a specific change."""
    store = get_changeset_store()
    all_rows = store.get_all_rows_dict()

    entry = all_rows.get(key)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Change key '{key}' not found")

    table = entry.get("table", "unknown")
    before = entry.get("before")
    after = entry.get("after")

    # For memory rows, resolve content from DB
    if table == "memories":
        from .. import get_graph_service
        graph = get_graph_service()

        if before and "id" in before:
            mem = await graph.get_memory_by_id(before["id"])
            if mem:
                before = {**before, "content": mem.get("content", "")}

        if after and "id" in after:
            mem = await graph.get_memory_by_id(after["id"])
            if mem:
                after = {**after, "content": mem.get("content", "")}

    return {
        "key": key,
        "table": table,
        "before": before,
        "after": after,
    }


@router.post("/rollback")
async def rollback_changes(req: RollbackRequest):
    """Rollback specific changes by their keys."""
    store = get_changeset_store()
    all_rows = store.get_all_rows_dict()

    from .. import get_graph_service
    graph = get_graph_service()

    errors = []
    rolled_back = []

    for key in req.keys:
        entry = all_rows.get(key)
        if not entry:
            errors.append({"key": key, "error": "Not found"})
            continue

        table = entry.get("table", "unknown")
        before = entry.get("before")
        after = entry.get("after")

        try:
            if table == "memories" and before and after:
                # Content was updated: rollback to the old memory version
                old_id = before.get("id")
                if old_id:
                    await graph.rollback_to_memory(old_id)
                    rolled_back.append(key)
                else:
                    errors.append({"key": key, "error": "No old memory ID"})

            elif table == "memories" and before is None and after:
                # Memory was created: we cannot easily undo this
                errors.append({"key": key, "error": "Cannot rollback memory creation"})

            elif table == "paths" and before is None and after:
                # Path was created: remove it
                domain = after.get("domain", "core")
                path = after.get("path", "")
                if path:
                    try:
                        await graph.remove_path(path=path, domain=domain)
                        rolled_back.append(key)
                    except ValueError as e:
                        errors.append({"key": key, "error": str(e)})

            elif table == "paths" and before and after is None:
                # Path was deleted: restore it
                domain = before.get("domain", "core")
                path = before.get("path", "")
                edge_id = before.get("edge_id")
                if path and edge_id:
                    # Find the node_uuid from the edge
                    from ..models import Edge
                    from sqlalchemy import select
                    from .. import get_db_manager
                    db = get_db_manager()
                    async with db.session() as session:
                        edge_result = await session.execute(
                            select(Edge).where(Edge.id == edge_id)
                        )
                        edge = edge_result.scalar_one_or_none()
                        if edge:
                            await graph.restore_path(
                                path=path,
                                domain=domain,
                                node_uuid=edge.child_uuid,
                                session=session,
                            )
                            rolled_back.append(key)
                        else:
                            errors.append({"key": key, "error": "Edge not found"})
                else:
                    errors.append({"key": key, "error": "Missing path or edge_id"})

            else:
                errors.append({"key": key, "error": f"Unsupported rollback for table '{table}'"})

        except Exception as e:
            errors.append({"key": key, "error": str(e)})

    # Remove successfully rolled-back keys from the store
    if rolled_back:
        store.remove_keys(rolled_back)

    return {
        "rolled_back": rolled_back,
        "errors": errors,
        "remaining": store.get_change_count(),
    }


@router.post("/integrate-all")
async def integrate_all():
    """Accept all pending changes (clear the changeset)."""
    store = get_changeset_store()
    count = store.clear_all()
    return {"integrated": count}
