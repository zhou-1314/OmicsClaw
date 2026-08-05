from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omicsclaw.skill.benchmark_campaign import (
    BenchmarkCampaignSpec,
    BenchmarkCaseSpec,
    BenchmarkRunRecord,
    BenchmarkSkillRevision,
    analyze_campaign,
    campaign_digest,
    load_campaign_records,
    render_campaign_markdown,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


def _spec(
    *,
    repeats: int = 2,
    experiment_kind: str = "main_skill_effect",
    anchor_case_ids: tuple[str, ...] = ("case-a", "case-b"),
) -> BenchmarkCampaignSpec:
    conditions, baseline, anchor = {
        "main_skill_effect": (
            ("no_skill", "curated_skill", "self_created_skill"),
            "no_skill",
            "self_created_skill",
        ),
        "memory_ablation": (
            ("memory_off", "memory_on"),
            "memory_off",
            "memory_on",
        ),
        "refinement_ablation": (
            ("original_skill", "refined_skill"),
            "original_skill",
            "refined_skill",
        ),
        "transfer": (
            ("no_skill", "curated_skill", "transferred_skill"),
            "no_skill",
            "transferred_skill",
        ),
    }[experiment_kind]
    return BenchmarkCampaignSpec(
        campaign_id="three-suite-pilot",
        suite_id="example-suite",
        suite_version="2026-08",
        suite_digest=SHA_A,
        subset_kind="preregistered_subset",
        experiment_kind=experiment_kind,
        suite_case_count=10,
        selection_rationale="Two cases selected before execution for a bounded pilot.",
        cases=(
            BenchmarkCaseSpec(
                case_id="case-a",
                domain="singlecell",
                dataset_digest=SHA_B,
                grader_digest=SHA_C,
            ),
            BenchmarkCaseSpec(
                case_id="case-b",
                domain="singlecell",
                dataset_digest=SHA_C,
                grader_digest=SHA_D,
            ),
        ),
        conditions=conditions,
        baseline_condition=baseline,
        coverage_anchor_condition=anchor,
        coverage_anchor_case_ids=anchor_case_ids,
        coverage_manifest_digest=SHA_D,
        repeats=repeats,
        grader_kind="deterministic",
        score_name="native_score",
        pass_threshold=0.7,
        runtime_id=SHA_A,
        model_id="same-model-for-all-conditions",
        agent_revision=SHA_A,
        environment_id=SHA_B,
        tool_policy_id=SHA_C,
        budget_id=SHA_D,
        condition_config_digests={
            condition: (SHA_A, SHA_B, SHA_C)[index % 3]
            for index, condition in enumerate(conditions)
        },
    )


def _record(
    spec: BenchmarkCampaignSpec,
    *,
    serial: int,
    case_id: str,
    condition: str,
    repeat_index: int,
    score: float | None = 1.0,
    coverage_status: str = "covered",
    outcome: str = "graded",
    skill_revisions: tuple[BenchmarkSkillRevision, ...] | None = None,
    reason_code: str | None = None,
) -> BenchmarkRunRecord:
    case = next(case for case in spec.cases if case.case_id == case_id)
    if skill_revisions is None:
        skill_revisions = (
            (
                BenchmarkSkillRevision(
                    skill_id="example-skill",
                    version="1.0.0",
                    manifest_hash=SHA_C,
                    source_hash=SHA_D,
                ),
            )
            if condition != "no_skill" and coverage_status == "covered"
            else ()
        )
    if reason_code is None:
        reason_code = {
            "graded": "none",
            "timeout": "timeout",
            "setup_failure": "setup_failure",
            "infra_failure": "infra_failure",
            "cancelled": "cancelled",
            "not_run": coverage_status,
        }[outcome]
    attempted = coverage_status == "covered"
    artifact_bundle_ref = (
        f"evaluation-artifact:sha256:{serial:064x}" if attempted else None
    )
    grader_evidence_digest = SHA_D if outcome == "graded" else None
    evidence_refs = tuple(
        item
        for item in (artifact_bundle_ref, grader_evidence_digest)
        if item is not None
    )
    return BenchmarkRunRecord(
        campaign_digest=campaign_digest(spec),
        run_id=f"{serial:032x}",
        case_id=case_id,
        condition=condition,
        repeat_index=repeat_index,
        coverage_status=coverage_status,
        outcome=outcome,
        score=score,
        dataset_digest=case.dataset_digest,
        grader_digest=case.grader_digest,
        agent_revision=SHA_A,
        environment_id=SHA_B,
        runtime_id=spec.runtime_id,
        model_id=spec.model_id,
        tool_policy_id=spec.tool_policy_id,
        budget_id=spec.budget_id,
        condition_config_digest=spec.condition_config_digests[condition],
        skill_revisions=skill_revisions,
        reason_code=reason_code,
        trial_id=f"{serial + 1000:032x}" if attempted else None,
        isolation_id=(f"sha256:{serial:064x}" if attempted else None),
        artifact_bundle_ref=artifact_bundle_ref,
        grader_evidence_digest=grader_evidence_digest,
        evidence_refs=evidence_refs,
        duration_seconds=(10.0 + serial if attempted else None),
        input_tokens=(100 + serial if attempted else None),
        output_tokens=(10 + serial if attempted else None),
        turns=(2 if attempted else None),
        cost_usd=(0.01 if attempted else None),
    )


def test_campaign_contract_requires_honest_full_or_preregistered_subset():
    payload = _spec().model_dump()
    payload.update(
        {
            "subset_kind": "full",
            "suite_case_count": 3,
            "selection_rationale": "",
        }
    )
    with pytest.raises(ValidationError, match="declare every suite case"):
        BenchmarkCampaignSpec.model_validate(payload)

    payload["subset_kind"] = "preregistered_subset"
    with pytest.raises(ValidationError, match="selection_rationale"):
        BenchmarkCampaignSpec.model_validate(payload)


def test_campaign_contract_freezes_the_anchor_case_manifest():
    payload = _spec().model_dump(mode="json")
    payload["coverage_anchor_case_ids"] = ["case-a", "not-declared"]
    with pytest.raises(ValidationError, match="anchor cases must be declared"):
        BenchmarkCampaignSpec.model_validate(payload)

    payload = _spec().model_dump(mode="json")
    payload["coverage_anchor_case_ids"] = ["case-a", "case-a"]
    with pytest.raises(ValidationError, match="anchor case IDs must be unique"):
        BenchmarkCampaignSpec.model_validate(payload)


def test_campaign_contract_rejects_mixed_experiment_axes():
    payload = _spec().model_dump(mode="json")
    payload["conditions"] = ["no_skill", "self_created_skill", "memory_on"]
    with pytest.raises(ValidationError, match="main_skill_effect requires conditions"):
        BenchmarkCampaignSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("experiment_kind", "conditions", "baseline", "anchor"),
    [
        (
            "memory_ablation",
            ("memory_off", "memory_on"),
            "memory_off",
            "memory_on",
        ),
        (
            "refinement_ablation",
            ("original_skill", "refined_skill"),
            "original_skill",
            "refined_skill",
        ),
        (
            "transfer",
            ("no_skill", "curated_skill", "transferred_skill"),
            "no_skill",
            "transferred_skill",
        ),
    ],
)
def test_campaign_contract_keeps_orthogonal_experiments_separate(
    experiment_kind, conditions, baseline, anchor
):
    payload = _spec().model_dump(mode="json")
    payload.update(
        {
            "experiment_kind": experiment_kind,
            "conditions": list(conditions),
            "baseline_condition": baseline,
            "coverage_anchor_condition": anchor,
            "condition_config_digests": {
                condition: (SHA_A, SHA_B, SHA_C)[index % 3]
                for index, condition in enumerate(conditions)
            },
        }
    )
    spec = BenchmarkCampaignSpec.model_validate(payload)
    assert spec.conditions == conditions


def test_campaign_digest_binds_conditions_cases_and_runtime():
    spec = _spec()
    changed_condition = _spec(experiment_kind="memory_ablation")
    changed_payload = spec.model_dump()
    changed_payload["runtime_id"] = SHA_B
    changed_runtime = BenchmarkCampaignSpec.model_validate(changed_payload)

    assert campaign_digest(spec) != campaign_digest(changed_condition)
    assert campaign_digest(spec) != campaign_digest(changed_runtime)


def test_strict_denominator_keeps_uncovered_runs_as_zero():
    spec = _spec(anchor_case_ids=("case-a",))
    records = [
        _record(spec, serial=1, case_id="case-a", condition="no_skill", repeat_index=0, score=0.5),
        _record(spec, serial=2, case_id="case-a", condition="no_skill", repeat_index=1, score=1.0),
        _record(spec, serial=3, case_id="case-b", condition="no_skill", repeat_index=0, score=0.0),
        _record(spec, serial=4, case_id="case-b", condition="no_skill", repeat_index=1, score=1.0),
        _record(
            spec,
            serial=5,
            case_id="case-a",
            condition="self_created_skill",
            repeat_index=0,
            score=1.0,
        ),
        _record(
            spec,
            serial=6,
            case_id="case-a",
            condition="self_created_skill",
            repeat_index=1,
            score=0.8,
        ),
        _record(
            spec,
            serial=7,
            case_id="case-b",
            condition="self_created_skill",
            repeat_index=0,
            score=None,
            coverage_status="uncovered",
            outcome="not_run",
        ),
        _record(
            spec,
            serial=8,
            case_id="case-b",
            condition="self_created_skill",
            repeat_index=1,
            score=None,
            coverage_status="uncovered",
            outcome="not_run",
        ),
    ]

    summary = analyze_campaign(spec, records)
    baseline = summary["conditions"]["no_skill"]
    created = summary["conditions"]["self_created_skill"]

    assert baseline["strict_mean_score"] == pytest.approx(0.625)
    assert baseline["strict_pass_rate"] == pytest.approx(0.5)
    assert created["coverage_rate"] == pytest.approx(0.5)
    assert created["strict_mean_score"] == pytest.approx(0.45)
    assert created["covered_mean_score"] == pytest.approx(0.9)
    assert baseline["anchor_covered_mean_score"] == pytest.approx(0.75)
    assert created["anchor_covered_mean_score"] == pytest.approx(0.9)
    assert summary["coverage_anchor_condition"] == "self_created_skill"
    assert summary["anchor_covered_cases"] == 1
    assert created["uncovered_cases"] == 1
    assert summary["comparisons"]["self_created_skill"][
        "strict_score_lift"
    ] == pytest.approx(-0.175)


def test_missing_matrix_cells_are_zero_and_visible():
    spec = _spec(repeats=1)
    records = [
        _record(spec, serial=1, case_id="case-a", condition="no_skill", repeat_index=0),
        _record(spec, serial=2, case_id="case-b", condition="no_skill", repeat_index=0),
        _record(spec, serial=3, case_id="case-a", condition="curated_skill", repeat_index=0),
    ]

    summary = analyze_campaign(spec, records)
    curated = summary["conditions"]["curated_skill"]
    assert curated["complete"] is False
    assert curated["missing_runs"] == 1
    assert curated["missing_cases"] == 1
    assert curated["strict_mean_score"] == pytest.approx(0.5)
    assert curated["outcomes"]["missing"] == 1


def test_zero_threshold_does_not_turn_failed_or_missing_cells_into_passes():
    spec = _spec(repeats=1).model_copy(
        update={"pass_threshold": 0.0}
    )
    records = [
        _record(
            spec,
            serial=1,
            case_id="case-a",
            condition="no_skill",
            repeat_index=0,
            score=0.0,
        ),
        _record(
            spec,
            serial=2,
            case_id="case-b",
            condition="no_skill",
            repeat_index=0,
            score=None,
            outcome="timeout",
        ),
    ]

    condition = analyze_campaign(spec, records)["conditions"]["no_skill"]
    assert condition["strict_passes"] == 1
    assert condition["strict_pass_rate"] == pytest.approx(0.5)


def test_campaign_preserves_bounded_failure_reasons_and_missing_records():
    spec = _spec(repeats=1)
    records = [
        _record(
            spec,
            serial=1,
            case_id="case-a",
            condition="no_skill",
            repeat_index=0,
            score=None,
            outcome="timeout",
        )
    ]

    condition = analyze_campaign(spec, records)["conditions"]["no_skill"]
    assert condition["reason_codes"] == {"missing_record": 1, "timeout": 1}

    payload = records[0].model_dump(mode="json")
    payload["reason_code"] = "none"
    with pytest.raises(ValidationError, match="non-graded runs require a failure reason"):
        BenchmarkRunRecord.model_validate(payload)


def test_campaign_reports_cost_measurement_coverage():
    spec = _spec(repeats=1)
    measured = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
    )
    payload = _record(
        spec,
        serial=2,
        case_id="case-b",
        condition="no_skill",
        repeat_index=0,
    ).model_dump(mode="json")
    for field in (
        "duration_seconds",
        "input_tokens",
        "output_tokens",
        "turns",
        "cost_usd",
    ):
        payload[field] = None
    unmeasured = BenchmarkRunRecord.model_validate(payload)

    condition = analyze_campaign(spec, [measured, unmeasured])["conditions"]["no_skill"]
    assert condition["measurement_counts"] == {
        "duration_seconds": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "turns": 1,
        "cost_usd": 1,
    }
    assert condition["median_duration_seconds"] == pytest.approx(11.0)


def test_stability_uses_complete_covered_repeat_batches():
    spec = _spec()
    records = [
        _record(spec, serial=1, case_id="case-a", condition="no_skill", repeat_index=0, score=1.0),
        _record(spec, serial=2, case_id="case-a", condition="no_skill", repeat_index=1, score=0.8),
        _record(spec, serial=3, case_id="case-b", condition="no_skill", repeat_index=0, score=0.5),
    ]

    stability = analyze_campaign(spec, records)["conditions"]["no_skill"]["stability"]
    assert stability["complete_covered_cases"] == 1
    assert stability["avg_stddev"] == pytest.approx(0.1)
    assert stability["avg_mad"] == pytest.approx(0.1)
    assert stability["nonconstant_cases"] == 1
    assert stability["low_variance_cases"] == 1


def test_analysis_rejects_identity_drift_and_duplicate_cells():
    spec = _spec(repeats=1)
    first = _record(spec, serial=1, case_id="case-a", condition="no_skill", repeat_index=0)
    duplicate_cell = _record(
        spec,
        serial=2,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
        score=0.5,
    )
    with pytest.raises(ValueError, match="duplicate Campaign matrix cell"):
        analyze_campaign(spec, [first, duplicate_cell])

    drifted = first.model_copy(update={"dataset_digest": SHA_D})
    with pytest.raises(ValueError, match="dataset digest mismatch"):
        analyze_campaign(spec, [drifted])


def test_analysis_rejects_anchor_coverage_invented_after_preregistration():
    spec = _spec(repeats=1, anchor_case_ids=("case-a",))
    outside_anchor = _record(
        spec,
        serial=1,
        case_id="case-b",
        condition="self_created_skill",
        repeat_index=0,
    )

    with pytest.raises(ValueError, match="coverage anchor manifest"):
        analyze_campaign(spec, [outside_anchor])


def test_analysis_keeps_missing_anchor_runs_in_the_diagnostic_denominator():
    spec = _spec(repeats=1, anchor_case_ids=("case-a",))
    baseline = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
        score=1.0,
    )

    summary = analyze_campaign(spec, [baseline])
    assert summary["anchor_covered_cases"] == 1
    assert summary["conditions"]["self_created_skill"][
        "anchor_covered_mean_score"
    ] == 0.0


def test_campaign_run_requires_content_addressed_attempt_evidence():
    spec = _spec(repeats=1)
    record = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
    )
    for field in (
        "trial_id",
        "isolation_id",
        "artifact_bundle_ref",
        "grader_evidence_digest",
    ):
        payload = record.model_dump(mode="json")
        payload[field] = None
        with pytest.raises(ValidationError, match="content-addressed evidence"):
            BenchmarkRunRecord.model_validate(payload)


def test_analysis_rejects_reused_trial_or_isolation_identity():
    spec = _spec(repeats=1)
    first = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
    )
    second = _record(
        spec,
        serial=2,
        case_id="case-b",
        condition="no_skill",
        repeat_index=0,
    )
    with pytest.raises(ValueError, match="trial identity was reused"):
        analyze_campaign(spec, [first, second.model_copy(update={"trial_id": first.trial_id})])
    with pytest.raises(ValueError, match="isolation identity was reused"):
        analyze_campaign(
            spec,
            [first, second.model_copy(update={"isolation_id": first.isolation_id})],
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("agent_revision", "agent revision mismatch"),
        ("environment_id", "environment identity mismatch"),
        ("runtime_id", "runtime identity mismatch"),
        ("model_id", "model identity mismatch"),
        ("tool_policy_id", "tool policy identity mismatch"),
        ("budget_id", "budget identity mismatch"),
        ("condition_config_digest", "condition configuration identity mismatch"),
    ],
)
def test_analysis_rejects_run_identity_drift(field, message):
    payload = _spec(repeats=1).model_dump(mode="json")
    payload["agent_revision"] = SHA_A
    payload["environment_id"] = SHA_B
    spec = BenchmarkCampaignSpec.model_validate(payload)
    record = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
    )

    with pytest.raises(ValueError, match=message):
        analyze_campaign(spec, [record.model_copy(update={field: "drifted"})])


def test_skill_conditions_bind_consistent_exact_skill_revisions():
    spec = _spec(repeats=2)
    revision = BenchmarkSkillRevision(
        skill_id="generated-case-a",
        version="1.0.0",
        manifest_hash=SHA_C,
        source_hash=SHA_D,
    )
    no_skill = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="no_skill",
        repeat_index=0,
    )
    payload = no_skill.model_dump(mode="json")
    payload["skill_revisions"] = [revision.model_dump(mode="json")]
    with pytest.raises(ValidationError, match="no_skill records cannot bind Skills"):
        BenchmarkRunRecord.model_validate(payload)

    first = _record(
        spec,
        serial=2,
        case_id="case-a",
        condition="self_created_skill",
        repeat_index=0,
        skill_revisions=(revision,),
    )
    changed = revision.model_copy(update={"source_hash": SHA_A})
    second = _record(
        spec,
        serial=3,
        case_id="case-a",
        condition="self_created_skill",
        repeat_index=1,
        skill_revisions=(changed,),
    )
    with pytest.raises(ValueError, match="Skill revisions changed across repeats"):
        analyze_campaign(spec, [first, second])


@pytest.mark.parametrize("outcome", ["timeout", "setup_failure", "infra_failure"])
def test_covered_skill_failures_still_require_exact_skill_revisions(outcome):
    spec = _spec(repeats=1)
    record = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="self_created_skill",
        repeat_index=0,
        score=None,
        outcome=outcome,
    )
    payload = record.model_dump(mode="json")
    payload["skill_revisions"] = []

    with pytest.raises(ValidationError, match="require exact Skill revisions"):
        BenchmarkRunRecord.model_validate(payload)


def test_creation_failure_is_an_uncovered_zero_not_a_revisionless_covered_run():
    spec = _spec(repeats=1, anchor_case_ids=("case-b",))
    record = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="self_created_skill",
        repeat_index=0,
        score=None,
        coverage_status="uncovered",
        outcome="not_run",
        reason_code="creation_failed",
    )

    summary = analyze_campaign(spec, [record])
    condition = summary["conditions"]["self_created_skill"]
    assert condition["strict_mean_score"] == 0.0
    assert condition["uncovered_cases"] == 1
    assert condition["reason_codes"]["creation_failed"] == 1


def test_memory_ablation_requires_the_same_skill_revision_and_coverage():
    spec = _spec(repeats=1, experiment_kind="memory_ablation")
    off = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="memory_off",
        repeat_index=0,
    )
    on = _record(
        spec,
        serial=2,
        case_id="case-a",
        condition="memory_on",
        repeat_index=0,
    )
    changed_revision = on.skill_revisions[0].model_copy(update={"source_hash": SHA_A})
    with pytest.raises(ValueError, match="memory conditions changed Skill revisions"):
        analyze_campaign(
            spec,
            [off, on.model_copy(update={"skill_revisions": (changed_revision,)})],
        )

    uncovered_spec = _spec(
        repeats=1,
        experiment_kind="memory_ablation",
        anchor_case_ids=(),
    )
    covered_off = _record(
        uncovered_spec,
        serial=3,
        case_id="case-a",
        condition="memory_off",
        repeat_index=0,
    )
    uncovered_on = _record(
        uncovered_spec,
        serial=4,
        case_id="case-a",
        condition="memory_on",
        repeat_index=0,
        score=None,
        coverage_status="uncovered",
        outcome="not_run",
    )
    with pytest.raises(ValueError, match="memory coverage changed between conditions"):
        analyze_campaign(uncovered_spec, [covered_off, uncovered_on])


def test_refinement_ablation_keeps_the_same_skill_ids():
    spec = _spec(repeats=1, experiment_kind="refinement_ablation")
    original = _record(
        spec,
        serial=1,
        case_id="case-a",
        condition="original_skill",
        repeat_index=0,
    )
    refined = _record(
        spec,
        serial=2,
        case_id="case-a",
        condition="refined_skill",
        repeat_index=0,
    )
    unrelated = refined.skill_revisions[0].model_copy(
        update={"skill_id": "unrelated-skill"}
    )
    with pytest.raises(ValueError, match="refinement conditions changed Skill identities"):
        analyze_campaign(
            spec,
            [original, refined.model_copy(update={"skill_revisions": (unrelated,)})],
        )


def test_run_contract_separates_coverage_from_grading():
    spec = _spec(repeats=1)
    with pytest.raises(ValidationError, match="require outcome=not_run"):
        _record(
            spec,
            serial=1,
            case_id="case-a",
            condition="no_skill",
            repeat_index=0,
            coverage_status="unsupported",
            outcome="graded",
            score=0.0,
        )
    with pytest.raises(ValidationError, match="graded runs require a score"):
        _record(
            spec,
            serial=2,
            case_id="case-a",
            condition="no_skill",
            repeat_index=0,
            score=None,
        )


def test_jsonl_loader_rejects_duplicate_keys_and_conflicting_ids(tmp_path: Path):
    spec = _spec(repeats=1)
    record = _record(spec, serial=1, case_id="case-a", condition="no_skill", repeat_index=0)
    path = tmp_path / "records.jsonl"
    path.write_text('{"schema_version": 1, "schema_version": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        load_campaign_records(path)

    first = record.model_dump(mode="json")
    second = {**first, "score": 0.5}
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting benchmark run_id"):
        load_campaign_records(path)


def test_markdown_report_labels_strict_and_covered_scores():
    spec = _spec(repeats=1)
    records = [
        _record(spec, serial=1, case_id="case-a", condition="no_skill", repeat_index=0),
        _record(spec, serial=2, case_id="case-b", condition="no_skill", repeat_index=0),
    ]
    report = render_campaign_markdown(analyze_campaign(spec, records))
    assert "Cases: 2/10" in report
    assert "Strict score" in report
    assert "Covered score" in report
    assert "missing, unsupported, uncovered" in report
    assert "matrix-integrity-only" in report
    assert "not-established-without-execution-harness-provenance" in report
