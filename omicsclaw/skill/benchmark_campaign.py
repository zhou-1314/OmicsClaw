"""Strict suite-level benchmark analysis for Skill lifecycle experiments.

Per-Skill Evaluation Protocols answer whether one exact Skill revision earned
evidence from one declared case. A Benchmark Campaign answers a different
question: whether an agent/runtime condition improves over a fixed suite
denominator. Keeping these records separate prevents agent-level, missing-case,
or cost evidence from accidentally promoting one Skill.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BenchmarkCampaignSpec",
    "BenchmarkCaseSpec",
    "BenchmarkRunRecord",
    "BenchmarkSkillRevision",
    "analyze_campaign",
    "campaign_digest",
    "load_campaign_records",
    "load_campaign_spec",
    "render_campaign_markdown",
]


BenchmarkCondition = Literal[
    "no_skill",
    "curated_skill",
    "self_created_skill",
    "original_skill",
    "refined_skill",
    "memory_off",
    "memory_on",
    "transferred_skill",
]
BenchmarkExperiment = Literal[
    "main_skill_effect",
    "memory_ablation",
    "refinement_ablation",
    "transfer",
]
BenchmarkCoverage = Literal["covered", "uncovered", "unsupported"]
BenchmarkOutcome = Literal[
    "graded",
    "timeout",
    "setup_failure",
    "infra_failure",
    "cancelled",
    "not_run",
]
BenchmarkReasonCode = Literal[
    "none",
    "timeout",
    "setup_failure",
    "infra_failure",
    "cancelled",
    "uncovered",
    "unsupported",
    "missing_dependency",
    "bad_input",
    "resource_exhausted",
    "policy_blocked",
    "creation_failed",
    "execution_failed",
    "invalid_output",
    "grader_failed",
]
BenchmarkGrader = Literal["deterministic", "multimetric", "llm_judge"]
BenchmarkSubset = Literal["full", "preregistered_subset"]

_EXPERIMENT_CONTRACTS: dict[
    str, tuple[tuple[str, ...], str, str]
] = {
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
}

_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_OPAQUE_ID_PATTERN = r"^[0-9a-f]{32}$"
_ARTIFACT_REF_PATTERN = r"^evaluation-artifact:sha256:[0-9a-f]{64}$"
_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BenchmarkCaseSpec(_StrictModel):
    """One pre-registered case and the immutable evidence it must use."""

    case_id: str = Field(pattern=_TOKEN_PATTERN)
    domain: str = Field(default="", max_length=128)
    dataset_digest: str = Field(pattern=_SHA256_PATTERN)
    grader_digest: str = Field(pattern=_SHA256_PATTERN)


class BenchmarkCampaignSpec(_StrictModel):
    """Frozen experiment matrix for one suite-native scoring system."""

    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=_TOKEN_PATTERN)
    suite_id: str = Field(pattern=_TOKEN_PATTERN)
    suite_version: str = Field(min_length=1, max_length=128)
    suite_digest: str = Field(pattern=_SHA256_PATTERN)
    subset_kind: BenchmarkSubset
    experiment_kind: BenchmarkExperiment
    suite_case_count: int = Field(ge=1, le=100_000)
    selection_rationale: str = Field(default="", max_length=2_000)
    cases: tuple[BenchmarkCaseSpec, ...] = Field(min_length=1)
    conditions: tuple[BenchmarkCondition, ...] = Field(min_length=1)
    baseline_condition: BenchmarkCondition = "no_skill"
    coverage_anchor_condition: BenchmarkCondition
    coverage_anchor_case_ids: tuple[str, ...]
    coverage_manifest_digest: str = Field(pattern=_SHA256_PATTERN)
    repeats: int = Field(default=1, ge=1, le=100)
    grader_kind: BenchmarkGrader
    score_name: str = Field(min_length=1, max_length=128)
    pass_threshold: float = Field(ge=0.0, le=1.0)
    runtime_id: str = Field(min_length=1, max_length=256)
    model_id: str = Field(min_length=1, max_length=256)
    agent_revision: str = Field(min_length=1, max_length=256)
    environment_id: str = Field(min_length=1, max_length=256)
    tool_policy_id: str = Field(min_length=1, max_length=256)
    budget_id: str = Field(min_length=1, max_length=256)
    condition_config_digests: dict[BenchmarkCondition, str]
    low_variance_threshold: float = Field(default=0.1, ge=0.0, le=1.0)

    @field_validator("cases", "conditions", "coverage_anchor_case_ids", mode="before")
    @classmethod
    def _json_arrays_to_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_matrix(self):
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("campaign case IDs must be unique")
        if len(self.conditions) != len(set(self.conditions)):
            raise ValueError("campaign conditions must be unique")
        if self.baseline_condition not in self.conditions:
            raise ValueError("baseline_condition must be declared in conditions")
        if self.coverage_anchor_condition not in self.conditions:
            raise ValueError("coverage_anchor_condition must be declared in conditions")
        if len(self.coverage_anchor_case_ids) != len(set(self.coverage_anchor_case_ids)):
            raise ValueError("coverage anchor case IDs must be unique")
        if not set(self.coverage_anchor_case_ids).issubset(case_ids):
            raise ValueError("coverage anchor cases must be declared Campaign cases")
        expected_conditions, expected_baseline, expected_anchor = _EXPERIMENT_CONTRACTS[
            self.experiment_kind
        ]
        if self.conditions != expected_conditions:
            raise ValueError(
                f"{self.experiment_kind} requires conditions={expected_conditions}"
            )
        if self.baseline_condition != expected_baseline:
            raise ValueError(
                f"{self.experiment_kind} requires baseline_condition={expected_baseline}"
            )
        if self.coverage_anchor_condition != expected_anchor:
            raise ValueError(
                f"{self.experiment_kind} requires "
                f"coverage_anchor_condition={expected_anchor}"
            )
        if set(self.condition_config_digests) != set(self.conditions):
            raise ValueError(
                "condition_config_digests must bind every declared condition exactly"
            )
        if any(
            re.fullmatch(_SHA256_PATTERN, digest) is None
            for digest in self.condition_config_digests.values()
        ):
            raise ValueError("condition configuration digests must be sha256 identities")
        if len(self.cases) > self.suite_case_count:
            raise ValueError("selected cases exceed suite_case_count")
        if self.subset_kind == "full" and len(self.cases) != self.suite_case_count:
            raise ValueError("a full campaign must declare every suite case")
        if self.subset_kind == "preregistered_subset" and not self.selection_rationale.strip():
            raise ValueError("a preregistered subset requires selection_rationale")
        return self


class BenchmarkSkillRevision(_StrictModel):
    """Exact Skill identity used by one agent-level benchmark run."""

    skill_id: str = Field(pattern=_TOKEN_PATTERN)
    version: str = Field(min_length=1, max_length=128)
    manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    source_hash: str = Field(pattern=_SHA256_PATTERN)


class BenchmarkRunRecord(_StrictModel):
    """One observed cell in a Campaign's case x condition x repeat matrix."""

    schema_version: Literal[1] = 1
    campaign_digest: str = Field(pattern=_SHA256_PATTERN)
    run_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    case_id: str = Field(pattern=_TOKEN_PATTERN)
    condition: BenchmarkCondition
    repeat_index: int = Field(ge=0, le=99)
    coverage_status: BenchmarkCoverage
    outcome: BenchmarkOutcome
    reason_code: BenchmarkReasonCode
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    dataset_digest: str = Field(pattern=_SHA256_PATTERN)
    grader_digest: str = Field(pattern=_SHA256_PATTERN)
    agent_revision: str = Field(min_length=1, max_length=256)
    environment_id: str = Field(min_length=1, max_length=256)
    runtime_id: str = Field(min_length=1, max_length=256)
    model_id: str = Field(min_length=1, max_length=256)
    tool_policy_id: str = Field(min_length=1, max_length=256)
    budget_id: str = Field(min_length=1, max_length=256)
    condition_config_digest: str = Field(pattern=_SHA256_PATTERN)
    trial_id: str | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    isolation_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    artifact_bundle_ref: str | None = Field(
        default=None,
        pattern=_ARTIFACT_REF_PATTERN,
    )
    grader_evidence_digest: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    skill_revisions: tuple[BenchmarkSkillRevision, ...] = ()
    native_metrics: dict[str, float] = Field(default_factory=dict)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    turns: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("skill_revisions", "evidence_refs", mode="before")
    @classmethod
    def _json_arrays_to_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("skill_revisions")
    @classmethod
    def _unique_skill_revisions(
        cls, value: tuple[BenchmarkSkillRevision, ...]
    ) -> tuple[BenchmarkSkillRevision, ...]:
        skill_ids = [revision.skill_id for revision in value]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skill_revisions must contain each Skill at most once")
        return tuple(sorted(value, key=lambda revision: revision.skill_id))

    @field_validator("native_metrics")
    @classmethod
    def _finite_native_metrics(cls, value: dict[str, float]) -> dict[str, float]:
        if len(value) > 64:
            raise ValueError("native_metrics exceeds 64 entries")
        for name, metric in value.items():
            if not name or len(name) > 128 or not math.isfinite(metric):
                raise ValueError("native_metrics must have bounded names and finite values")
        return value

    @field_validator("duration_seconds", "cost_usd")
    @classmethod
    def _finite_costs(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("cost and duration values must be finite")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _bounded_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 512 for item in value):
            raise ValueError("evidence_refs must be non-empty bounded strings")
        return value

    @model_validator(mode="after")
    def _validate_outcome(self):
        if self.condition == "no_skill" and self.skill_revisions:
            raise ValueError("no_skill records cannot bind Skills")
        if (
            self.condition != "no_skill"
            and self.coverage_status == "covered"
            and not self.skill_revisions
        ):
            raise ValueError("covered Skill conditions require exact Skill revisions")
        if self.coverage_status == "covered":
            if self.outcome == "not_run":
                raise ValueError("covered runs cannot use outcome=not_run")
            if not self.trial_id or not self.isolation_id or not self.artifact_bundle_ref:
                raise ValueError(
                    "covered Campaign runs require content-addressed evidence, "
                    "trial identity, and isolation identity"
                )
            if self.artifact_bundle_ref not in self.evidence_refs:
                raise ValueError(
                    "covered Campaign artifact bundle must appear in evidence_refs"
                )
        elif self.outcome != "not_run":
            raise ValueError("uncovered or unsupported runs require outcome=not_run")

        if self.outcome == "graded":
            if self.score is None:
                raise ValueError("graded runs require a score")
            if self.reason_code != "none":
                raise ValueError("graded runs require reason_code=none")
            if not self.grader_evidence_digest:
                raise ValueError(
                    "graded Campaign runs require content-addressed evidence"
                )
            if self.grader_evidence_digest not in self.evidence_refs:
                raise ValueError(
                    "grader evidence digest must appear in evidence_refs"
                )
        elif self.score not in {None, 0.0}:
            raise ValueError("non-graded runs cannot publish a non-zero score")
        elif self.reason_code == "none":
            raise ValueError("non-graded runs require a failure reason")
        elif self.grader_evidence_digest is not None:
            raise ValueError("non-graded runs cannot claim grader evidence")
        if self.outcome == "not_run":
            if any(
                value is not None
                for value in (
                    self.trial_id,
                    self.isolation_id,
                    self.artifact_bundle_ref,
                    self.duration_seconds,
                    self.input_tokens,
                    self.output_tokens,
                    self.turns,
                    self.cost_usd,
                )
            ):
                raise ValueError("not_run records cannot claim execution measurements")
            allowed_not_run_reasons = {self.coverage_status}
            if (
                self.coverage_status == "uncovered"
                and self.condition == "self_created_skill"
            ):
                allowed_not_run_reasons.add("creation_failed")
            if self.reason_code not in allowed_not_run_reasons:
                raise ValueError(
                    "not_run reason must match coverage or a condition-specific "
                    "generation failure"
                )
        return self


def campaign_digest(spec: BenchmarkCampaignSpec) -> str:
    """Content identity for the complete pre-registered Campaign contract."""
    payload = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _strict_json(raw: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError(f"duplicate JSON key: {key}")
            decoded[key] = value
        return decoded

    return json.loads(
        raw,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )


def load_campaign_spec(path: str | Path) -> BenchmarkCampaignSpec:
    """Load a strict Campaign JSON document."""
    return BenchmarkCampaignSpec.model_validate(_strict_json(Path(path).read_text()))


def load_campaign_records(path: str | Path) -> list[BenchmarkRunRecord]:
    """Load strict JSONL records, collapsing only byte-equivalent run IDs."""
    records: list[BenchmarkRunRecord] = []
    by_run_id: dict[str, BenchmarkRunRecord] = {}
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = BenchmarkRunRecord.model_validate(_strict_json(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid benchmark record at line {lineno}") from exc
        previous = by_run_id.get(record.run_id)
        if previous is not None:
            if previous != record:
                raise ValueError(f"conflicting benchmark run_id at line {lineno}")
            continue
        by_run_id[record.run_id] = record
        records.append(record)
    return records


def _effective_score(record: BenchmarkRunRecord | None) -> float:
    if record is None or record.outcome != "graded" or record.score is None:
        return 0.0
    return record.score


def _is_pass(record: BenchmarkRunRecord | None, threshold: float) -> bool:
    return bool(
        record is not None
        and record.outcome == "graded"
        and record.score is not None
        and record.score >= threshold
    )


def _mean_or_none(values: Sequence[float | int | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    return statistics.fmean(numbers) if numbers else None


def _median_or_none(values: Sequence[float | int | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    return float(statistics.median(numbers)) if numbers else None


def analyze_campaign(
    spec: BenchmarkCampaignSpec,
    records: Sequence[BenchmarkRunRecord],
) -> dict[str, Any]:
    """Analyze one suite without dropping missing, failed, or uncovered cells.

    Every declared case x condition x repeat contributes to the strict
    denominator. Missing records, setup/infra failures, timeouts, unsupported
    cases, and uncovered self-created Skills therefore contribute a score of
    zero. Covered-only metrics are emitted separately and are diagnostic.
    """
    digest = campaign_digest(spec)
    cases = {case.case_id: case for case in spec.cases}
    matrix: dict[tuple[str, str, int], BenchmarkRunRecord] = {}
    seen_run_ids: dict[str, BenchmarkRunRecord] = {}
    seen_trial_ids: dict[str, str] = {}
    seen_isolation_ids: dict[str, str] = {}
    revisions_by_case_condition: dict[
        tuple[str, str], tuple[BenchmarkSkillRevision, ...]
    ] = {}

    for record in records:
        if record.campaign_digest != digest:
            raise ValueError("record campaign_digest does not match the Campaign")
        case = cases.get(record.case_id)
        if case is None:
            raise ValueError(f"record references undeclared case: {record.case_id}")
        if record.condition not in spec.conditions:
            raise ValueError(f"record references undeclared condition: {record.condition}")
        if record.repeat_index >= spec.repeats:
            raise ValueError("record repeat_index is outside the Campaign matrix")
        if record.dataset_digest != case.dataset_digest:
            raise ValueError(f"record dataset digest mismatch for {record.case_id}")
        if record.grader_digest != case.grader_digest:
            raise ValueError(f"record grader digest mismatch for {record.case_id}")
        if record.agent_revision != spec.agent_revision:
            raise ValueError("record agent revision mismatch")
        if record.environment_id != spec.environment_id:
            raise ValueError("record environment identity mismatch")
        if record.runtime_id != spec.runtime_id:
            raise ValueError("record runtime identity mismatch")
        if record.model_id != spec.model_id:
            raise ValueError("record model identity mismatch")
        if record.tool_policy_id != spec.tool_policy_id:
            raise ValueError("record tool policy identity mismatch")
        if record.budget_id != spec.budget_id:
            raise ValueError("record budget identity mismatch")
        if record.condition_config_digest != spec.condition_config_digests[record.condition]:
            raise ValueError("record condition configuration identity mismatch")
        if record.condition == spec.coverage_anchor_condition:
            expected_covered = record.case_id in spec.coverage_anchor_case_ids
            if (record.coverage_status == "covered") != expected_covered:
                raise ValueError(
                    f"record coverage disagrees with the coverage anchor manifest "
                    f"for {record.case_id}"
                )

        revision_key = (record.case_id, record.condition)
        previous_revisions = revisions_by_case_condition.get(revision_key)
        if previous_revisions is not None and previous_revisions != record.skill_revisions:
            raise ValueError(
                f"Skill revisions changed across repeats for "
                f"{record.case_id}/{record.condition}"
            )
        revisions_by_case_condition[revision_key] = record.skill_revisions

        previous = seen_run_ids.get(record.run_id)
        if previous is not None:
            if previous != record:
                raise ValueError(f"conflicting benchmark run_id: {record.run_id}")
            continue
        seen_run_ids[record.run_id] = record
        if record.coverage_status == "covered":
            assert record.trial_id is not None
            assert record.isolation_id is not None
            previous_trial = seen_trial_ids.get(record.trial_id)
            if previous_trial is not None:
                raise ValueError(
                    f"Campaign trial identity was reused by {previous_trial} and "
                    f"{record.run_id}"
                )
            previous_isolation = seen_isolation_ids.get(record.isolation_id)
            if previous_isolation is not None:
                raise ValueError(
                    f"Campaign isolation identity was reused by {previous_isolation} "
                    f"and {record.run_id}"
                )
            seen_trial_ids[record.trial_id] = record.run_id
            seen_isolation_ids[record.isolation_id] = record.run_id

        key = (record.case_id, record.condition, record.repeat_index)
        if key in matrix and matrix[key] != record:
            raise ValueError(f"duplicate Campaign matrix cell: {key}")
        matrix[key] = record

    condition_summaries: dict[str, dict[str, Any]] = {}
    selected_count = len(spec.cases)
    planned_per_condition = selected_count * spec.repeats

    def coverage_by_case(condition: str) -> dict[str, str]:
        case_status: dict[str, str] = {}
        for case in spec.cases:
            statuses = {
                matrix[(case.case_id, condition, repeat_index)].coverage_status
                for repeat_index in range(spec.repeats)
                if (case.case_id, condition, repeat_index) in matrix
            }
            if len(statuses) > 1:
                raise ValueError(
                    f"coverage_status changed across repeats for {case.case_id}/{condition}"
                )
            case_status[case.case_id] = next(iter(statuses), "missing")
        return case_status

    anchor_covered_case_ids = set(spec.coverage_anchor_case_ids)
    anchor_planned_runs = len(anchor_covered_case_ids) * spec.repeats

    if spec.experiment_kind == "memory_ablation":
        off_status = coverage_by_case("memory_off")
        on_status = coverage_by_case("memory_on")
        for case_id in cases:
            if (
                off_status[case_id] != "missing"
                and on_status[case_id] != "missing"
                and off_status[case_id] != on_status[case_id]
            ):
                raise ValueError(
                    f"memory coverage changed between conditions for {case_id}"
                )
            off_revisions = revisions_by_case_condition.get(
                (case_id, "memory_off"), ()
            )
            on_revisions = revisions_by_case_condition.get((case_id, "memory_on"), ())
            if off_revisions and on_revisions and off_revisions != on_revisions:
                raise ValueError(
                    f"memory conditions changed Skill revisions for {case_id}"
                )

    if spec.experiment_kind == "refinement_ablation":
        for case_id in cases:
            original = revisions_by_case_condition.get((case_id, "original_skill"), ())
            refined = revisions_by_case_condition.get((case_id, "refined_skill"), ())
            if original and refined and tuple(r.skill_id for r in original) != tuple(
                r.skill_id for r in refined
            ):
                raise ValueError(
                    f"refinement conditions changed Skill identities for {case_id}"
                )

    for condition in spec.conditions:
        expected_keys = [
            (case.case_id, condition, repeat_index)
            for case in spec.cases
            for repeat_index in range(spec.repeats)
        ]
        observed = [matrix[key] for key in expected_keys if key in matrix]
        scores = [_effective_score(matrix.get(key)) for key in expected_keys]
        missing_runs = planned_per_condition - len(observed)
        passes = sum(_is_pass(matrix.get(key), spec.pass_threshold) for key in expected_keys)

        case_status = coverage_by_case(condition)

        covered_case_ids = {
            case_id for case_id, status in case_status.items() if status == "covered"
        }
        covered_planned_runs = len(covered_case_ids) * spec.repeats
        covered_score_sum = sum(
            _effective_score(matrix.get(key))
            for key in expected_keys
            if key[0] in covered_case_ids
        )
        anchor_covered_score_sum = sum(
            _effective_score(matrix.get(key))
            for key in expected_keys
            if key[0] in anchor_covered_case_ids
        )

        outcome_counts = Counter(record.outcome for record in observed)
        reason_counts = Counter(record.reason_code for record in observed)
        if missing_runs:
            outcome_counts["missing"] = missing_runs
            reason_counts["missing_record"] = missing_runs

        stability_rows: list[dict[str, float | str]] = []
        if spec.repeats > 1:
            for case_id in sorted(covered_case_ids):
                case_records = [
                    matrix.get((case_id, condition, repeat_index))
                    for repeat_index in range(spec.repeats)
                ]
                if any(record is None for record in case_records):
                    continue
                case_scores = [_effective_score(record) for record in case_records]
                mean_score = statistics.fmean(case_scores)
                stddev = statistics.pstdev(case_scores)
                mad = statistics.fmean(abs(score - mean_score) for score in case_scores)
                stability_rows.append(
                    {
                        "case_id": case_id,
                        "mean": mean_score,
                        "stddev": stddev,
                        "mad": mad,
                    }
                )

        condition_summaries[condition] = {
            "planned_runs": planned_per_condition,
            "observed_runs": len(observed),
            "missing_runs": missing_runs,
            "complete": missing_runs == 0,
            "covered_cases": len(covered_case_ids),
            "uncovered_cases": sum(status == "uncovered" for status in case_status.values()),
            "unsupported_cases": sum(status == "unsupported" for status in case_status.values()),
            "missing_cases": sum(status == "missing" for status in case_status.values()),
            "coverage_rate": len(covered_case_ids) / selected_count,
            "strict_mean_score": statistics.fmean(scores),
            "strict_pass_rate": passes / planned_per_condition,
            "strict_passes": passes,
            "covered_mean_score": (
                covered_score_sum / covered_planned_runs
                if covered_planned_runs
                else None
            ),
            "anchor_covered_mean_score": (
                anchor_covered_score_sum / anchor_planned_runs
                if anchor_planned_runs
                else None
            ),
            "outcomes": dict(sorted(outcome_counts.items())),
            "reason_codes": dict(sorted(reason_counts.items())),
            "median_duration_seconds": _median_or_none(
                [record.duration_seconds for record in observed if record.outcome != "not_run"]
            ),
            "mean_input_tokens": _mean_or_none([record.input_tokens for record in observed]),
            "mean_output_tokens": _mean_or_none([record.output_tokens for record in observed]),
            "mean_turns": _mean_or_none([record.turns for record in observed]),
            "mean_cost_usd": _mean_or_none([record.cost_usd for record in observed]),
            "measurement_counts": {
                "duration_seconds": sum(
                    record.duration_seconds is not None for record in observed
                ),
                "input_tokens": sum(record.input_tokens is not None for record in observed),
                "output_tokens": sum(
                    record.output_tokens is not None for record in observed
                ),
                "turns": sum(record.turns is not None for record in observed),
                "cost_usd": sum(record.cost_usd is not None for record in observed),
            },
            "stability": {
                "complete_covered_cases": len(stability_rows),
                "avg_stddev": _mean_or_none([row["stddev"] for row in stability_rows]),
                "avg_mad": _mean_or_none([row["mad"] for row in stability_rows]),
                "nonconstant_cases": sum(row["stddev"] > 0 for row in stability_rows),
                "low_variance_cases": sum(
                    row["stddev"] <= spec.low_variance_threshold
                    for row in stability_rows
                ),
            },
        }

    baseline = condition_summaries[spec.baseline_condition]
    comparisons = {
        condition: {
            "strict_score_lift": summary["strict_mean_score"]
            - baseline["strict_mean_score"],
            "strict_pass_rate_lift": summary["strict_pass_rate"]
            - baseline["strict_pass_rate"],
        }
        for condition, summary in condition_summaries.items()
        if condition != spec.baseline_condition
    }
    return {
        "schema_version": 1,
        "campaign_id": spec.campaign_id,
        "campaign_digest": digest,
        "suite_id": spec.suite_id,
        "suite_version": spec.suite_version,
        "suite_digest": spec.suite_digest,
        "subset_kind": spec.subset_kind,
        "experiment_kind": spec.experiment_kind,
        "selected_cases": selected_count,
        "suite_case_count": spec.suite_case_count,
        "selection_coverage": selected_count / spec.suite_case_count,
        "repeats": spec.repeats,
        "grader_kind": spec.grader_kind,
        "score_name": spec.score_name,
        "pass_threshold": spec.pass_threshold,
        "runtime_id": spec.runtime_id,
        "model_id": spec.model_id,
        "agent_revision": spec.agent_revision,
        "environment_id": spec.environment_id,
        "tool_policy_id": spec.tool_policy_id,
        "budget_id": spec.budget_id,
        "condition_config_digests": dict(spec.condition_config_digests),
        "baseline_condition": spec.baseline_condition,
        "coverage_anchor_condition": spec.coverage_anchor_condition,
        "coverage_anchor_case_ids": list(spec.coverage_anchor_case_ids),
        "coverage_manifest_digest": spec.coverage_manifest_digest,
        "anchor_covered_cases": len(anchor_covered_case_ids),
        "analysis_assurance": "matrix-integrity-only",
        "causal_claim_status": "not-established-without-execution-harness-provenance",
        "conditions": condition_summaries,
        "comparisons": comparisons,
    }


def render_campaign_markdown(summary: Mapping[str, Any]) -> str:
    """Render a compact suite-native report without pooling unlike scores."""
    lines = [
        f"# Benchmark Campaign: {summary['campaign_id']}",
        "",
        f"- Suite: `{summary['suite_id']}` (`{summary['subset_kind']}`)",
        f"- Experiment: `{summary['experiment_kind']}`",
        f"- Cases: {summary['selected_cases']}/{summary['suite_case_count']}",
        f"- Repeats: {summary['repeats']}",
        f"- Campaign digest: `{summary['campaign_digest']}`",
        f"- Assurance: `{summary['analysis_assurance']}`",
        f"- Causal claim: `{summary['causal_claim_status']}`",
        "",
        "| Condition | Coverage | Strict score | Pass rate | Anchor-covered score | Missing |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    conditions = summary["conditions"]
    for name, condition in conditions.items():
        covered = condition["anchor_covered_mean_score"]
        covered_text = "n/a" if covered is None else f"{covered:.4f}"
        lines.append(
            f"| `{name}` | {condition['coverage_rate']:.1%} | "
            f"{condition['strict_mean_score']:.4f} | "
            f"{condition['strict_pass_rate']:.1%} | {covered_text} | "
            f"{condition['missing_runs']} |"
        )
    lines.extend(
        (
            "",
            "Strict scores retain missing, unsupported, uncovered, timeout, setup, and "
            "infrastructure cells as zero. Covered score is diagnostic only.",
            "This offline analysis verifies matrix identities and denominators only. "
            "A causal Agent-lift claim additionally requires execution-harness "
            "provenance for the selected experiment kind.",
            "",
        )
    )
    return "\n".join(lines)
