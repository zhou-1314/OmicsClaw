"""``aggregate_skill_health``: a closed per-skill run-health projection.

The four attribution buckets (skill / environment / framework / cancelled) plus
success do NOT cover every failure kind — ``bad_input``, ``upstream_failed``,
``unknown`` and ``none`` are unattributed. ``aggregate_skill_health`` folds those
into ``other`` so the breakdown always closes:

    success + skill_defect + environment + framework + cancelled + other == total

A UI that summed only the four attribution buckets would otherwise silently
under-100%. These tests pin that invariant and the cross-version/env rollup.
"""

from __future__ import annotations

from omicsclaw.skill.evolution import (
    SkillHealthLedger,
    SkillRunEvent,
    aggregate_skill_health,
)


def _event(skill_id, outcome, error_kind, *, version="1.0.0", skill_hash="hash", n=0):
    return SkillRunEvent(
        event_id=f"{skill_id}-{outcome}-{error_kind}-{n}",
        occurred_at="2026-07-22T00:00:00+00:00",
        run_id=f"run-{n}",
        skill_id=skill_id,
        skill_version=version,
        skill_hash=skill_hash,
        environment_id="env",
        outcome=outcome,
        error_kind=error_kind,
        exit_code=0 if outcome == "succeeded" else 1,
        duration_seconds=1.0,
    )


def _ledger(tmp_path, events):
    ledger = SkillHealthLedger(tmp_path / "skill-runs.jsonl")
    for event in events:
        ledger.append(event)
    return ledger


def test_missing_ledger_is_empty(tmp_path):
    ledger = SkillHealthLedger(tmp_path / "does-not-exist.jsonl")
    assert aggregate_skill_health(ledger) == {}


def test_unattributed_failures_land_in_other_and_sum_closes(tmp_path):
    events = [
        _event("clust", "succeeded", "none", n=0),
        _event("clust", "succeeded", "none", n=1),
        _event("clust", "failed", "script_defect", n=2),  # skill
        _event("clust", "failed", "missing_dependency", n=3),  # environment
        _event("clust", "failed", "contract_validator_failed", n=4),  # framework
        _event("clust", "cancelled", "cancelled", n=5),  # cancelled
        _event("clust", "failed", "bad_input", n=6),  # OTHER
        _event("clust", "failed", "upstream_failed", n=7),  # OTHER
        _event("clust", "failed", "unknown", n=8),  # OTHER
    ]
    summary = aggregate_skill_health(_ledger(tmp_path, events))["clust"]
    d = summary.to_dict()

    assert d["total"] == 9
    assert d["success"] == 2
    assert d["skill_defect"] == 1
    assert d["environment"] == 1
    assert d["framework"] == 1
    assert d["cancelled"] == 1
    assert d["other"] == 3  # bad_input + upstream_failed + unknown
    # The M2 invariant: the breakdown is fully closed, never under-100%.
    assert (
        d["success"]
        + d["skill_defect"]
        + d["environment"]
        + d["framework"]
        + d["cancelled"]
        + d["other"]
    ) == d["total"]
    assert d["completion_rate"] == round(2 / 9, 4)


def test_aggregates_across_versions_and_environments(tmp_path):
    events = [
        _event("de", "succeeded", "none", version="1.0.0", n=0),
        _event("de", "failed", "script_defect", version="2.0.0", n=1),
    ]
    # A different environment/hash on the same skill still folds into one summary.
    other_env = SkillRunEvent(
        event_id="de-env2",
        occurred_at="2026-07-22T00:00:00+00:00",
        run_id="r",
        skill_id="de",
        skill_version="1.0.0",
        skill_hash="h2",
        environment_id="env2",
        outcome="succeeded",
        error_kind="none",
        exit_code=0,
        duration_seconds=1.0,
    )
    summary = aggregate_skill_health(_ledger(tmp_path, events + [other_env]))["de"]

    assert summary.total_count == 3
    assert summary.success_count == 2
    assert summary.skill_defect_count == 1
    assert summary.other_count == 0
