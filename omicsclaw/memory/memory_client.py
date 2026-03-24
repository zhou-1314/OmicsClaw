"""
MemoryClient — High-level memory API for OmicsClaw multi-agent pipelines.

Provides a simple, intuitive interface for agents to store and retrieve
memories without needing to understand the underlying graph structure.

Usage:
    client = MemoryClient()
    await client.initialize()

    # Store a memory
    await client.remember(
        uri="project://current_research",
        content="Studying TME in PDAC using spatial transcriptomics",
        disclosure="When discussing this research project",
    )

    # Retrieve a memory
    ctx = await client.recall("project://current_research")

    # Search across memories
    results = await client.search("spatial transcriptomics")

    # Boot: load core identity and context
    boot_ctx = await client.boot()
"""

import os
from typing import Optional, Dict, Any, List


def _parse_uri(uri: str) -> tuple:
    """Parse a URI like 'domain://path' into (domain, path)."""
    if "://" in uri:
        domain, path = uri.split("://", 1)
        return domain, path
    return "core", uri


class MemoryClient:
    """High-level memory API for multi-agent pipelines.

    Wraps GraphService with a simple URI-based interface.
    """

    def __init__(self, database_url: Optional[str] = None):
        self._db_url = database_url
        self._initialized = False
        self._graph = None
        self._search = None
        self._db = None

    async def initialize(self):
        """Initialize the database and services."""
        if self._initialized:
            return

        from .database import DatabaseManager
        from .search import SearchIndexer
        from .glossary import GlossaryService
        from .graph import GraphService

        self._db = DatabaseManager(self._db_url)
        await self._db.init_db()

        self._search = SearchIndexer(self._db)
        glossary = GlossaryService(self._db, self._search)
        self._graph = GraphService(self._db, self._search)
        self._initialized = True

    async def _ensure_init(self):
        if not self._initialized:
            await self.initialize()

    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()
        self._initialized = False

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def remember(
        self,
        uri: str,
        content: str,
        priority: int = 0,
        disclosure: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store or update a memory at the given URI.

        If the URI already exists, the memory content is updated.
        If not, a new memory is created.

        Args:
            uri: Memory URI (e.g., "project://current_research")
            content: Memory content text
            priority: Priority (0 = normal)
            disclosure: When to disclose this memory to the user

        Returns:
            Dict with memory details (id, node_uuid, uri, etc.)
        """
        await self._ensure_init()
        domain, path = _parse_uri(uri)

        # Try to update existing
        existing = await self._graph.get_memory_by_path(path, domain)
        if existing and existing.get("id", 0) != 0:
            result = await self._graph.update_memory(
                path=path,
                content=content,
                priority=priority,
                disclosure=disclosure,
                domain=domain,
            )
        else:
            # Create new — ensure parent path exists
            if "/" in path:
                parent_path = path.rsplit("/", 1)[0]
                title = path.rsplit("/", 1)[1]
                # Ensure parent chain exists
                await self._ensure_parent_chain(domain, parent_path)
            else:
                parent_path = ""
                title = path

            result = await self._graph.create_memory(
                parent_path=parent_path,
                content=content,
                priority=priority,
                title=title,
                disclosure=disclosure,
                domain=domain,
            )

        # Record changes to ChangesetStore for Review & Audit UI
        from .snapshot import get_changeset_store
        store = get_changeset_store()
        store.record_many(
            before_state=result.get("rows_before", {}),
            after_state=result.get("rows_after", {}),
        )

        return result

    async def recall(
        self, uri: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a memory by URI.

        Args:
            uri: Memory URI (e.g., "project://current_research")

        Returns:
            Memory dict with content, or None if not found.
        """
        await self._ensure_init()
        domain, path = _parse_uri(uri)
        return await self._graph.get_memory_by_path(path, domain)

    async def forget(self, uri: str) -> Dict[str, Any]:
        """Remove a memory by URI.

        The memory content is preserved but marked deprecated, allowing
        rollback via the review interface.

        Args:
            uri: Memory URI to remove

        Returns:
            Dict with removal details
        """
        await self._ensure_init()
        domain, path = _parse_uri(uri)
        result = await self._graph.remove_path(path=path, domain=domain)
        
        # Record changes to ChangesetStore for Review & Audit UI
        from .snapshot import get_changeset_store
        store = get_changeset_store()
        store.record_many(
            before_state=result.get("rows_before", {}),
            after_state=result.get("rows_after", {}),
        )

        return result

    async def search(
        self,
        query: str,
        limit: int = 10,
        domain: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search across all memories.

        Args:
            query: Search query
            limit: Max results
            domain: Optional domain filter

        Returns:
            List of matching memory dicts
        """
        await self._ensure_init()
        return await self._search.search(query, limit=limit, domain=domain)

    async def list_children(
        self,
        uri: str = "core://",
    ) -> List[Dict[str, Any]]:
        """List direct children of a memory node.

        Args:
            uri: Parent URI (e.g., "project://")

        Returns:
            List of child node dicts
        """
        await self._ensure_init()
        domain, path = _parse_uri(uri)

        if not path:
            from .models import ROOT_NODE_UUID
            return await self._graph.get_children(
                node_uuid=ROOT_NODE_UUID,
                context_domain=domain,
            )

        mem = await self._graph.get_memory_by_path(path, domain)
        if not mem:
            return []

        return await self._graph.get_children(
            node_uuid=mem["node_uuid"],
            context_domain=domain,
            context_path=path,
        )

    async def boot(self) -> str:
        """Load core identity and user context for LLM boot prompt.

        Reads all core:// memories and formats them into a context string.

        Returns:
            Formatted context string for LLM system prompt injection.
        """
        await self._ensure_init()

        core_uris_str = os.getenv(
            "OMICSCLAW_MEMORY_CORE_URIS",
            "core://agent,core://my_user",
        )
        core_uris = [u.strip() for u in core_uris_str.split(",") if u.strip()]

        parts = []
        for uri in core_uris:
            mem = await self.recall(uri)
            if mem and mem.get("content"):
                parts.append(f"[{uri}]\n{mem['content']}")

        return "\n\n".join(parts) if parts else ""

    async def get_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recently updated memories.

        Returns:
            List of recent memory dicts
        """
        await self._ensure_init()
        return await self._graph.get_recent_memories(limit=limit)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_parent_chain(self, domain: str, parent_path: str):
        """Recursively ensure parent nodes exist up to the root."""
        existing = await self._graph.get_memory_by_path(parent_path, domain)
        if existing:
            return

        if "/" in parent_path:
            grandparent = parent_path.rsplit("/", 1)[0]
            title = parent_path.rsplit("/", 1)[1]
            await self._ensure_parent_chain(domain, grandparent)
        else:
            grandparent = ""
            title = parent_path

        await self._graph.create_memory(
            parent_path=grandparent,
            content=f"Container node: {domain}://{parent_path}",
            priority=0,
            title=title,
            domain=domain,
        )
