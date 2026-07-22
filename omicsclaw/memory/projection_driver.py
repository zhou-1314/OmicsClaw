"""Projection driver — sweep pending Intents and apply them (ADR 0064).

This is the thin wiring between the control plane's frozen Projection Intents
and the pure :func:`omicsclaw.memory.projection.apply_projection_intent`
applicator. It reads pending Intents across all Projects, applies each with the
real ``finish_project_projection`` as the terminal-mark, and reports a summary.

Robustness for a background sweep: a transient writer/source fault on one
Intent is caught and that Intent is left ``pending`` (deferred) so it retries on
the next sweep, without blocking the other Intents in the batch. Permanent
failures (digest mismatch, source loss) are marked ``failed`` by the applicator,
not deferred.

The driver is Project-lifecycle-blind by construction — it applies pending
Intents whether or not their Project is now archived, which is exactly the
"finish already-accepted work after archive" half of ADR 0064.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from omicsclaw.control.models import ProjectionIntentRecord, StateChangeResult
from omicsclaw.memory.projection import (
    AsyncProjectionWriter,
    ProjectionOutcome,
    ProjectionResult,
    ProjectionWriter,
    SourceReader,
    aapply_projection_intent,
    apply_projection_intent,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectionDriveSummary",
    "drive_pending_projections",
    "adrive_pending_projections",
]


class _ProjectionRepository(Protocol):
    """The narrow control-repository surface the driver depends on."""

    def list_pending_projection_intents(
        self, *, limit: int = 100
    ) -> tuple[ProjectionIntentRecord, ...]: ...

    def finish_project_projection(
        self, projection_intent_id: str, *, state: str, error_code: str | None = None
    ) -> StateChangeResult: ...


@dataclass(frozen=True, slots=True)
class ProjectionDriveSummary:
    processed: int
    applied: int
    failed: int
    deferred: tuple[str, ...] = ()
    outcomes: tuple[ProjectionOutcome, ...] = ()


def drive_pending_projections(
    repository: _ProjectionRepository,
    *,
    read_source: SourceReader,
    write_projection: ProjectionWriter,
    limit: int = 100,
) -> ProjectionDriveSummary:
    """Apply one batch of pending Projection Intents; never raises per-Intent.

    ``read_source`` / ``write_projection`` are the same injected callables the
    applicator documents. Returns counts plus the ids of any Intents deferred by
    a transient fault (left ``pending`` for the next sweep).
    """
    pending = repository.list_pending_projection_intents(limit=limit)
    outcomes: list[ProjectionOutcome] = []
    deferred: list[str] = []
    for intent in pending:
        try:
            outcome = apply_projection_intent(
                intent,
                read_source=read_source,
                write_projection=write_projection,
                finish_intent=repository.finish_project_projection,
            )
        except Exception:  # noqa: BLE001 — a transient sweep fault must not abort the batch
            # The Intent is still pending (the applicator only marks terminal on
            # a settled outcome), so the next sweep retries it. Log and continue.
            logger.warning(
                "Deferring projection intent %s after transient fault",
                intent.projection_intent_id,
                exc_info=True,
            )
            deferred.append(intent.projection_intent_id)
            continue
        outcomes.append(outcome)

    return _summarize(outcomes, deferred)


def _summarize(
    outcomes: list[ProjectionOutcome], deferred: list[str]
) -> ProjectionDriveSummary:
    return ProjectionDriveSummary(
        processed=len(outcomes),
        applied=sum(1 for o in outcomes if o.result is ProjectionResult.APPLIED),
        failed=sum(1 for o in outcomes if o.result is ProjectionResult.FAILED),
        deferred=tuple(deferred),
        outcomes=tuple(outcomes),
    )


async def adrive_pending_projections(
    repository: _ProjectionRepository,
    *,
    read_source: SourceReader,
    write_projection: AsyncProjectionWriter,
    limit: int = 100,
) -> ProjectionDriveSummary:
    """Async twin of :func:`drive_pending_projections` for an async Memory writer.

    Same per-Intent fault isolation: a transient write fault defers that Intent
    (left pending) and the sweep continues.
    """
    pending = repository.list_pending_projection_intents(limit=limit)
    outcomes: list[ProjectionOutcome] = []
    deferred: list[str] = []
    for intent in pending:
        try:
            outcome = await aapply_projection_intent(
                intent,
                read_source=read_source,
                write_projection=write_projection,
                finish_intent=repository.finish_project_projection,
            )
        except Exception:  # noqa: BLE001 — a transient sweep fault must not abort the batch
            logger.warning(
                "Deferring projection intent %s after transient fault",
                intent.projection_intent_id,
                exc_info=True,
            )
            deferred.append(intent.projection_intent_id)
            continue
        outcomes.append(outcome)

    return _summarize(outcomes, deferred)
