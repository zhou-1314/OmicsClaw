"""Tests for the ADR-0074 first-slice derived audit read models.

Covers the pure ``derive_experience_view`` derivation: declared vs effective
validation, the four validation states, revision isolation, health
classification reuse, bounded evidence refs, and rebuild determinism (AUD-02).
"""

from __future__ import annotations

import pytest

from omicsclaw.skill.evolution import SkillErrorKind, SkillRunEvent
from omicsclaw.skill.skill_audit import (
    VALIDATION_LADDER,
    CurrentRevision,
    SkillAuditRuntime,
    SkillRevision,
    SkillExperienceView,
    derive_experience_view,
)

REV = SkillRevision(skill_id="sc-de", version="1.0.0", manifest_hash="m1", source_hash="s1")


def _ev(
    *,
    skill_id: str = "sc-de",
    version: str = "1.0.0",
    manifest_hash: str = "m1",
    source_hash: str = "s1",
    outcome: str = "succeeded",
    error_kind: str = "none",
    evidence_kind: str = "ordinary",
    occurred_at: str = "2026-07-23T00:00:00Z",
    environment_id: str = "env1",
    evidence_refs: list[str] | None = None,
    event_id: str = "e1",
) -> SkillRunEvent:
    return SkillRunEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        run_id="",
        skill_id=skill_id,
        skill_version=version,
        skill_hash=manifest_hash,
        environment_id=environment_id,
        outcome=outcome,
        error_kind=error_kind,
        exit_code=0,
        duration_seconds=1.0,
        evidence_kind=evidence_kind,
        source_hash=source_hash,
        evidence_refs=list(evidence_refs or []),
    )


# ---- effective / state derivation ------------------------------------------


def test_current_demo_evidence_matches_declared_demo_validated():
    view = derive_experience_view(
        REV, "demo-validated", [_ev(evidence_kind="demo")]
    )
    assert view.effective_validation_level == "demo-validated"
    assert view.validation_state == "current"
    assert view.usage["execution_count"] == 1
    assert view.health["successes"] == 1


def test_declared_above_evidence_is_evaluation_required_and_capped():
    # Declared fixture-validated but only demo evidence exists → the first slice
    # cannot prove fixture level, so effective caps at demo-validated and the
    # state honestly flags that an evaluation is required (no on-disk demotion).
    view = derive_experience_view(
        REV, "fixture-validated", [_ev(evidence_kind="demo")]
    )
    assert view.effective_validation_level == "demo-validated"
    assert view.validation_state == "evaluation_required"


def test_ordinary_success_alone_does_not_prove_demo():
    # An ordinary (non-demo) success cannot earn demo-validated (ADR 0066).
    view = derive_experience_view(
        REV, "demo-validated", [_ev(evidence_kind="ordinary")]
    )
    assert view.effective_validation_level == "smoke-only"
    assert view.validation_state == "evaluation_required"


def test_current_defect_is_review_required():
    view = derive_experience_view(
        REV,
        "demo-validated",
        [
            _ev(evidence_kind="demo"),
            _ev(
                outcome="failed",
                error_kind=SkillErrorKind.CONTRACT_FAILURE.value,
                event_id="e2",
            ),
        ],
    )
    assert view.validation_state == "review_required"
    assert view.effective_validation_level == "smoke-only"
    assert view.health["skill_defects"] == 1


def test_drift_within_version_is_stale():
    # Evidence exists for the same id+version but different source bytes → stale.
    prior = _ev(source_hash="s0", evidence_kind="demo")
    view = derive_experience_view(REV, "demo-validated", [prior])
    assert view.validation_state == "stale"
    assert view.usage["execution_count"] == 0  # no current-revision evidence


def test_never_evaluated_is_evaluation_required():
    view = derive_experience_view(REV, "demo-validated", [])
    assert view.validation_state == "evaluation_required"
    assert view.effective_validation_level == "smoke-only"


def test_effective_never_exceeds_declared():
    # Declared smoke-only + a demo success → effective stays smoke-only (the
    # excess is a promotion candidate, not a higher effective level), state current.
    view = derive_experience_view(REV, "smoke-only", [_ev(evidence_kind="demo")])
    assert view.effective_validation_level == "smoke-only"
    assert view.validation_state == "current"


# ---- revision isolation + health classification ----------------------------


def test_other_revision_events_do_not_count():
    other = _ev(source_hash="OTHER", evidence_kind="demo", outcome="succeeded")
    also_other = _ev(skill_id="sc-annotate", evidence_kind="demo")
    view = derive_experience_view(REV, "demo-validated", [other, also_other])
    assert view.usage["execution_count"] == 0
    assert view.health["successes"] == 0
    # Same id+version, drifted bytes present → stale, not evaluation_required.
    assert view.validation_state == "stale"


def test_health_classification_reuses_ledger_buckets():
    events = [
        _ev(evidence_kind="demo", event_id="s"),
        _ev(outcome="failed", error_kind=SkillErrorKind.SCRIPT_DEFECT.value, event_id="d"),
        _ev(outcome="failed", error_kind=SkillErrorKind.MISSING_DEPENDENCY.value, event_id="env"),
        _ev(outcome="failed", error_kind=SkillErrorKind.CONTRACT_VALIDATOR_FAILED.value, event_id="fw"),
    ]
    view = derive_experience_view(REV, "smoke-only", events)
    assert view.health == {
        "successes": 1,
        "skill_defects": 1,
        "environment_failures": 1,
        "framework_failures": 1,
    }
    # A framework failure (validator) must NOT read as a skill defect.
    assert view.validation_state == "review_required"  # the script_defect drives it


# ---- bounded evidence refs + determinism -----------------------------------


def test_evidence_refs_are_bounded_deduped_newest_first():
    events = [
        _ev(occurred_at="2026-07-23T00:00:01Z", evidence_refs=["a", "b"], event_id="1"),
        _ev(occurred_at="2026-07-23T00:00:02Z", evidence_refs=["b", "c"], event_id="2"),
    ]
    view = derive_experience_view(REV, "smoke-only", events)
    # newest (…02Z) first, deduped
    assert view.evidence_refs == ("b", "c", "a")


def test_derivation_is_order_independent_and_deterministic():
    events = [
        _ev(evidence_kind="demo", occurred_at="2026-07-23T00:00:02Z", event_id="1"),
        _ev(outcome="failed", error_kind=SkillErrorKind.TIMEOUT.value,
            occurred_at="2026-07-23T00:00:01Z", event_id="2"),
    ]
    a = derive_experience_view(REV, "demo-validated", events)
    b = derive_experience_view(REV, "demo-validated", list(reversed(events)))
    assert a == b  # AUD-02: rebuildable, order-independent


def test_last_observed_at_is_latest_current_event():
    events = [
        _ev(occurred_at="2026-07-23T00:00:01Z", event_id="1"),
        _ev(occurred_at="2026-07-23T00:00:05Z", event_id="2"),
    ]
    view = derive_experience_view(REV, "smoke-only", events)
    assert view.last_observed_at == "2026-07-23T00:00:05Z"


# ---- shape / ladder ---------------------------------------------------------


def test_to_dict_shape_is_stable_and_json_safe():
    view = derive_experience_view(REV, "demo-validated", [_ev(evidence_kind="demo")])
    d = view.to_dict()
    assert d["skill_revision"] == {
        "skill_id": "sc-de", "version": "1.0.0", "manifest_hash": "m1", "source_hash": "s1",
    }
    for key in (
        "declared_validation_level", "effective_validation_level", "validation_state",
        "last_observed_at", "usage", "health", "stability", "approved_gotchas",
        "coverage_gaps", "pending_proposal_ids", "evidence_refs",
    ):
        assert key in d


def test_ladder_order_and_unknown_level_floors():
    assert VALIDATION_LADDER == (
        "smoke-only", "demo-validated", "fixture-validated", "benchmarked", "production",
    )
    # An unknown declared level ranks at the floor: a demo success then "reaches"
    # it, so the state is current rather than a false evaluation_required.
    view = derive_experience_view(REV, "bogus-level", [_ev(evidence_kind="demo")])
    assert view.validation_state == "current"
    assert isinstance(view, SkillExperienceView)


# ---- SkillAuditRuntime (increment 2) ---------------------------------------


class _FakeLedger:
    def __init__(self, events):
        self._events = list(events)
        self.reads = 0

    def events(self):
        self.reads += 1
        return list(self._events)


def _cr(skill_id="sc-de", version="1.0.0", manifest_hash="m1", source_hash="s1",
        declared="demo-validated"):
    return CurrentRevision(
        SkillRevision(skill_id, version, manifest_hash, source_hash), declared
    )


def test_runtime_one_view_per_current_revision_sorted_by_id():
    ledger = _FakeLedger([
        _ev(evidence_kind="demo"),
        _ev(skill_id="sc-annotate", evidence_kind="demo", event_id="2"),
    ])
    revs = [_cr(skill_id="sc-de"), _cr(skill_id="sc-annotate")]
    runtime = SkillAuditRuntime(ledger, lambda: revs)
    views = runtime.experience_views()
    assert [v.skill_revision.skill_id for v in views] == ["sc-annotate", "sc-de"]
    assert all(v.validation_state == "current" for v in views)


def test_runtime_omits_revision_the_resolver_does_not_return():
    # Ledger has sc-de evidence, but the resolver only knows sc-annotate.
    ledger = _FakeLedger([_ev(evidence_kind="demo")])
    runtime = SkillAuditRuntime(ledger, lambda: [_cr(skill_id="sc-annotate", declared="smoke-only")])
    views = runtime.experience_views()
    assert [v.skill_revision.skill_id for v in views] == ["sc-annotate"]
    assert views[0].validation_state == "evaluation_required"  # no matching evidence


def test_runtime_reads_ledger_once_per_snapshot():
    ledger = _FakeLedger([_ev(evidence_kind="demo")])
    runtime = SkillAuditRuntime(ledger, lambda: [_cr(), _cr(skill_id="sc-annotate")])
    runtime.experience_views()
    assert ledger.reads == 1  # one evidence snapshot shared by every view (AUD-02)


def test_summary_is_zero_filled_and_counts_states_and_levels():
    ledger = _FakeLedger([_ev(evidence_kind="demo")])  # sc-de current
    revs = [
        _cr(skill_id="sc-de", declared="demo-validated"),               # current
        _cr(skill_id="sc-x", manifest_hash="mx", source_hash="sx",
            declared="fixture-validated"),                              # evaluation_required
    ]
    runtime = SkillAuditRuntime(ledger, lambda: revs)
    s = runtime.summary()
    assert s["total_skills"] == 2
    assert s["by_validation_state"]["current"] == 1
    assert s["by_validation_state"]["evaluation_required"] == 1
    assert s["by_validation_state"]["stale"] == 0            # zero-filled, present
    assert s["by_validation_state"]["review_required"] == 0
    assert s["by_declared_level"]["demo-validated"] == 1
    assert s["by_declared_level"]["fixture-validated"] == 1
    assert s["by_declared_level"]["production"] == 0          # zero-filled, present


def test_summary_can_reuse_precomputed_views():
    ledger = _FakeLedger([_ev(evidence_kind="demo")])
    runtime = SkillAuditRuntime(ledger, lambda: [_cr()])
    views = runtime.experience_views()
    before = ledger.reads
    runtime.summary(views)  # passing views must not re-read the ledger
    assert ledger.reads == before
