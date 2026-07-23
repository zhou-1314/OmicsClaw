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
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

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
    "CurrentRevision",
    "SkillAuditRuntime",
    "SkillIdentityInput",
    "CachedRevisionResolver",
    "ProtocolEvaluationResult",
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


def _max_level(a: str, b: str) -> str:
    return a if _ladder_index(a) >= _ladder_index(b) else b


# A passing protocol of this kind earns this validation level (ADR 0074 §6.2).
# ``stability`` is an orthogonal result, not a ladder step; ``production``
# additionally requires human approval and is never earned from evidence alone.
_PROTOCOL_KIND_LEVEL: dict[str, str] = {
    "demo": "demo-validated",
    "fixture": "fixture-validated",
    "benchmark": "benchmarked",
}


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


@dataclass(frozen=True, slots=True)
class ProtocolEvaluationResult:
    """One evaluation outcome bound to a declared protocol (ADR 0074 §6).

    Counts toward effective validation only when it ``succeeded`` AND its
    ``protocol_digest`` still equals the current protocol's digest — i.e. the
    protocol's executable/spec/deps have not drifted since this result was
    produced. Callers pass results already scoped to the target revision.
    """

    protocol_id: str
    kind: str
    protocol_digest: str
    outcome: str
    occurred_at: str = ""


def _protocol_supported_level(
    results: Sequence[ProtocolEvaluationResult],
    current_protocol_digests: Mapping[str, str] | None,
) -> str:
    """Highest ladder level earned by fresh, passing protocol results.

    A result is fresh only if its ``protocol_digest`` matches the current digest
    declared for that ``protocol_id``; a drifted protocol earns nothing.
    """
    digests = current_protocol_digests or {}
    supported = "smoke-only"
    for result in results:
        if result.outcome != "succeeded":
            continue
        if digests.get(result.protocol_id) != result.protocol_digest:
            continue
        level = _PROTOCOL_KIND_LEVEL.get(result.kind)
        if level is not None:
            supported = _max_level(supported, level)
    return supported


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
    protocol_results: Sequence[ProtocolEvaluationResult] = (),
    current_protocol_digests: Mapping[str, str] | None = None,
) -> SkillExperienceView:
    """Derive one Skill Experience View for an exact revision (ADR 0074).

    ``events`` may contain evidence for any Skill; only events matching
    ``revision`` exactly (id + version + manifest hash + source hash) count
    toward this revision's usage, health and effective validation. Events for a
    *different* revision of the same ``(skill_id, version)`` distinguish
    ``stale`` (drifted since it was evaluated) from ``evaluation_required``
    (never evaluated).

    ``protocol_results`` are evaluation outcomes bound to declared Evaluation
    Protocols (ADR 0074 §6); a fresh, passing ``fixture``/``benchmark`` result
    lifts the evidence-supported level above ``demo-validated``. A result counts
    only when its ``protocol_digest`` still matches ``current_protocol_digests``
    for that protocol id, so a drifted protocol earns nothing. Without protocol
    evidence, execution evidence proves at most ``demo-validated`` (a current
    ``demo`` success with no current defect); a higher *declared* level then
    reports ``evaluation_required`` (never silently downgraded on disk).
    ``production`` requires human approval and is never earned from evidence.
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
    demo_supported = "smoke-only" if (skill_defects or not demo_success) else "demo-validated"

    fresh_protocol_results = [
        r
        for r in protocol_results
        if (current_protocol_digests or {}).get(r.protocol_id) == r.protocol_digest
    ]
    supported = _max_level(
        demo_supported,
        _protocol_supported_level(protocol_results, current_protocol_digests),
    )
    if skill_defects:
        # A reproducing current-revision defect caps support at the floor,
        # regardless of any protocol result.
        supported = "smoke-only"

    effective = _min_level(declared_validation_level, supported)

    has_evidence = bool(current) or bool(fresh_protocol_results)
    if skill_defects:
        state = _STATE_REVIEW_REQUIRED
    elif has_evidence and _ladder_index(supported) >= _ladder_index(declared_validation_level):
        state = _STATE_CURRENT
    elif has_evidence:
        # Current evidence exists but does not reach the declared level.
        state = _STATE_EVALUATION_REQUIRED
    elif same_id_version_drifted:
        # The declared level was likely earned on a prior revision; the code or
        # manifest changed, so that evidence no longer applies to these bytes.
        state = _STATE_STALE
    else:
        state = _STATE_EVALUATION_REQUIRED

    last_observed_at = max(
        [e.occurred_at for e in current]
        + [r.occurred_at for r in fresh_protocol_results],
        default="",
    )

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


@dataclass(frozen=True, slots=True)
class CurrentRevision:
    """A Skill's current revision plus its last human-approved declared level.

    Produced by a resolver over the live registry (the resolver — which computes
    the current manifest/source identity — is wired in a later slice); the audit
    runtime consumes it to project experience without touching the filesystem.

    ``protocol_digests`` maps each declared Evaluation Protocol id to its current
    digest, so a stored evaluation result earns a level only while its protocol
    has not drifted (ADR 0074 §6.4). Empty when a Skill declares no protocols.
    """

    revision: SkillRevision
    declared_validation_level: str
    protocol_digests: Mapping[str, str] = field(default_factory=dict)


class _EventSource(Protocol):
    """Structural type for the append-only ledger (``SkillHealthLedger``)."""

    def events(self) -> Sequence[SkillRunEvent]: ...


# A resolver yields the current revision + declared level for each live Skill.
RevisionResolver = Callable[[], Iterable[CurrentRevision]]


class SkillAuditRuntime:
    """Deep read-model Module over audit evidence (ADR 0074).

    Accepts an evidence source (the existing ``SkillHealthLedger`` as an
    Adapter) and a resolver for the current Skill revisions, and produces the
    bounded, rebuildable Experience Views and a snapshot summary. It owns
    identity-scoped aggregation, effective validation and freshness; it never
    mutates canonical Skill files — ``SkillEvolutionGovernance`` remains the sole
    mutation authority, and this Module only ever *reads*.

    Deterministic and side-effect free given a fixed ledger snapshot: it reads
    the ledger's events exactly once per call so every view in one snapshot is
    derived from the same evidence set (AUD-02).
    """

    def __init__(
        self,
        ledger: _EventSource,
        revision_resolver: RevisionResolver,
        protocol_results: Callable[[SkillRevision], Sequence[ProtocolEvaluationResult]] | None = None,
    ):
        self._ledger = ledger
        self._resolve = revision_resolver
        # Optional per-revision protocol-evaluation source (the eval store).
        # When absent, views carry execution evidence only (no protocol lift).
        self._protocol_results = protocol_results

    def experience_views(self) -> list[SkillExperienceView]:
        """One Experience View per current Skill revision, sorted by skill id.

        The resolver decides which revisions are "current"; the ledger supplies
        the execution evidence and (when configured) the evaluation store supplies
        each revision's protocol results. A revision the resolver omits earns no
        view even if the ledger still has its historical events, and evidence for
        a superseded revision surfaces only as the current view's ``stale`` state.
        """
        events = list(self._ledger.events())
        views: list[SkillExperienceView] = []
        for cr in self._resolve():
            results = self._protocol_results(cr.revision) if self._protocol_results else ()
            views.append(
                derive_experience_view(
                    cr.revision,
                    cr.declared_validation_level,
                    events,
                    protocol_results=results,
                    current_protocol_digests=cr.protocol_digests,
                )
            )
        views.sort(key=lambda v: (v.skill_revision.skill_id, v.skill_revision.version))
        return views

    def summary(self, views: Sequence[SkillExperienceView] | None = None) -> dict[str, Any]:
        """Bounded aggregate for the additive Desktop snapshot (ADR 0074 §9.2).

        Closed, count-only shape; no raw payloads. ``by_validation_state`` and
        ``by_declared_level`` always carry every known key (zero-filled) so a
        consumer never has to guess whether an absent key means zero.
        """
        if views is None:
            views = self.experience_views()
        by_state = {
            _STATE_CURRENT: 0,
            _STATE_STALE: 0,
            _STATE_EVALUATION_REQUIRED: 0,
            _STATE_REVIEW_REQUIRED: 0,
        }
        by_declared = {level: 0 for level in VALIDATION_LADDER}
        for view in views:
            if view.validation_state in by_state:
                by_state[view.validation_state] += 1
            if view.declared_validation_level in by_declared:
                by_declared[view.declared_validation_level] += 1
        return {
            "total_skills": len(views),
            "by_validation_state": by_state,
            "by_declared_level": by_declared,
        }


@dataclass(frozen=True, slots=True)
class SkillIdentityInput:
    """The cheap per-Skill inputs a resolver enumerates from the live registry.

    ``cache_key`` identifies the Skill's source location (e.g. its directory);
    ``mtime_signature`` is an opaque token that changes whenever the manifest or
    source bytes could have changed, so the expensive identity computation is
    reused while the Skill is unchanged.
    """

    skill_id: str
    version: str
    declared_validation_level: str
    cache_key: str
    mtime_signature: str


class CachedRevisionResolver:
    """Resolve current ``SkillRevision`` identities with an mtime-gated cache.

    Computing a Skill's ``(manifest_hash, source_hash)`` walks the ADR-0069
    conservative source closure and is too expensive to repeat for every Skill
    on every snapshot. This resolver caches that pair per Skill and recomputes
    only when the Skill's ``mtime_signature`` changes (or after ``invalidate``,
    which the refresh path calls). It is otherwise a thin, deterministic mapping
    from enumerated inputs to ``CurrentRevision`` values — it never mutates
    Skill files.

    ``compute_identity(cache_key)`` returns ``(manifest_hash, source_hash)``.
    """

    def __init__(
        self,
        enumerate_skills: Callable[[], Iterable[SkillIdentityInput]],
        compute_identity: Callable[[str], tuple[str, str]],
    ):
        self._enumerate = enumerate_skills
        self._compute = compute_identity
        # cache_key -> (mtime_signature, (manifest_hash, source_hash))
        self._cache: dict[str, tuple[str, tuple[str, str]]] = {}

    def invalidate(self) -> None:
        """Drop all cached identities (call when Skill sources may have changed)."""
        self._cache.clear()

    def __call__(self) -> list[CurrentRevision]:
        resolved: list[CurrentRevision] = []
        for item in self._enumerate():
            cached = self._cache.get(item.cache_key)
            if cached is not None and cached[0] == item.mtime_signature:
                manifest_hash, source_hash = cached[1]
            else:
                manifest_hash, source_hash = self._compute(item.cache_key)
                self._cache[item.cache_key] = (
                    item.mtime_signature,
                    (manifest_hash, source_hash),
                )
            resolved.append(
                CurrentRevision(
                    SkillRevision(
                        skill_id=item.skill_id,
                        version=item.version,
                        manifest_hash=manifest_hash,
                        source_hash=source_hash,
                    ),
                    item.declared_validation_level,
                )
            )
        return resolved
