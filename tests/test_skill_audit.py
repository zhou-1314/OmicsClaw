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
    CachedRevisionResolver,
    CurrentRevision,
    ProtocolEvaluationResult,
    SkillAuditRuntime,
    SkillIdentityInput,
    SkillRevision,
    SkillExperienceView,
    derive_experience_view,
)


def _pr(*, protocol_id="p1", kind="fixture", digest="d1", outcome="succeeded",
        occurred_at="2026-07-23T00:00:00Z"):
    return ProtocolEvaluationResult(protocol_id, kind, digest, outcome, occurred_at)

REV = SkillRevision(skill_id="sc-de", version="1.0.0", manifest_hash="m1", source_hash="s1")
BENCH_DATASET = "sha256:" + "a" * 64
BENCH_ENVIRONMENT = "sha256:" + "b" * 64
BENCH_CONTRACT = {
    "kind": "benchmark",
    "repeats": 1,
    "pass_rule": "all_runs",
    "dataset_ref": {
        "store": "repository",
        "path": "data/benchmarks/omicbench/omicbench-A03_hvg",
        "content_sha256": BENCH_DATASET,
    },
    "environment_id": BENCH_ENVIRONMENT,
}


def _benchmark_result(**overrides):
    values = {
        "protocol_id": "p1",
        "kind": "benchmark",
        "protocol_digest": "d1",
        "outcome": "succeeded",
        "occurred_at": "2026-07-23T00:00:00Z",
        "result_id": "result-1",
        "evaluation_id": "batch-1",
        "environment_id": BENCH_ENVIRONMENT,
        "dataset_digest": BENCH_DATASET,
    }
    values.update(overrides)
    return ProtocolEvaluationResult(**values)


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


# ---- protocol-aware effective validation (M-B2) ----------------------------


def test_fresh_passing_fixture_protocol_lifts_effective_to_fixture_validated():
    v = derive_experience_view(
        REV, "fixture-validated", [],
        protocol_results=[_pr(kind="fixture", digest="d1")],
        current_protocol_digests={"p1": "d1"},
    )
    assert v.effective_validation_level == "fixture-validated"
    assert v.validation_state == "current"


def test_fresh_passing_benchmark_protocol_earns_benchmarked():
    v = derive_experience_view(
        REV, "benchmarked", [],
        protocol_results=[_benchmark_result()],
        current_protocol_digests={"p1": "d1"},
        current_protocol_contracts={"p1": BENCH_CONTRACT},
    )
    assert v.effective_validation_level == "benchmarked"
    assert v.validation_state == "current"


def test_protocol_level_requires_one_complete_all_successful_evaluation_batch():
    contract = {**BENCH_CONTRACT, "repeats": 2}
    partial_failure = [
        _benchmark_result(
            occurred_at="t1", run_index=0, repeats=2, evaluation_id="batch-1"
        ),
        _benchmark_result(
            outcome="failed",
            occurred_at="t2",
            run_index=1,
            repeats=2,
            result_id="result-2",
            evaluation_id="batch-1",
        ),
    ]
    failed_view = derive_experience_view(
        REV,
        "benchmarked",
        [],
        protocol_results=partial_failure,
        current_protocol_digests={"p1": "d1"},
        current_protocol_contracts={"p1": contract},
    )
    assert failed_view.effective_validation_level == "smoke-only"

    complete_success = [
        _benchmark_result(
            occurred_at="t1", run_index=0, repeats=2, evaluation_id="batch-2"
        ),
        _benchmark_result(
            occurred_at="t2",
            run_index=1,
            repeats=2,
            result_id="result-2",
            evaluation_id="batch-2",
        ),
    ]
    passed_view = derive_experience_view(
        REV,
        "benchmarked",
        [],
        protocol_results=complete_success,
        current_protocol_digests={"p1": "d1"},
        current_protocol_contracts={"p1": contract},
    )
    assert passed_view.effective_validation_level == "benchmarked"


def test_experience_view_exposes_evidence_supported_level_above_declared():
    view = derive_experience_view(
        REV,
        "smoke-only",
        [],
        protocol_results=[
            _benchmark_result(occurred_at="t")
        ],
        current_protocol_digests={"p1": "d1"},
        current_protocol_contracts={"p1": BENCH_CONTRACT},
    )

    assert view.effective_validation_level == "smoke-only"
    assert view.evidence_supported_validation_level == "benchmarked"
    assert view.to_dict()["evidence_supported_validation_level"] == "benchmarked"


def test_benchmark_cannot_self_declare_or_mix_dataset_and_environment():
    unbound = derive_experience_view(
        REV,
        "benchmarked",
        [],
        protocol_results=[_benchmark_result()],
        current_protocol_digests={"p1": "d1"},
    )
    assert unbound.evidence_supported_validation_level == "smoke-only"

    contract = {**BENCH_CONTRACT, "repeats": 2}
    mixed = [
        _benchmark_result(run_index=0, repeats=2),
        _benchmark_result(
            run_index=1,
            repeats=2,
            result_id="result-2",
            dataset_digest="sha256:" + "c" * 64,
            environment_id="sha256:" + "d" * 64,
        ),
    ]
    view = derive_experience_view(
        REV,
        "benchmarked",
        [],
        protocol_results=mixed,
        current_protocol_digests={"p1": "d1"},
        current_protocol_contracts={"p1": contract},
    )
    assert view.evidence_supported_validation_level == "smoke-only"


def test_drifted_protocol_digest_earns_nothing():
    v = derive_experience_view(
        REV, "fixture-validated", [],
        protocol_results=[_pr(kind="fixture", digest="OLD")],
        current_protocol_digests={"p1": "NEW"},
    )
    assert v.effective_validation_level == "smoke-only"
    assert v.validation_state == "evaluation_required"


def test_failing_protocol_earns_nothing_but_is_evidence():
    v = derive_experience_view(
        REV, "fixture-validated", [],
        protocol_results=[_pr(kind="fixture", digest="d1", outcome="failed")],
        current_protocol_digests={"p1": "d1"},
    )
    assert v.effective_validation_level == "smoke-only"
    assert v.validation_state == "evaluation_required"  # fresh result is evidence


def test_current_defect_caps_supported_even_with_passing_protocol():
    events = [_ev(outcome="failed", error_kind=SkillErrorKind.CONTRACT_FAILURE.value)]
    v = derive_experience_view(
        REV, "fixture-validated", events,
        protocol_results=[_pr(kind="fixture", digest="d1")],
        current_protocol_digests={"p1": "d1"},
    )
    assert v.effective_validation_level == "smoke-only"
    assert v.validation_state == "review_required"


def test_protocol_effective_never_exceeds_declared():
    v = derive_experience_view(
        REV, "demo-validated", [],
        protocol_results=[_pr(kind="fixture", digest="d1")],
        current_protocol_digests={"p1": "d1"},
    )
    assert v.effective_validation_level == "demo-validated"
    assert v.validation_state == "current"


def test_last_observed_at_includes_protocol_result_time():
    events = [_ev(occurred_at="2026-07-23T00:00:01Z")]
    v = derive_experience_view(
        REV, "smoke-only", events,
        protocol_results=[_pr(digest="d1", occurred_at="2026-07-23T00:00:09Z")],
        current_protocol_digests={"p1": "d1"},
    )
    assert v.last_observed_at == "2026-07-23T00:00:09Z"


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


def test_runtime_targeted_experience_resolves_only_the_selected_skill():
    ledger = _FakeLedger([])
    computed: list[str] = []
    resolver = CachedRevisionResolver(
        lambda: [
            _sii(skill_id="skill-a", cache_key="/skills/a"),
            _sii(skill_id="skill-b", cache_key="/skills/b"),
        ],
        lambda key: (computed.append(key) or ("m-" + key[-1], "s-" + key[-1])),
    )
    runtime = SkillAuditRuntime(ledger, resolver)

    view = runtime.experience_view("skill-b")

    assert view is not None
    assert view.skill_revision.skill_id == "skill-b"
    assert computed == ["/skills/b"]


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


def test_runtime_feeds_protocol_results_into_effective():
    ledger = _FakeLedger([])  # no execution events; protocol evidence alone
    rev = SkillRevision("sc-de", "1.0.0", "m1", "s1")
    cr = CurrentRevision(rev, "fixture-validated", protocol_digests={"p1": "d1"})
    results = {rev: [ProtocolEvaluationResult("p1", "fixture", "d1", "succeeded", "t")]}
    runtime = SkillAuditRuntime(
        ledger, lambda: [cr], protocol_results=lambda r: results.get(r, [])
    )
    view = runtime.experience_views()[0]
    assert view.effective_validation_level == "fixture-validated"
    assert view.validation_state == "current"


def test_runtime_without_protocol_source_stays_execution_only():
    ledger = _FakeLedger([_ev(evidence_kind="demo")])
    cr = CurrentRevision(
        SkillRevision("sc-de", "1.0.0", "m1", "s1"), "fixture-validated",
        protocol_digests={"p1": "d1"},
    )
    runtime = SkillAuditRuntime(ledger, lambda: [cr])  # no protocol source
    view = runtime.experience_views()[0]
    assert view.effective_validation_level == "demo-validated"  # protocols ignored
    assert view.validation_state == "evaluation_required"


def test_runtime_protocol_digest_drift_earns_nothing():
    ledger = _FakeLedger([])
    rev = SkillRevision("sc-de", "1.0.0", "m1", "s1")
    cr = CurrentRevision(rev, "fixture-validated", protocol_digests={"p1": "NEW"})
    results = {rev: [ProtocolEvaluationResult("p1", "fixture", "OLD", "succeeded", "t")]}
    runtime = SkillAuditRuntime(
        ledger, lambda: [cr], protocol_results=lambda r: results.get(r, [])
    )
    assert runtime.experience_views()[0].effective_validation_level == "smoke-only"


def test_summary_can_reuse_precomputed_views():
    ledger = _FakeLedger([_ev(evidence_kind="demo")])
    runtime = SkillAuditRuntime(ledger, lambda: [_cr()])
    views = runtime.experience_views()
    before = ledger.reads
    runtime.summary(views)  # passing views must not re-read the ledger
    assert ledger.reads == before


# ---- CachedRevisionResolver (increment 3a) ---------------------------------


def _sii(skill_id="sc-de", version="1.0.0", declared="demo-validated",
         cache_key="/skills/sc-de", mtime="t0"):
    return SkillIdentityInput(skill_id, version, declared, cache_key, mtime)


def test_resolver_computes_identity_and_builds_current_revision():
    computed: list[str] = []

    def compute(key):
        computed.append(key)
        return ("m-" + key[-1], "s-" + key[-1])

    resolver = CachedRevisionResolver(lambda: [_sii(cache_key="/skills/a")], compute)
    revs = resolver()
    assert computed == ["/skills/a"]
    assert revs == [
        CurrentRevision(SkillRevision("sc-de", "1.0.0", "m-a", "s-a"), "demo-validated")
    ]


def test_resolver_can_filter_before_expensive_identity_computation():
    computed: list[str] = []
    resolver = CachedRevisionResolver(
        lambda: [
            _sii(skill_id="a", cache_key="/skills/a"),
            _sii(skill_id="b", cache_key="/skills/b"),
        ],
        lambda key: (computed.append(key) or ("m", "s")),
    )

    assert [revision.revision.skill_id for revision in resolver.resolve({"b"})] == ["b"]
    assert computed == ["/skills/b"]


def test_resolver_cache_hit_skips_recompute_when_mtime_unchanged():
    calls: list[str] = []

    def compute(key):
        calls.append(key)
        return ("m1", "s1")

    inputs = [_sii(mtime="t0")]
    resolver = CachedRevisionResolver(lambda: list(inputs), compute)
    resolver()
    resolver()  # same mtime -> cache hit, no recompute
    assert calls == ["/skills/sc-de"]  # computed exactly once


def test_resolver_recomputes_when_mtime_changes():
    calls: list[str] = []

    def compute(key):
        calls.append(key)
        return ("m", "s")

    state = {"inputs": [_sii(mtime="t0")]}
    resolver = CachedRevisionResolver(lambda: list(state["inputs"]), compute)
    resolver()
    state["inputs"] = [_sii(mtime="t1")]  # source bytes changed
    resolver()
    assert calls == ["/skills/sc-de", "/skills/sc-de"]  # recomputed on mtime change


def test_resolver_invalidate_forces_recompute():
    calls: list[str] = []

    def compute(key):
        calls.append(key)
        return ("m", "s")

    resolver = CachedRevisionResolver(lambda: [_sii(mtime="t0")], compute)
    resolver()
    resolver.invalidate()
    resolver()
    assert calls == ["/skills/sc-de", "/skills/sc-de"]


def test_resolver_feeds_runtime_end_to_end():
    ledger = _FakeLedger([_ev(evidence_kind="demo")])  # sc-de m1/s1
    resolver = CachedRevisionResolver(
        lambda: [_sii(cache_key="/skills/sc-de", mtime="t0")],
        lambda key: ("m1", "s1"),
    )
    runtime = SkillAuditRuntime(ledger, resolver)
    views = runtime.experience_views()
    assert len(views) == 1
    assert views[0].skill_revision == SkillRevision("sc-de", "1.0.0", "m1", "s1")
    assert views[0].validation_state == "current"


# ---- governance snapshot wiring (increment 3b) -----------------------------


class _FakeAuditRuntime:
    """Returns canned Experience Views + successive summaries (last repeats)."""

    def __init__(self, summaries=None, views=()):
        self._views = tuple(views)
        self._summaries = list(summaries) if summaries else [{"total_skills": len(views)}]
        self.calls = 0

    def experience_views(self):
        return list(self._views)

    def summary(self, views=None):
        idx = min(self.calls, len(self._summaries) - 1)
        self.calls += 1
        return dict(self._summaries[idx])


def _view(skill_id, *, state="current", declared="demo-validated"):
    return SkillExperienceView(
        skill_revision=SkillRevision(skill_id, "1.0.0", "m", "s"),
        declared_validation_level=declared,
        evidence_supported_validation_level="demo-validated",
        effective_validation_level="demo-validated",
        validation_state=state,
        last_observed_at="",
        usage={"execution_count": 0, "routing_count": 0, "explicit_count": 0},
        health={"successes": 0, "skill_defects": 0, "environment_failures": 0, "framework_failures": 0},
    )


def _governance(tmp_path, *, audit_runtime):
    from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
    from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance

    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        audit_runtime=audit_runtime,
    )


def test_governance_snapshot_is_additive_and_preserves_legacy(tmp_path):
    gov = _governance(tmp_path, audit_runtime=_FakeAuditRuntime([{"total_skills": 7}]))
    snap = gov.snapshot()
    # legacy contract unchanged (an old App still consumes these):
    assert isinstance(snap["proposals"], list)
    assert isinstance(snap["health"], list)
    # additive ADR-0074 fields:
    assert snap["schema_version"] == 1
    assert isinstance(snap["authority_epoch"], str) and len(snap["authority_epoch"]) == 32
    assert snap["snapshot_revision"] == 0  # no refresh yet
    assert "generated_at" in snap
    assert "experience_view" in snap["capabilities"]
    assert snap["summary"] == {"total_skills": 7}


def test_snapshot_projects_once_then_reads_the_cache(tmp_path):
    fake = _FakeAuditRuntime([{"total_skills": 1}])
    gov = _governance(tmp_path, audit_runtime=fake)
    calls_after_init = fake.calls  # one call from summary([]) in __init__
    gov.snapshot()
    calls_after_first_read = fake.calls
    # The first audit read projects, so a reader is never told "no evidence"
    # just because no refresh has happened yet in this process.
    assert calls_after_first_read == calls_after_init + 1
    gov.snapshot()
    gov.snapshot()
    assert fake.calls == calls_after_first_read  # later GETs never recompute


def test_refresh_bumps_snapshot_revision_when_summary_changes(tmp_path):
    # Summaries are consumed by: __init__, the first-read projection, refresh.
    fake = _FakeAuditRuntime(
        [{"total_skills": 1}, {"total_skills": 1}, {"total_skills": 2}]
    )
    gov = _governance(tmp_path, audit_runtime=fake)
    assert gov.snapshot()["snapshot_revision"] == 0  # projection matched init
    gov.refresh()  # empty skills_root -> no proposals; _recompute sees a changed summary
    snap = gov.snapshot()
    assert snap["snapshot_revision"] == 1
    assert snap["summary"] == {"total_skills": 2}


def test_refresh_keeps_revision_when_summary_unchanged(tmp_path):
    fake = _FakeAuditRuntime([{"total_skills": 5}])  # always the same
    gov = _governance(tmp_path, audit_runtime=fake)
    gov.refresh()
    assert gov.snapshot()["snapshot_revision"] == 0  # identical content -> no bump


# ---- real registry-backed resolver builder ---------------------------------


def _write_minimal_skill(skills_root, *, skill_id="aud-skill", domain="spatial",
                         version="1.0.0", level="smoke-only", protocols=None):
    import yaml

    skill_dir = skills_root / domain / skill_id
    skill_dir.mkdir(parents=True)
    script = skill_id.replace("-", "_") + ".py"
    (skill_dir / script).write_text("if __name__ == '__main__':\n    pass\n", encoding="utf-8")
    validation = {"level": level}
    if protocols:
        validation["protocols"] = protocols
        for proto in protocols:  # materialize each protocol entry so its digest is real
            entry = skill_dir / proto["entry"]
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(f"# protocol {proto['id']}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 2, "id": skill_id, "name": skill_id, "domain": domain,
            "version": version,
            "summary": {
                "load_when": "audit resolver test",
                "skip_when": [{"condition": "n/a", "use": "another fixture"}],
                "trigger_keywords": ["audit"],
            },
            "runtime": {"entry": script},
            "type": "leaf",
            "lifecycle": {"status": "mvp"},
            "validation": validation,
        }),
        encoding="utf-8",
    )
    return skill_dir


def test_governance_experience_view_reflects_stored_protocol_evaluation(tmp_path):
    # End-to-end read path: a skill declaring a fixture protocol + a stored,
    # digest-matching evaluation result -> the live experience view reports
    # fixture-validated / current (ADR 0074 M-C).
    from omicsclaw.skill.evaluation_run import EvaluationResultStore
    from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
    from omicsclaw.skill.evolution_governance import (
        SkillEvolutionGovernance,
        _build_registry_revision_resolver,
    )

    skills_root = tmp_path / "skills"
    _write_minimal_skill(
        skills_root, skill_id="aud-skill", version="1.0.0", level="fixture-validated",
        protocols=[{"id": "p1", "kind": "fixture", "entry": "tests/t.py"}],
    )
    eval_store = EvaluationResultStore(tmp_path / "evals.jsonl")
    gov = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        evaluation_store=eval_store,
    )

    # Before any evaluation: declared fixture-validated but no evidence -> stale/eval.
    gov.refresh()
    assert gov.experience_view("aud-skill")["validation_state"] == "evaluation_required"

    # Store a passing result whose digest matches the manifest-derived protocol digest.
    cr = _build_registry_revision_resolver(skills_root)()[0]
    eval_store.append(
        cr.revision,
        ProtocolEvaluationResult(
            "p1",
            "fixture",
            cr.protocol_digests["p1"],
            "succeeded",
            "t",
            environment_id=cr.protocol_contracts["p1"]["environment_id"],
        ),
    )
    gov.refresh()
    view = gov.experience_view("aud-skill")
    assert view["effective_validation_level"] == "fixture-validated"
    assert view["validation_state"] == "current"


def test_governance_evaluate_runs_stores_and_lifts_effective(tmp_path):
    # evaluate() with an injected runner runs each declared protocol, stores the
    # digest-bound results, refreshes, and the view reflects the earned level.
    from omicsclaw.skill.evaluation_run import EvaluationResultStore
    from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
    from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance

    skills_root = tmp_path / "skills"
    _write_minimal_skill(
        skills_root, skill_id="aud-skill", version="1.0.0", level="fixture-validated",
        protocols=[{"id": "p1", "kind": "fixture", "entry": "tests/t.py"}],
    )
    gov = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        evaluation_store=EvaluationResultStore(tmp_path / "evals.jsonl"),
    )
    results = gov.evaluate("aud-skill", run_one=lambda spec: "succeeded")
    assert [(r.protocol_id, r.outcome) for r in results] == [("p1", "succeeded")]
    assert results[0].environment_id.startswith("sha256:")
    view = gov.experience_view("aud-skill")
    assert view["effective_validation_level"] == "fixture-validated"
    assert view["validation_state"] == "current"


def test_governance_default_command_runner_keeps_resolvable_artifacts(tmp_path):
    from omicsclaw.skill.evaluation_run import (
        EvaluationArtifactStore,
        EvaluationResultStore,
    )
    from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
    from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance

    skills_root = tmp_path / "skills"
    skill_dir = _write_minimal_skill(
        skills_root,
        skill_id="aud-skill",
        protocols=[
            {
                "id": "fixture-command",
                "kind": "fixture",
                "entry": "tests/fixture_command.py",
                "runner": "command",
            }
        ],
    )
    (skill_dir / "tests" / "fixture_command.py").write_text(
        """
import json
import os
from pathlib import Path

print("governed command evidence")
Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(json.dumps({
    "schema_version": 1,
    "outcome": "succeeded",
    "reason_code": "none",
    "metrics": {},
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result_store = EvaluationResultStore(tmp_path / "audit" / "evals.jsonl")
    artifact_store = EvaluationArtifactStore(tmp_path / "audit" / "artifacts")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        evaluation_store=result_store,
        evaluation_artifact_store=artifact_store,
    )

    [result] = governance.evaluate("aud-skill")

    [bundle_ref] = [
        ref
        for ref in result.evidence_refs
        if ref.startswith("evaluation-artifact:sha256:")
    ]
    bundle = artifact_store.read_json(bundle_ref)
    stdout = next(item for item in bundle["artifacts"] if item["role"] == "stdout")
    assert artifact_store.read_bytes(stdout["ref"]) == b"governed command evidence\n"
    revision = governance._revision_resolver()[0].revision
    assert result_store.results_for(revision) == [result]
    assert revision.skill_id == "aud-skill"


def test_governance_evaluate_unknown_skill_raises(tmp_path):
    from omicsclaw.skill.evaluation_run import EvaluationResultStore
    from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
    from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance

    (tmp_path / "skills").mkdir()
    gov = SkillEvolutionGovernance(
        skills_root=tmp_path / "skills",
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        evaluation_store=EvaluationResultStore(tmp_path / "evals.jsonl"),
    )
    with pytest.raises(KeyError):
        gov.evaluate("nope", run_one=lambda spec: "succeeded")


def test_governance_audit_read_fails_closed_on_corrupt_evaluation_store(tmp_path):
    from omicsclaw.skill.evaluation_run import (
        EvaluationResultStore,
        EvaluationStoreCorruptError,
    )
    from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
    from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance

    skills_root = tmp_path / "skills"
    _write_minimal_skill(skills_root, skill_id="aud-skill")
    store_path = tmp_path / "evals.jsonl"
    store_path.write_text("{not-json\n", encoding="utf-8")
    gov = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        evaluation_store=EvaluationResultStore(store_path),
    )

    with pytest.raises(EvaluationStoreCorruptError):
        gov.experience_view("aud-skill")


def test_build_registry_resolver_empty_root_is_empty(tmp_path):
    from omicsclaw.skill.evolution_governance import _build_registry_revision_resolver

    (tmp_path / "skills").mkdir()
    assert _build_registry_revision_resolver(tmp_path / "skills")() == []


def test_build_registry_resolver_computes_real_identity(tmp_path):
    from omicsclaw.skill.evolution_governance import _build_registry_revision_resolver

    skills_root = tmp_path / "skills"
    _write_minimal_skill(skills_root, skill_id="aud-skill", version="1.0.0",
                         level="demo-validated")
    revs = _build_registry_revision_resolver(skills_root)()
    assert len(revs) == 1
    cr = revs[0]
    assert cr.revision.skill_id == "aud-skill"
    assert cr.revision.version == "1.0.0"
    assert cr.declared_validation_level == "demo-validated"
    # real, computed identity (not the unknown-fallback)
    assert cr.revision.manifest_hash not in ("", "unknown")
    assert cr.revision.source_hash not in ("", "unknown")


# ---- per-skill Experience View detail + pagination (increment 4) -----------


def test_experience_view_detail_and_unknown(tmp_path):
    fake = _FakeAuditRuntime(views=[_view("a"), _view("b", state="stale")])
    gov = _governance(tmp_path, audit_runtime=fake)
    gov.refresh()  # populates the cached views
    assert gov.experience_view("b")["validation_state"] == "stale"
    assert gov.experience_view("b")["skill_revision"]["skill_id"] == "b"
    assert gov.experience_view("zzz") is None


def test_experience_page_paginates_with_opaque_cursor(tmp_path):
    fake = _FakeAuditRuntime(views=[_view("a"), _view("b"), _view("c")])
    gov = _governance(tmp_path, audit_runtime=fake)
    gov.refresh()
    page1 = gov.experience_page(limit=2)
    assert [s["skill_revision"]["skill_id"] for s in page1["skills"]] == ["a", "b"]
    assert page1["next_cursor"]  # more to come
    page2 = gov.experience_page(cursor=page1["next_cursor"], limit=2)
    assert [s["skill_revision"]["skill_id"] for s in page2["skills"]] == ["c"]
    assert page2["next_cursor"] is None  # exhausted


def test_experience_page_filters_by_state(tmp_path):
    fake = _FakeAuditRuntime(
        views=[_view("a", state="current"), _view("b", state="stale"),
               _view("c", state="current")]
    )
    gov = _governance(tmp_path, audit_runtime=fake)
    gov.refresh()
    got = gov.experience_page(state="current")
    assert [s["skill_revision"]["skill_id"] for s in got["skills"]] == ["a", "c"]


def test_experience_page_clamps_limit_and_rejects_bad_cursor(tmp_path):
    fake = _FakeAuditRuntime(views=[_view(f"s{i:02d}") for i in range(5)])
    gov = _governance(tmp_path, audit_runtime=fake)
    gov.refresh()
    assert len(gov.experience_page(limit=10_000)["skills"]) == 5  # clamped, all returned
    with pytest.raises(ValueError):
        gov.experience_page(cursor="not+valid+base64+@@")


def test_experience_read_models_project_on_first_read(tmp_path):
    gov = _governance(tmp_path, audit_runtime=_FakeAuditRuntime(views=[_view("a")]))
    page = gov.experience_page()  # no explicit refresh needed to read the fleet
    assert [v["skill_revision"]["skill_id"] for v in page["skills"]] == ["a"]
    assert gov.experience_view("a") is not None


# ---- AUD-10: stability dispersion aggregation -------------------------------


def _sr(protocol_id="s1", kind="stability", digest="d1", outcome="succeeded",
        run_index=0, repeats=1, metrics=None, *, evaluation_id="batch-1",
        environment_id="env-1", dataset_digest="data-1", occurred_at="t"):
    return ProtocolEvaluationResult(
        protocol_id,
        kind,
        digest,
        outcome,
        occurred_at,
        run_index,
        repeats,
        metrics or {},
        evaluation_id=evaluation_id,
        environment_id=environment_id,
        dataset_digest=dataset_digest,
    )


def test_stability_view_aggregates_repeats_and_metric_dispersion():
    results = [
        _sr(run_index=0, repeats=3, metrics={"silhouette": 0.80}),
        _sr(run_index=1, repeats=3, metrics={"silhouette": 0.82}),
        _sr(run_index=2, repeats=3, outcome="failed", metrics={"silhouette": 0.60}),
    ]
    view = derive_experience_view(REV, "fixture-validated", [],
                                  protocol_results=results,
                                  current_protocol_digests={"s1": "d1"})
    stab = view.stability["s1"]
    assert stab["kind"] == "stability"
    assert stab["repeats"] == 3 and stab["runs"] == 3 and stab["successes"] == 2
    assert stab["success_rate"] == round(2 / 3, 4)
    assert stab["outcomes_consistent"] is False
    disp = stab["metric_dispersion"]["silhouette"]
    assert disp["count"] == 3 and disp["min"] == 0.6 and disp["max"] == 0.82
    assert disp["stddev"] > 0


def test_stability_is_orthogonal_and_only_fresh():
    fresh = _sr(protocol_id="s1", digest="d1", metrics={"m": 1.0})
    drifted = _sr(protocol_id="s2", digest="OLD", metrics={"m": 9.0})
    view = derive_experience_view(REV, "fixture-validated", [],
                                  protocol_results=[fresh, drifted],
                                  current_protocol_digests={"s1": "d1", "s2": "NEW"})
    # A stability protocol earns no validation level; with no demo/fixture
    # evidence the effective level stays at the floor (orthogonal to stability).
    assert view.effective_validation_level == "smoke-only"
    assert "s1" in view.stability
    assert "s2" not in view.stability  # drifted digest contributes no stability


def test_stability_view_never_mixes_independent_evaluation_batches():
    results = [
        _sr(
            run_index=0,
            repeats=2,
            metrics={"silhouette": 0.10},
            evaluation_id="older-batch",
            occurred_at="2026-08-01T00:00:01Z",
        ),
        _sr(
            run_index=1,
            repeats=2,
            metrics={"silhouette": 0.20},
            evaluation_id="older-batch",
            occurred_at="2026-08-01T00:00:02Z",
        ),
        _sr(
            run_index=0,
            repeats=2,
            metrics={"silhouette": 0.90},
            evaluation_id="latest-batch",
            occurred_at="2026-08-02T00:00:01Z",
        ),
        _sr(
            run_index=1,
            repeats=2,
            metrics={"silhouette": 1.00},
            evaluation_id="latest-batch",
            occurred_at="2026-08-02T00:00:02Z",
        ),
    ]

    view = derive_experience_view(
        REV,
        "smoke-only",
        [],
        protocol_results=results,
        current_protocol_digests={"s1": "d1"},
    )

    stability = view.stability["s1"]
    assert stability["evaluation_id"] == "latest-batch"
    assert stability["batch_count"] == 2
    assert stability["runs"] == 2
    assert stability["metric_dispersion"]["silhouette"]["mean"] == 0.95


def test_stability_view_keeps_environment_and_dataset_batches_separate():
    results = [
        _sr(
            metrics={"score": 0.1},
            evaluation_id="same-id",
            environment_id="env-1",
            dataset_digest="data-1",
            occurred_at="2026-08-01T00:00:00Z",
        ),
        _sr(
            metrics={"score": 0.9},
            evaluation_id="same-id",
            environment_id="env-2",
            dataset_digest="data-2",
            occurred_at="2026-08-02T00:00:00Z",
        ),
    ]

    view = derive_experience_view(
        REV,
        "smoke-only",
        [],
        protocol_results=results,
        current_protocol_digests={"s1": "d1"},
    )

    stability = view.stability["s1"]
    assert stability["batch_count"] == 2
    assert stability["environment_id"] == "env-2"
    assert stability["dataset_digest"] == "data-2"
    assert stability["runs"] == 1


# ---- protocol_digest dependency-version binding (ADR 0074 §6.4) -------------


def test_manifest_protocol_digest_binds_declared_dependency_versions(tmp_path, monkeypatch):
    import omicsclaw.skill.evolution_governance as gov_mod
    from omicsclaw.skill.evolution_governance import _manifest_protocol_digests
    from omicsclaw.skill.schema import parse_skill_manifest

    def _manifest(deps):
        d = {
            "schema_version": 2, "id": "dep-skill", "name": "dep-skill",
            "domain": "spatial", "version": "1.0.0",
            "summary": {"load_when": "dep binding test", "trigger_keywords": ["dep"]},
            "runtime": {"entry": "dep_skill.py"}, "type": "leaf",
            "lifecycle": {"status": "mvp"},
            "validation": {"level": "fixture-validated",
                           "protocols": [{"id": "p1", "kind": "fixture", "entry": "tests/t.py"}]},
        }
        if deps is not None:
            d["deps"] = {"python": deps}
        return parse_skill_manifest(d)

    skill_dir = tmp_path / "dep-skill"
    (skill_dir / "tests").mkdir(parents=True)
    (skill_dir / "tests" / "t.py").write_text("# p1\n", encoding="utf-8")

    monkeypatch.setattr(gov_mod, "_installed_dependency_version", lambda pkg: "1.10.0")
    with_dep = _manifest_protocol_digests(_manifest(["scanpy>=1.9"]), skill_dir)["p1"]
    no_dep = _manifest_protocol_digests(_manifest(None), skill_dir)["p1"]
    assert with_dep != no_dep  # a declared dependency participates in the digest

    monkeypatch.setattr(gov_mod, "_installed_dependency_version", lambda pkg: "1.11.0")
    upgraded = _manifest_protocol_digests(_manifest(["scanpy>=1.9"]), skill_dir)["p1"]
    assert upgraded != with_dep  # a version change re-digests, staling prior evidence


def test_dependency_versions_resolve_against_the_skill_runner_interpreter(monkeypatch):
    """ADR 0074 §6.4 binds the digest to the env that RUNS the Skill.

    Skills execute in ``get_skill_runner_python()``, which diverges from
    ``sys.executable`` whenever ``OMICSCLAW_RUN_PYTHON`` is set. Resolving
    in-process would judge runner-earned evidence against the orchestrator's
    environment and silently drop it as stale, so the probe must follow the
    runner — and two orchestrators sharing one runner must agree.
    """
    import omicsclaw.skill.evolution_governance as gov_mod

    probed: list[str] = []

    def _fake_probe(executable: str):
        probed.append(executable)
        return {"scanpy": "9.9.9"}

    monkeypatch.setattr(gov_mod, "_runner_distribution_versions", _fake_probe)
    monkeypatch.setattr(
        "omicsclaw.skill.execution.python_runtime.get_skill_runner_python",
        lambda: "/runner/bin/python",
    )

    assert gov_mod._installed_dependency_version("scanpy") == "9.9.9"
    assert probed == ["/runner/bin/python"]  # not sys.executable


def test_explicit_environment_cache_invalidation_observes_runtime_upgrades(monkeypatch):
    import sys
    import omicsclaw.skill.evolution_governance as gov_mod

    versions = {"scanpy": "1.10.0"}
    monkeypatch.setattr(
        gov_mod,
        "_local_distribution_versions",
        lambda: dict(versions),
    )
    monkeypatch.setattr(
        "omicsclaw.skill.execution.python_runtime.get_skill_runner_python",
        lambda: sys.executable,
    )
    gov_mod._invalidate_evaluation_environment_caches()
    try:
        assert gov_mod._installed_dependency_version("scanpy") == "1.10.0"
        versions["scanpy"] = "1.11.0"
        assert gov_mod._installed_dependency_version("scanpy") == "1.10.0"

        gov_mod._invalidate_evaluation_environment_caches()
        assert gov_mod._installed_dependency_version("scanpy") == "1.11.0"
    finally:
        gov_mod._invalidate_evaluation_environment_caches()


def test_unprobeable_runner_reads_unresolved_not_the_orchestrator_versions(monkeypatch):
    """A runner we cannot probe must stale evidence, never fake freshness."""
    import omicsclaw.skill.evolution_governance as gov_mod

    monkeypatch.setattr(gov_mod, "_runner_distribution_versions", lambda _exe: None)
    monkeypatch.setattr(
        "omicsclaw.skill.execution.python_runtime.get_skill_runner_python",
        lambda: "/broken/python",
    )

    resolved = gov_mod._installed_dependency_version("scanpy")
    assert resolved == "unresolved"
    # Distinct from "probed and absent", so the two never collide in a digest.
    assert resolved != "missing"


def test_local_distribution_versions_keep_the_first_path_entry(monkeypatch):
    """A shadowed duplicate must not overwrite the one Python actually imports.

    ``distributions()`` yields every copy on the path (a user-site ``torch`` in
    front of the env's, say); import resolution takes the FIRST, so the digest
    must record that one to describe the environment the Skill really ran in.
    """
    import omicsclaw.skill.evolution_governance as gov_mod

    class _Dist:
        def __init__(self, name, version):
            self.metadata = {"Name": name}
            self.version = version

    monkeypatch.setattr(
        gov_mod.importlib.metadata,
        "distributions",
        lambda: iter([_Dist("torch", "2.5.1"), _Dist("torch", "2.12.0")]),
    )
    assert gov_mod._local_distribution_versions()["torch"] == "2.5.1"


def test_distribution_lookup_normalizes_pep503_names(monkeypatch):
    """``STAGATE-pyG`` in a manifest must match the ``stagate_pyg`` distribution."""
    import omicsclaw.skill.evolution_governance as gov_mod

    monkeypatch.setattr(
        gov_mod, "_runner_distribution_versions", lambda _exe: {"stagate-pyg": "1.0.0"}
    )
    monkeypatch.setattr(
        "omicsclaw.skill.execution.python_runtime.get_skill_runner_python",
        lambda: "/runner/bin/python",
    )
    assert gov_mod._installed_dependency_version("STAGATE-pyG") == "1.0.0"
    assert gov_mod._installed_dependency_version("stagate_pyg") == "1.0.0"


def test_view_reports_the_protocol_ids_the_revision_declares():
    """A consumer must be able to tell "no protocol declared" apart from
    "declared but never run" — otherwise every Skill gets a run-evaluation
    action that can only ever return zero results."""
    declared = derive_experience_view(
        REV, "smoke-only", [], current_protocol_digests={"p2": "d2", "p1": "d1"}
    )
    assert declared.declared_protocol_ids == ("p1", "p2")
    assert declared.to_dict()["declared_protocol_ids"] == ["p1", "p2"]

    undeclared = derive_experience_view(REV, "smoke-only", [])
    assert undeclared.declared_protocol_ids == ()
    assert undeclared.to_dict()["declared_protocol_ids"] == []
