"""Background projection sweep service (ADR 0064).

The runnable *trigger* for the projection path: a long-lived background task that
periodically sweeps pending Project Projection Intents and applies them into
Project-scoped Memory. ADR 0064 explicitly allows a background sweep ("Project
archive does not wait for pending Projection Intents"), and this is lower-risk
than coupling into the terminalization hot path — the Producer freezes Intents
in the terminal transaction, and this service applies them out of band.

Composition: the caller injects the control ``repository``, a ``read_source``
(e.g. a ``RunManifestSourceReader`` over ``run_store.read_manifest``), and a
``client_factory`` (namespace -> MemoryClient) over the shared MemoryEngine.
Lifecycle mirrors the Desktop bridge task: ``start()`` then ``await close()``.
"""

from __future__ import annotations

import asyncio
import logging

from omicsclaw.memory.projection import SourceReader
from omicsclaw.memory.projection_driver import (
    ProjectionDriveSummary,
    adrive_pending_projections,
)
from omicsclaw.memory.projection_writer import ClientFactory, MemoryProjectionWriter

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_PROJECTION_INTERVAL_SECONDS", "MemoryProjectionService"]

DEFAULT_PROJECTION_INTERVAL_SECONDS = 30.0


class MemoryProjectionService:
    """Periodically applies pending Project Projection Intents into Memory."""

    def __init__(
        self,
        *,
        repository,
        read_source: SourceReader,
        client_factory: ClientFactory,
        interval_seconds: float = DEFAULT_PROJECTION_INTERVAL_SECONDS,
        limit: int = 100,
    ):
        self._repository = repository
        self._read_source = read_source
        self._writer = MemoryProjectionWriter(client_factory)
        self._interval = max(float(interval_seconds), 0.0)
        self._limit = limit
        self._task: asyncio.Task | None = None

    async def run_once(self) -> ProjectionDriveSummary:
        """Apply one batch of pending Intents; safe to call directly (tests, hooks)."""
        return await adrive_pending_projections(
            self._repository,
            read_source=self._read_source,
            write_projection=self._writer,
            limit=self._limit,
        )

    async def _loop(self) -> None:
        # Mirror the Desktop bridge task: swallow per-sweep errors, re-raise only
        # cancellation. adrive already isolates per-Intent faults, so a raise here
        # is an unexpected repository/infrastructure error, not a bad Intent.
        try:
            while True:
                try:
                    summary = await self.run_once()
                    if summary.applied or summary.failed or summary.deferred:
                        logger.info(
                            "Memory projection sweep: applied=%d failed=%d deferred=%d",
                            summary.applied,
                            summary.failed,
                            len(summary.deferred),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — a sweep error must not kill the loop
                    logger.exception("Memory projection sweep failed")
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("Memory projection service cancelled")
            raise

    def start(self) -> None:
        """Start the background sweep (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="memory-projection")

    async def close(self) -> None:
        """Cancel and await the background sweep."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
