"""Project-scoped Memory writer for applied projections (ADR 0064).

The final mile of the projection path: an :class:`AsyncProjectionWriter` that
lands a frozen projection in an **explicit Project scope** of graph Memory,
rather than in a legacy transport-derived ``platform/user_id`` namespace. This
is where the ADR-0064 "end-to-end projection" and "explicit scope" directions
meet — a projected record is owned by its Project, so it is partitioned into a
``project/<project_id>`` namespace and keyed by the frozen Intent ID.
"""

from __future__ import annotations

from typing import Callable

from omicsclaw.control.models import ProjectionIntentRecord
from omicsclaw.memory.memory_client import MemoryClient

__all__ = [
    "ClientFactory",
    "project_scope_namespace",
    "projection_memory_uri",
    "MemoryProjectionWriter",
]

# namespace string -> a MemoryClient bound to that namespace (over a shared engine).
ClientFactory = Callable[[str], MemoryClient]


def project_scope_namespace(project_id: str) -> str:
    """The explicit Project-scope Memory namespace (ADR 0064).

    Not a transport identity: the Project owns the row, so the partition is the
    Project ID, never a Channel sender / Desktop launch / ``platform/user_id``.
    """
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required for a Project-scoped projection")
    return f"project/{pid}"


def projection_memory_uri(intent: ProjectionIntentRecord) -> str:
    """Deterministic per-Intent URI so a re-apply is an idempotent overwrite.

    ``projection://<projection_kind>/<intent_id>`` — the ``projection`` domain
    is overwrite-mode (absent from VERSIONED_PREFIXES) and non-shared, so the
    write stays in the Project namespace and a duplicate apply rewrites the same
    node instead of forking a version. The Intent ID makes the URI unique and
    idempotent, which is exactly the applicator's "idempotent by Intent ID"
    requirement.
    """
    kind = str(intent.projection_kind or "").strip() or "projection"
    return f"projection://{kind}/{intent.projection_intent_id}"


class MemoryProjectionWriter:
    """AsyncProjectionWriter landing frozen projections in Project-scoped Memory."""

    def __init__(self, client_factory: ClientFactory):
        # ``client_factory``: namespace string -> MemoryClient. Injected so the
        # writer never owns engine/DB lifecycle; a runner supplies a factory
        # over its shared MemoryEngine (mirrors CompatMemoryStore._client_for_namespace).
        self._client_factory = client_factory

    async def __call__(self, *, intent: ProjectionIntentRecord, content: bytes) -> None:
        client = self._client_factory(project_scope_namespace(intent.project_id))
        # Frozen scientific projections are text (JSON); a non-UTF-8 source is a
        # genuine data fault, so let the decode raise rather than silently mangle
        # — the driver treats a raised writer fault as transient and retries.
        text = content.decode("utf-8")
        await client.remember(
            uri=projection_memory_uri(intent),
            content=text,
            disclosure=(
                f"Project projection {intent.projection_kind} from "
                f"{intent.origin_kind} {intent.origin_id}"
            ),
        )
