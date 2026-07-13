from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

import pytest

from omicsclaw.skill.capability_resolver import CapabilityCandidate, CapabilityDecision
import omicsclaw.skill.routing_oracle as routing_oracle_module
from omicsclaw.skill.routing_oracle import evaluate_routing_oracle, load_routing_oracle


ORACLE_PATH = Path(__file__).parent / "fixtures" / "routing_oracle" / "v1.json"
ROOT = Path(__file__).resolve().parent.parent
EXPECTED_DOMAINS = {
    "spatial",
    "singlecell",
    "genomics",
    "proteomics",
    "metabolomics",
    "bulkrna",
    "orchestrator",
    "literature",
}


def _oracle_payload() -> dict:
    return json.loads(ORACLE_PATH.read_text(encoding="utf-8"))


def _write_oracle(tmp_path: Path, payload: dict, name: str = "oracle.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_full_domain_routing_oracle_is_valid_and_meets_its_quality_thresholds():
    oracle = load_routing_oracle(ORACLE_PATH)
    assert oracle.validation_errors == []
    assert {case.domain for case in oracle.cases} == EXPECTED_DOMAINS

    report = evaluate_routing_oracle(oracle)

    assert report.passed, report.format_failures()
    assert report.metrics["hallucinated_alias_rate"] == 0.0
    assert report.metrics["precondition_accuracy"] == 1.0
    assert {
        result.observed_precondition_status
        for result in report.case_results
        if result.expected_precondition_status
    } == {"eligible", "needs_preparation", "blocked"}
    assert all(
        metrics["precision_at_1"] >= 0.9
        for metrics in report.per_domain.values()
    )
    assert len(report.case_results) == len(oracle.cases)


def test_routing_oracle_cli_is_a_ci_quality_gate():
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "evaluate_routing_oracle.py"),
            "--oracle",
            str(ORACLE_PATH),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["passed"] is True
    assert payload["metrics"]["precision_at_1"] >= 0.9


def test_pr_ci_executes_acquisition_and_routing_regression_suites():
    workflow = (ROOT / ".github" / "workflows" / "pr-ci.yml").read_text(
        encoding="utf-8"
    )
    required_tests = {
        "tests/test_skill_scaffolder.py",
        "tests/test_scaffolder_literal_lift.py",
        "tests/test_scaffolder_corpus_derived.py",
        "tests/test_analysis_router.py",
        "tests/test_capability_resolver.py",
        "tests/test_capability_resolver_golden.py",
        "tests/test_routing_oracle.py",
        "tests/test_routing_budget_gate.py",
        "tests/test_skill_preconditions.py",
    }

    missing = sorted(path for path in required_tests if path not in workflow)
    assert missing == [], f"PR CI does not execute regression suites: {missing}"
    assert workflow.count('"anndata==0.11.4"') == 2


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("duplicate_id", "duplicate case id"),
        ("duplicate_query", "duplicate normalized query"),
        ("unknown_domain", "unknown domain"),
        ("skill_domain_mismatch", "belongs to 'singlecell'"),
        ("multiple_coverages", "exactly one expected_coverage"),
        ("no_skill_with_skill", "no_skill decision cannot declare expected_skills"),
        ("missing_threshold", "missing metric thresholds"),
        ("out_of_range_threshold", "must be within [0, 1]"),
        ("underfilled_domain", "minimum is 4"),
    ],
)
def test_oracle_validation_rejects_malformed_or_gameable_corpora(
    tmp_path: Path,
    mutation: str,
    expected_error: str,
):
    payload = _oracle_payload()
    if mutation == "duplicate_id":
        payload["cases"][1]["id"] = payload["cases"][0]["id"]
    elif mutation == "duplicate_query":
        payload["cases"][1]["query"] = payload["cases"][0]["query"]
    elif mutation == "unknown_domain":
        payload["cases"][0]["domain"] = "unknown-domain"
    elif mutation == "skill_domain_mismatch":
        payload["cases"][0]["expected_skills"] = ["sc-preprocessing"]
    elif mutation == "multiple_coverages":
        payload["cases"][0]["expected_coverage"] = [
            "exact_skill",
            "partial_skill",
        ]
    elif mutation == "no_skill_with_skill":
        payload["cases"][-1]["expected_skills"] = ["literature"]
    elif mutation == "missing_threshold":
        del payload["thresholds"]["top3_recall"]
    elif mutation == "out_of_range_threshold":
        payload["thresholds"]["precision_at_1"] = 1.1
    elif mutation == "underfilled_domain":
        payload["minimum_cases_per_domain"] = 4
    else:  # pragma: no cover - keeps the table exhaustive during edits
        raise AssertionError(mutation)

    oracle = load_routing_oracle(_write_oracle(tmp_path, payload))

    assert any(expected_error in error for error in oracle.validation_errors), (
        mutation,
        oracle.validation_errors,
    )


def test_partial_decision_branch_requires_partial_coverage(monkeypatch):
    payload = _oracle_payload()
    case = payload["cases"][3]
    case["decision"] = "partial"
    case["expected_coverage"] = ["partial_skill"]
    oracle = load_routing_oracle(ORACLE_PATH)
    oracle.cases = [
        oracle.cases[3].__class__(
            id=case["id"],
            domain=case["domain"],
            query=case["query"],
            expected_skills=tuple(case["expected_skills"]),
            expected_coverage=("partial_skill",),
            decision="partial",
        )
    ]
    oracle.validation_errors = []
    oracle.thresholds = {name: 0.0 for name in oracle.thresholds}
    oracle.per_domain_thresholds = {
        name: 0.0 for name in oracle.per_domain_thresholds
    }
    expected_skill = case["expected_skills"][0]
    monkeypatch.setattr(
        routing_oracle_module,
        "resolve_capability",
        lambda *args, **kwargs: CapabilityDecision(
            query=case["query"],
            domain=case["domain"],
            coverage="partial_skill",
            chosen_skill=expected_skill,
            skill_candidates=[
                CapabilityCandidate(skill=expected_skill, domain=case["domain"], score=8.0)
            ],
        ),
    )

    report = evaluate_routing_oracle(oracle)

    assert report.case_results[0].decision_ok is True
    assert report.metrics["decision_accuracy"] == 1.0


def test_hallucinated_alias_is_counted_and_fails_zero_tolerance(monkeypatch):
    oracle = load_routing_oracle(ORACLE_PATH)
    oracle.cases = [oracle.cases[0]]
    oracle.validation_errors = []
    oracle.thresholds = {name: 0.0 for name in oracle.thresholds}
    oracle.per_domain_thresholds = {
        name: 0.0 for name in oracle.per_domain_thresholds
    }
    monkeypatch.setattr(
        routing_oracle_module,
        "resolve_capability",
        lambda *args, **kwargs: CapabilityDecision(
            query=oracle.cases[0].query,
            domain="spatial",
            coverage="exact_skill",
            chosen_skill="invented-spatial-skill",
            skill_candidates=[
                CapabilityCandidate(
                    skill="invented-spatial-skill", domain="spatial", score=99.0
                )
            ],
        ),
    )

    report = evaluate_routing_oracle(oracle)

    assert report.metrics["hallucinated_alias_rate"] == 1.0
    assert report.case_results[0].hallucinated_aliases == (
        "invented-spatial-skill",
    )
    assert any("hallucinated_alias_rate" in item for item in report.threshold_failures)


def test_precondition_mismatch_fails_the_oracle_quality_gate(tmp_path: Path):
    payload = _oracle_payload()
    raw_case = next(
        case
        for case in payload["cases"]
        if case["id"] == "singlecell__cluster_raw_precondition"
    )
    raw_case["expected_precondition_status"] = "eligible"
    raw_case["expected_execution_ready"] = True

    report = evaluate_routing_oracle(load_routing_oracle(_write_oracle(tmp_path, payload)))

    assert report.metrics["precondition_accuracy"] < 1.0
    assert any("precondition_accuracy" in item for item in report.threshold_failures)
    assert "observed(needs_preparation, ready=False" in report.format_failures()


def test_precondition_metric_requires_a_real_evaluation(monkeypatch):
    oracle = load_routing_oracle(ORACLE_PATH)
    case = next(
        item
        for item in oracle.cases
        if item.id == "singlecell__cluster_raw_precondition"
    )
    oracle.cases = [case]
    oracle.validation_errors = []
    oracle.thresholds = {name: 0.0 for name in oracle.thresholds}
    oracle.thresholds["precondition_accuracy"] = 1.0
    oracle.per_domain_thresholds = {
        name: 0.0 for name in oracle.per_domain_thresholds
    }
    expected_skill = case.expected_skills[0]
    monkeypatch.setattr(
        routing_oracle_module,
        "resolve_capability",
        lambda *args, **kwargs: CapabilityDecision(
            query=case.query,
            domain=case.domain,
            coverage=case.expected_coverage[0],
            chosen_skill=expected_skill,
            skill_candidates=[
                CapabilityCandidate(
                    skill=expected_skill,
                    domain=case.domain,
                    score=10.0,
                )
            ],
            precondition_status=case.expected_precondition_status,
            precondition_evaluated=False,
            execution_ready=bool(case.expected_execution_ready),
        ),
    )

    report = evaluate_routing_oracle(oracle)

    assert report.metrics["precondition_accuracy"] == 0.0
    assert report.case_results[0].precondition_ok is False
    assert "evaluated=False" in report.format_failures()


def test_schema_v1_keeps_the_new_precondition_threshold_optional(tmp_path: Path):
    payload = _oracle_payload()
    del payload["thresholds"]["precondition_accuracy"]

    oracle = load_routing_oracle(_write_oracle(tmp_path, payload))

    assert oracle.validation_errors == []


def test_precondition_threshold_requires_all_three_oracle_states(tmp_path: Path):
    payload = _oracle_payload()
    for case in payload["cases"]:
        case.pop("input_profile", None)
        case.pop("expected_precondition_status", None)
        case.pop("expected_execution_ready", None)

    oracle = load_routing_oracle(_write_oracle(tmp_path, payload))

    assert any(
        "precondition_accuracy requires cases covering" in error
        for error in oracle.validation_errors
    )


@pytest.mark.parametrize(
    ("kind", "expected_exit"),
    [("invalid", 2), ("quality_failure", 1)],
)
def test_routing_oracle_cli_distinguishes_invalid_input_from_quality_failure(
    tmp_path: Path,
    kind: str,
    expected_exit: int,
):
    payload = _oracle_payload()
    if kind == "invalid":
        payload["cases"][0]["expected_coverage"] = [
            "exact_skill",
            "partial_skill",
        ]
    else:
        payload["cases"][0]["query"] = "What is the weather tomorrow?"
    oracle_path = _write_oracle(tmp_path, payload, f"{kind}.json")

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "evaluate_routing_oracle.py"),
            "--oracle",
            str(oracle_path),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == expected_exit, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["passed"] is False
    if kind == "invalid":
        assert report["validation_errors"]
    else:
        assert report["threshold_failures"]
