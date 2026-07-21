"""Memory projection applicator (ADR 0064).

ADR 0064's control plane freezes a content-free **Project Projection Intent**
in the same transaction that terminalizes a Project-bound Turn or Run. This
module is the *consumer* half: it applies a pending Intent to Memory exactly
once, idempotently, after verifying the frozen source digest — and it is safe
to run after the Project has been archived, because applying an already-frozen
Intent is completion of accepted work, not novel scientific mutation.

The applicator is deliberately pure and dependency-injected: the caller
supplies how to read the frozen source, how to write the projection into Memory
(idempotent by Intent ID), and how to mark the Intent terminal. That keeps the
safety logic — the part the ADR spends most of its words on — fully testable
without a live control DB or Memory engine, and lets a thin driver (a later
slice) wire the real ControlStateRepository + MemoryClient.

Safety properties (ADR 0064 §"Accepted work may create one frozen Projection
Intent"):
  - **Idempotent / restart-safe** — an already-terminal Intent is a no-op; a
    crash between the write and the mark re-applies the (idempotent) write and
    then marks, because the Intent is still ``pending`` on restart.
  - **Digest-verified** — the source is re-read and its SHA-256 compared to the
    frozen digest; a mismatch fails the Intent rather than writing drift.
  - **Source-loss-safe** — a vanished source fails the Intent for explicit
    repair; it never falls back to a legacy Namespace write.
  - **Archive-independent** — the applicator never consults Project lifecycle,
    so it completes accepted work whether or not the Project is now archived,
    and it can never broaden scope (it writes only the frozen projection to the
    Intent's frozen ``project_id``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from omicsclaw.control.models import ProjectionIntentRecord, StateChangeResult

__all__ = [
    "ProjectionResult",
    "ProjectionOutcome",
    "SourceReader",
    "ProjectionWriter",
    "IntentFinisher",
    "SOURCE_MISSING",
    "DIGEST_MISMATCH",
    "apply_projection_intent",
]

# Permanent-failure codes frozen onto the Intent's ``last_error_code`` for
# explicit repair (ADR 0064: "A mismatched source, digest, Project, or origin
# fails the Intent and requires explicit repair").
SOURCE_MISSING = "source_missing"
DIGEST_MISMATCH = "digest_mismatch"


class ProjectionResult(str, Enum):
    APPLIED = "applied"
    FAILED = "failed"
    ALREADY_APPLIED = "already_applied"
    ALREADY_FAILED = "already_failed"


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    projection_intent_id: str
    result: ProjectionResult
    error_code: str | None = None


class SourceReader(Protocol):
    def __call__(self, *, source_store: str, source_ref: str) -> bytes | None:
        """Return the frozen source bytes, or ``None`` if the source is lost."""


class ProjectionWriter(Protocol):
    def __call__(self, *, intent: ProjectionIntentRecord, content: bytes) -> None:
        """Write EXACTLY the frozen projection into Memory.

        MUST be idempotent by ``intent.projection_intent_id``: a crash after
        this write but before the terminal mark re-runs the whole apply on
        restart, so a second write must not fork a duplicate. A raised error is
        treated as a transient fault — the Intent is left ``pending`` for retry.
        """


class IntentFinisher(Protocol):
    def __call__(
        self, projection_intent_id: str, *, state: str, error_code: str | None = None
    ) -> StateChangeResult:
        """Mark the Intent terminal (``applied``|``failed``), idempotent by ID."""


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def apply_projection_intent(
    intent: ProjectionIntentRecord,
    *,
    read_source: SourceReader,
    write_projection: ProjectionWriter,
    finish_intent: IntentFinisher,
) -> ProjectionOutcome:
    """Apply one pending Projection Intent to Memory, idempotently.

    Returns a :class:`ProjectionOutcome`; a transient :class:`ProjectionWriter`
    fault propagates instead (leaving the Intent ``pending`` for the driver to
    retry). The function never consults Project lifecycle — see the module
    docstring's archive-independence property.
    """
    intent_id = intent.projection_intent_id

    # Restart / duplicate safety: an already-terminal Intent is a no-op. We do
    # not re-read the source or re-write Memory for a settled Intent.
    if intent.state == "applied":
        return ProjectionOutcome(intent_id, ProjectionResult.ALREADY_APPLIED)
    if intent.state == "failed":
        return ProjectionOutcome(
            intent_id, ProjectionResult.ALREADY_FAILED, intent.last_error_code
        )
    # Only ``pending`` remains (the table CHECK-constrains state to these three).

    content = read_source(source_store=intent.source_store, source_ref=intent.source_ref)
    if content is None:
        finish_intent(intent_id, state="failed", error_code=SOURCE_MISSING)
        return ProjectionOutcome(intent_id, ProjectionResult.FAILED, SOURCE_MISSING)

    if _sha256_hex(content) != intent.content_sha256:
        finish_intent(intent_id, state="failed", error_code=DIGEST_MISMATCH)
        return ProjectionOutcome(intent_id, ProjectionResult.FAILED, DIGEST_MISMATCH)

    # Write exactly the frozen projection to the frozen Project. A raised writer
    # error propagates here on purpose: the Intent stays ``pending`` (unmarked)
    # so the driver retries, rather than being permanently failed by an
    # infrastructure blip.
    write_projection(intent=intent, content=content)

    # Commit point. ``finish_intent`` is idempotent by ID, so a concurrent
    # projector that already marked this Intent applied is tolerated — the
    # effect (the frozen write) is done either way.
    finish_intent(intent_id, state="applied")
    return ProjectionOutcome(intent_id, ProjectionResult.APPLIED)
