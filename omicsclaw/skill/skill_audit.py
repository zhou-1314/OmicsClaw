"""Derived Skill audit read models (ADR 0074, first implementation slice).

Pure derivation of the per-revision **Skill Experience View** and the
**declared vs effective validation** separation over the *existing*
``SkillHealthLedger`` evidence. There is no new event store, no Evaluation
Protocol schema and no ``AuditOperation`` here — this is the lowest-risk
ADR-0074 slice (see the ADR "first implementation slice" consequence). The
``SkillAuditRuntime`` that reads the real ledger + registry, and the additive
Desktop snapshot fields, land in later slices.

Design references:
``docs/adr/0074-govern-skill-experience-and-continuous-evaluation.md`` and
``docs/design/skill-audit-continuous-evaluation.md`` §3, §6.4, §7.

The module is intentionally pure (no filesystem, no network, no clock). Every
conclusion is bound to an exact Skill revision and derived only from the events
it is given, so the view is rebuildable from the ledger (AUD-02).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# Reuse the ledger's authoritative failure classification and event type so the
# audit view can never drift from how ``SkillHealthLedger.summarize`` counts a
# success or attributes a defect (ADR 0065/0068). These are the single source
# of truth for that classification; duplicating them here would be a latent bug.
from omicsclaw.skill.evolution import (
    SkillRunEvent,
    _ENVIRONMENT_FAILURE_KINDS,
    _FRAMEWORK_FAILURE_KINDS,
    _SKILL_DEFECT_KINDS,
)

__all__ = [
    "VALIDATION_LADDER",
    "SkillRevision",
    "SkillExperienceView",
    "derive_experience_view",
]

# ADR 0074 validation ladder, weakest -> strongest. The values match
# ``skill.yaml`` ``ValidationLevel`` (omicsclaw/skill/schema.py); this tuple adds
# the ordering the ``Literal`` type does not carry.
VALIDATION_LADDER: tuple[str, ...] = (
    "smoke-only",
    "demo-validated",
    "fixture-validated",
    "benchmarked",
    "production",
)

# The existing ``SkillHealthLedger`` can prove at most ``demo-validated``: the
# higher levels require the Evaluation Protocols a later ADR-0074 slice
# introduces. Until then, evidence-supported validation is honestly capped here
# so a manually-declared higher level surfaces as ``evaluation_required`` rather
# than silently claiming protocol evidence that does not exist.
_MAX_EVIDENCE_SUPPORTED_LEVEL = "demo-validated"

# Upper bound on evidence identifiers surfaced in one view (ADR 0074: read
# models are bounded). Newest-first, deduplicated.
_MAX_EVIDENCE_REFS = 20

# Validation-state vocabulary (ADR 0074 §3 "Validation state").
_STATE_CURRENT = "current"
_STATE_STALE = "stale"
_STATE_EVALUATION_REQUIRED = "evaluation_required"
_STATE_REVIEW_REQUIRED = "review_required"


def _ladder_index(level: str) -> int:
    """Rank a level on the ladder; an unknown level ranks at the floor.

    Failing closed to the floor means an unrecognized declared/effective level
    can never be presented as stronger evidence than a known one.
    """
    try:
        return VALIDATION_LADDER.index(level)
    except ValueError:
        return 0


def _min_level(a: str, b: str) -> str:
    return a if _ladder_index(a) <= _ladder_index(b) else b


@dataclass(frozen=True, slots=True)
class SkillRevision:
    """The exact evaluatable Skill state (ADR 0074).

    ``environment_id``, ``protocol_digest`` and ``run_id`` are orthogonal
    evidence dimensions and may never substitute for this revision.
    """

    skill_id: str
    version: str
    manifest_hash: str
    source_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "manifest_hash": self.manifest_hash,
            "source_hash": self.source_hash,
        }


def _event_matches_revision(event: SkillRunEvent, revision: SkillRevision) -> bool:
    return (
        event.skill_id == revision.skill_id
        and event.skill_version == revision.version
        and event.skill_hash == revision.manifest_hash
        and event.source_hash == revision.source_hash
    )


@dataclass(frozen=True, slots=True)
class SkillExperienceView:
    """Rebuildable per-revision experience projection (ADR 0074 §7).

    A projection over the audit ledger — never a free-text note, a second Skill
    source of truth, or a Graph Memory fact. It expresses only what the events
    can prove.
    """

    skill_revision: SkillRevision
    declared_validation_level: str
    effective_validation_level: str
    validation_state: str  # current | stale | evaluation_required | review_required
    last_observed_at: str
    usage: dict[str, int]
    health: dict[str, int]
    evidence_refs: tuple[str, ...] = ()
    # Reserved ADR-0074 §7 fields, populated by later slices (protocols, Gotcha
    # linkage, proposal linkage). Present now so the view schema is stable.
    stability: dict[str, Any] = field(default_factory=dict)
    approved_gotchas: tuple[str, ...] = ()
    coverage_gaps: tuple[str, ...] = ()
    pending_proposal_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_revision": self.skill_revision.to_dict(),
            "declared_validation_level": self.declared_validation_level,
            "effective_validation_level": self.effective_validation_level,
            "validation_state": self.validation_state,
            "last_observed_at": self.last_observed_at,
            "usage": dict(self.usage),
            "health": dict(self.health),
            "stability": dict(self.stability),
            "approved_gotchas": list(self.approved_gotchas),
            "coverage_gaps": list(self.coverage_gaps),
            "pending_proposal_ids": list(self.pending_proposal_ids),
            "evidence_refs": list(self.evidence_refs),
        }


def _bounded_evidence_refs(events: Sequence[SkillRunEvent]) -> tuple[str, ...]:
    """Newest-first, deduplicated, bounded evidence identifiers."""
    seen: set[str] = set()
    refs: list[str] = []
    for event in sorted(events, key=lambda e: e.occurred_at, reverse=True):
        for ref in event.evidence_refs:
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
                if len(refs) >= _MAX_EVIDENCE_REFS:
                    return tuple(refs)
    return tuple(refs)


def derive_experience_view(
    revision: SkillRevision,
    declared_validation_level: str,
    events: Sequence[SkillRunEvent],
) -> SkillExperienceView:
    """Derive one Skill Experience View for an exact revision (ADR 0074).

    ``events`` may contain evidence for any Skill; only events matching
    ``revision`` exactly (id + version + manifest hash + source hash) count
    toward this revision's usage, health and effective validation. Events for a
    *different* revision of the same ``(skill_id, version)`` distinguish
    ``stale`` (drifted since it was evaluated) from ``evaluation_required``
    (never evaluated).

    First-slice semantics: evidence proves at most ``demo-validated`` (a current
    ``demo`` success with no current defect); ``fixture-validated`` and above
    need Evaluation Protocols from a later slice, so a higher *declared* level
    without matching current evidence is honestly reported as
    ``evaluation_required`` (never silently downgraded on disk).
    """
    current = [e for e in events if _event_matches_revision(e, revision)]

    same_id_version_drifted = any(
        e.skill_id == revision.skill_id
        and e.skill_version == revision.version
        and (e.skill_hash != revision.manifest_hash or e.source_hash != revision.source_hash)
        for e in events
    )

    failure_kinds = [e.error_kind for e in current if e.outcome != "succeeded"]
    successes = sum(1 for e in current if e.outcome == "succeeded")
    skill_defects = sum(1 for k in failure_kinds if k in _SKILL_DEFECT_KINDS)
    environment_failures = sum(1 for k in failure_kinds if k in _ENVIRONMENT_FAILURE_KINDS)
    framework_failures = sum(1 for k in failure_kinds if k in _FRAMEWORK_FAILURE_KINDS)

    demo_success = any(
        e.evidence_kind == "demo" and e.outcome == "succeeded" for e in current
    )

    # Highest level the current-revision evidence can prove (first slice).
    if skill_defects or not demo_success:
        supported = "smoke-only"
    else:
        supported = _MAX_EVIDENCE_SUPPORTED_LEVEL

    effective = _min_level(declared_validation_level, supported)

    if skill_defects:
        state = _STATE_REVIEW_REQUIRED
    elif current and _ladder_index(supported) >= _ladder_index(declared_validation_level):
        state = _STATE_CURRENT
    elif current:
        # Current evidence exists but does not reach the declared level.
        state = _STATE_EVALUATION_REQUIRED
    elif same_id_version_drifted:
        # The declared level was likely earned on a prior revision; the code or
        # manifest changed, so that evidence no longer applies to these bytes.
        state = _STATE_STALE
    else:
        state = _STATE_EVALUATION_REQUIRED

    last_observed_at = max((e.occurred_at for e in current), default="")

    return SkillExperienceView(
        skill_revision=revision,
        declared_validation_level=declared_validation_level,
        effective_validation_level=effective,
        validation_state=state,
        last_observed_at=last_observed_at,
        usage={
            "execution_count": len(current),
            "routing_count": 0,
            "explicit_count": 0,
        },
        health={
            "successes": successes,
            "skill_defects": skill_defects,
            "environment_failures": environment_failures,
            "framework_failures": framework_failures,
        },
        evidence_refs=_bounded_evidence_refs(current),
    )
