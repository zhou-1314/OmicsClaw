"""Bridge between QueryEngineCallbacks.on_context_compacted and the SSE queue.

Lives in its own module so the wiring is unit-testable without importing
the rest of ``server.py`` (which carries fastapi / httpx imports).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, Union

from omicsclaw.runtime.context.compaction import (
    CompactionEvent,
    build_compaction_status_payload,
)

logger = logging.getLogger(__name__)

# The handler accepts either the in-process ``CompactionEvent`` dataclass or
# the neutral dict payload the ADR 0006 dispatcher delivers via
# ``ContextCompacted.payload``.
CompactionEventOrPayload = Union[CompactionEvent, Mapping[str, Any]]


def _coerce_compaction_event(event: CompactionEventOrPayload) -> CompactionEvent:
    """Normalise a dispatch payload dict back into a ``CompactionEvent``.

    The ADR 0006 dispatcher serialises the event to a neutral dict before it
    crosses the Surface boundary (``dataclasses.asdict``). Reconstruct it here,
    tolerating extra/missing keys, so the desktop Surface can reuse
    :func:`build_compaction_status_payload`. A ``CompactionEvent`` is returned
    unchanged.
    """
    if isinstance(event, CompactionEvent):
        return event
    data = dict(event)
    stages = data.get("applied_stages") or ()
    return CompactionEvent(
        messages_compressed=int(data.get("messages_compressed", 0) or 0),
        tokens_saved_estimate=int(data.get("tokens_saved_estimate", 0) or 0),
        applied_stages=tuple(stages),
    )


def make_compaction_event_handler(
    queue: Any,
) -> Callable[[CompactionEventOrPayload], None]:
    """Return a handler that serialises a compaction event and pushes it
    onto the desktop server's SSE queue as a 'status' frame.

    The handler is **synchronous** — call it, never ``await`` it. It accepts
    either a :class:`CompactionEvent` or the neutral payload dict the
    dispatcher delivers. The queue is duck-typed: anything with a
    ``put_nowait(item)`` method works (asyncio.Queue, but also test doubles).
    """

    def handle(event: CompactionEventOrPayload) -> None:
        try:
            payload = build_compaction_status_payload(_coerce_compaction_event(event))
            queue.put_nowait(
                {"type": "status", "data": json.dumps(payload)}
            )
        except Exception:
            logger.warning(
                "Failed to push compaction event to SSE queue; ignoring.",
                exc_info=True,
            )

    return handle


__all__ = ["make_compaction_event_handler"]
