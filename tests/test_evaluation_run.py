"""Tests for the ADR-0074 M-C evaluation result store + orchestration."""

from __future__ import annotations

import pytest

from omicsclaw.skill.evaluation_run import (
    EvaluationResultStore,
    EvaluationStoreCorruptError,
    run_protocol_evaluations,
)
from omicsclaw.skill.skill_audit import ProtocolEvaluationResult, SkillRevision

REV = SkillRevision("sc-de", "1.0.0", "m1", "s1")
OTHER = SkillRevision("sc-de", "1.0.0", "m2", "s2")


def _result(protocol_id="p1", kind="fixture", digest="d1", outcome="succeeded",
            occurred_at="2026-07-23T00:00:00Z"):
    return ProtocolEvaluationResult(protocol_id, kind, digest, outcome, occurred_at)


# ---- store ------------------------------------------------------------------


def test_store_append_and_results_round_trip(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    r = _result()
    store.append(REV, r)
    assert store.results_for(REV) == [r]


def test_store_filters_by_exact_revision(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    store.append(REV, _result(protocol_id="a"))
    store.append(OTHER, _result(protocol_id="b"))
    got = store.results_for(REV)
    assert [x.protocol_id for x in got] == ["a"]


def test_store_missing_file_is_empty(tmp_path):
    assert EvaluationResultStore(tmp_path / "nope.jsonl").results_for(REV) == []


def test_store_preserves_append_order(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    for i in range(3):
        store.append(REV, _result(protocol_id=f"p{i}", occurred_at=f"t{i}"))
    assert [r.protocol_id for r in store.results_for(REV)] == ["p0", "p1", "p2"]


def test_store_corrupt_row_fails_closed(tmp_path):
    path = tmp_path / "evals.jsonl"
    store = EvaluationResultStore(path)
    store.append(REV, _result())
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    with pytest.raises(EvaluationStoreCorruptError):
        store.results_for(REV)


# ---- orchestration ----------------------------------------------------------


def test_run_protocol_evaluations_builds_bound_results():
    protocols = [
        ({"id": "p1", "kind": "fixture"}, "digest-1"),
        ({"id": "p2", "kind": "benchmark"}, "digest-2"),
    ]
    outcomes = {"p1": "succeeded", "p2": "failed"}
    results = run_protocol_evaluations(
        REV, protocols,
        run_one=lambda spec: outcomes[spec["id"]],
        now=lambda: "2026-07-23T12:00:00Z",
    )
    assert [(r.protocol_id, r.kind, r.protocol_digest, r.outcome) for r in results] == [
        ("p1", "fixture", "digest-1", "succeeded"),
        ("p2", "benchmark", "digest-2", "failed"),
    ]
    assert all(r.occurred_at == "2026-07-23T12:00:00Z" for r in results)


def test_run_then_store_then_derive_end_to_end(tmp_path):
    # The results a run produces, once stored, are exactly what results_for returns.
    from omicsclaw.skill.skill_audit import derive_experience_view

    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    results = run_protocol_evaluations(
        REV, [({"id": "p1", "kind": "fixture"}, "d1")],
        run_one=lambda spec: "succeeded",
        now=lambda: "2026-07-23T12:00:00Z",
    )
    for r in results:
        store.append(REV, r)

    fresh = store.results_for(REV)
    view = derive_experience_view(
        REV, "fixture-validated", [],
        protocol_results=fresh, current_protocol_digests={"p1": "d1"},
    )
    assert view.effective_validation_level == "fixture-validated"
    assert view.validation_state == "current"


# ---- AUD-10: repeats + metric allowlist -------------------------------------


def test_repeats_runs_protocol_n_times():
    protocols = [({"id": "s1", "kind": "stability", "repeats": 3}, "d1")]
    results = run_protocol_evaluations(
        REV, protocols, run_one=lambda spec: "succeeded", now=lambda: "t")
    assert len(results) == 3
    assert [r.run_index for r in results] == [0, 1, 2]
    assert all(r.repeats == 3 for r in results)


def test_metrics_are_allowlist_filtered_and_numeric():
    protocols = [({"id": "s1", "kind": "stability", "repeats": 1,
                   "metrics": ["silhouette"]}, "d1")]
    results = run_protocol_evaluations(
        REV, protocols,
        run_one=lambda spec: ("succeeded", {"silhouette": 0.8, "denied": 1.0,
                                            "bad": float("nan"), "notnum": "x"}),
        now=lambda: "t")
    assert results[0].metrics == {"silhouette": 0.8}


def test_string_runner_yields_no_metrics_and_round_trips(tmp_path):
    store = EvaluationResultStore(tmp_path / "ev.jsonl")
    [r] = run_protocol_evaluations(
        REV, [({"id": "s1", "kind": "stability", "repeats": 1, "metrics": ["m"]}, "d")],
        run_one=lambda spec: "failed", now=lambda: "t")
    assert r.metrics == {}
    store.append(REV, r)
    assert store.results_for(REV) == [r]  # run_index/repeats/metrics survive round-trip
