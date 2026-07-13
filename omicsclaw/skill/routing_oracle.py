"""Versioned, deterministic quality oracle for full-domain skill routing.

Unlike the historical golden snapshot (which intentionally preserves current
behaviour, including known misroutes), an oracle records *expected* behaviour
and enforces explicit quality thresholds.  It is pure local evaluation: no LLM,
network, or generated labels are involved.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

from .capability_resolver import resolve_capability
from .registry import ensure_registry_loaded


_REQUIRED_METRIC_NAMES = frozenset(
    {
        "precision_at_1",
        "top3_recall",
        "domain_accuracy",
        "decision_accuracy",
        "hallucinated_alias_rate",
    }
)
_METRIC_NAMES = _REQUIRED_METRIC_NAMES | {"precondition_accuracy"}
_COVERAGES = frozenset({"exact_skill", "partial_skill", "no_skill"})
_DECISIONS = frozenset({"route", "partial", "no_skill"})
_PER_DOMAIN_METRIC_NAMES = _METRIC_NAMES - {
    "hallucinated_alias_rate",
    "precondition_accuracy",
}
_PRECONDITION_STATUSES = frozenset({"eligible", "needs_preparation", "blocked"})


@dataclass(frozen=True, slots=True)
class RoutingOracleCase:
    id: str
    domain: str
    query: str
    expected_skills: tuple[str, ...]
    expected_coverage: tuple[str, ...]
    decision: str
    file_path: str = ""
    domain_hint: str = ""
    input_profile: dict[str, Any] | None = None
    expected_precondition_status: str = ""
    expected_execution_ready: bool | None = None


@dataclass(slots=True)
class RoutingOracle:
    schema_version: int
    oracle_version: str
    description: str
    minimum_cases_per_domain: int
    thresholds: dict[str, float]
    per_domain_thresholds: dict[str, float]
    cases: list[RoutingOracleCase]
    source_path: str = ""
    validation_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RoutingOracleCaseResult:
    id: str
    expected_domain: str
    observed_domain: str
    expected_skills: tuple[str, ...]
    observed_skill: str
    observed_top3: tuple[str, ...]
    expected_coverage: tuple[str, ...]
    observed_coverage: str
    top1_ok: bool
    top3_ok: bool
    domain_ok: bool
    decision_ok: bool
    expected_precondition_status: str = ""
    observed_precondition_status: str = ""
    expected_execution_ready: bool | None = None
    observed_precondition_evaluated: bool = False
    observed_execution_ready: bool = True
    precondition_ok: bool = True
    hallucinated_aliases: tuple[str, ...] = ()
    error: str = ""

    @property
    def passed(self) -> bool:
        return (
            not self.error
            and self.top1_ok
            and self.top3_ok
            and self.domain_ok
            and self.decision_ok
            and self.precondition_ok
            and not self.hallucinated_aliases
        )


@dataclass(slots=True)
class RoutingOracleReport:
    oracle_version: str
    metrics: dict[str, float]
    thresholds: dict[str, float]
    per_domain_thresholds: dict[str, float]
    case_results: list[RoutingOracleCaseResult]
    per_domain: dict[str, dict[str, float]]
    validation_errors: list[str] = field(default_factory=list)
    threshold_failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.validation_errors and not self.threshold_failures

    def format_failures(self) -> str:
        lines = list(self.validation_errors) + list(self.threshold_failures)
        for result in self.case_results:
            if result.passed:
                continue
            details = (
                f"{result.id}: expected skills={list(result.expected_skills)} "
                f"domain={result.expected_domain} coverage={list(result.expected_coverage)}; "
                f"observed skill={result.observed_skill or '<none>'} "
                f"domain={result.observed_domain or '<none>'} "
                f"coverage={result.observed_coverage or '<none>'} "
                f"top3={list(result.observed_top3)}"
            )
            if result.error:
                details += f" error={result.error}"
            if result.hallucinated_aliases:
                details += f" hallucinated={list(result.hallucinated_aliases)}"
            if not result.precondition_ok:
                details += (
                    " precondition="
                    f"expected({result.expected_precondition_status}, "
                    f"ready={result.expected_execution_ready}) "
                    f"observed({result.observed_precondition_status}, "
                    f"ready={result.observed_execution_ready}, "
                    f"evaluated={result.observed_precondition_evaluated})"
                )
            lines.append(details)
        return "\n".join(lines) or "routing oracle passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "oracle_version": self.oracle_version,
            "passed": self.passed,
            "metrics": dict(self.metrics),
            "thresholds": dict(self.thresholds),
            "per_domain_thresholds": dict(self.per_domain_thresholds),
            "per_domain": self.per_domain,
            "validation_errors": list(self.validation_errors),
            "threshold_failures": list(self.threshold_failures),
            "cases": [asdict(result) | {"passed": result.passed} for result in self.case_results],
        }


def _as_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _validate_oracle(oracle: RoutingOracle) -> list[str]:
    errors: list[str] = []
    if oracle.schema_version != 1:
        errors.append(f"unsupported routing oracle schema_version={oracle.schema_version}")
    if not oracle.oracle_version:
        errors.append("oracle_version is required")
    if oracle.minimum_cases_per_domain < 1:
        errors.append("minimum_cases_per_domain must be positive")

    missing_metrics = _REQUIRED_METRIC_NAMES - set(oracle.thresholds)
    extra_metrics = set(oracle.thresholds) - _METRIC_NAMES
    if missing_metrics:
        errors.append(f"missing metric thresholds: {sorted(missing_metrics)}")
    if extra_metrics:
        errors.append(f"unknown metric thresholds: {sorted(extra_metrics)}")
    for name, threshold in oracle.thresholds.items():
        if not 0.0 <= threshold <= 1.0:
            errors.append(f"threshold {name} must be within [0, 1]")
    unknown_domain_metrics = set(oracle.per_domain_thresholds) - _PER_DOMAIN_METRIC_NAMES
    if unknown_domain_metrics:
        errors.append(
            f"unknown per-domain metric thresholds: {sorted(unknown_domain_metrics)}"
        )
    if not oracle.per_domain_thresholds:
        errors.append("per_domain_thresholds must define at least one quality floor")
    for name, threshold in oracle.per_domain_thresholds.items():
        if not 0.0 <= threshold <= 1.0:
            errors.append(f"per-domain threshold {name} must be within [0, 1]")

    registry = ensure_registry_loaded()
    canonical = {
        alias: str(info.get("domain") or "")
        for alias, info in registry.iter_primary_skills()
    }
    known_domains = set(registry.domains)
    ids: set[str] = set()
    normalized_queries: dict[str, str] = {}
    domain_counts: Counter[str] = Counter()
    precondition_statuses: set[str] = set()
    for index, case in enumerate(oracle.cases, start=1):
        prefix = case.id or f"case#{index}"
        if not case.id:
            errors.append(f"case#{index}: id is required")
        elif case.id in ids:
            errors.append(f"duplicate case id: {case.id}")
        ids.add(case.id)
        if not case.query:
            errors.append(f"{prefix}: query is required")
        else:
            normalized_query = " ".join(case.query.casefold().split())
            prior_id = normalized_queries.get(normalized_query)
            if prior_id is not None:
                errors.append(
                    f"{prefix}: duplicate normalized query already used by {prior_id}"
                )
            else:
                normalized_queries[normalized_query] = prefix
        if case.domain not in known_domains:
            errors.append(f"{prefix}: unknown domain {case.domain!r}")
        else:
            domain_counts[case.domain] += 1
        if case.decision not in _DECISIONS:
            errors.append(f"{prefix}: unsupported decision {case.decision!r}")
        if case.expected_precondition_status:
            precondition_statuses.add(case.expected_precondition_status)
            if case.expected_precondition_status not in _PRECONDITION_STATUSES:
                errors.append(
                    f"{prefix}: invalid expected_precondition_status "
                    f"{case.expected_precondition_status!r}"
                )
            if case.expected_execution_ready is None:
                errors.append(
                    f"{prefix}: expected_execution_ready is required with a precondition expectation"
                )
            if case.input_profile is None:
                errors.append(
                    f"{prefix}: input_profile is required with a precondition expectation"
                )
        elif case.expected_execution_ready is not None:
            errors.append(
                f"{prefix}: expected_precondition_status is required with expected_execution_ready"
            )
        if not case.expected_coverage or not set(case.expected_coverage) <= _COVERAGES:
            errors.append(f"{prefix}: invalid expected_coverage {list(case.expected_coverage)}")
        elif len(case.expected_coverage) != 1:
            errors.append(
                f"{prefix}: declare exactly one expected_coverage, got "
                f"{list(case.expected_coverage)}"
            )
        else:
            expected_for_decision = {
                "route": "exact_skill",
                "partial": "partial_skill",
                "no_skill": "no_skill",
            }.get(case.decision)
            if expected_for_decision and case.expected_coverage[0] != expected_for_decision:
                errors.append(
                    f"{prefix}: decision {case.decision!r} requires "
                    f"expected_coverage [{expected_for_decision!r}]"
                )
        if case.decision == "no_skill":
            if case.expected_skills:
                errors.append(f"{prefix}: no_skill decision cannot declare expected_skills")
            if "no_skill" not in case.expected_coverage:
                errors.append(f"{prefix}: no_skill decision must accept no_skill coverage")
        elif not case.expected_skills:
            errors.append(f"{prefix}: routed decisions require expected_skills")
        for skill in case.expected_skills:
            if skill not in canonical:
                errors.append(f"{prefix}: expected skill {skill!r} is not a canonical alias")
            elif canonical[skill] != case.domain:
                errors.append(
                    f"{prefix}: expected skill {skill!r} belongs to {canonical[skill]!r}, "
                    f"not {case.domain!r}"
                )

    for domain in sorted(known_domains):
        count = domain_counts[domain]
        if count < oracle.minimum_cases_per_domain:
            errors.append(
                f"domain {domain!r} has {count} cases; "
                f"minimum is {oracle.minimum_cases_per_domain}"
            )
    if "precondition_accuracy" in oracle.thresholds:
        missing_statuses = _PRECONDITION_STATUSES - precondition_statuses
        if missing_statuses:
            errors.append(
                "precondition_accuracy requires cases covering all statuses; "
                f"missing {sorted(missing_statuses)}"
            )
    return errors


def load_routing_oracle(path: str | Path) -> RoutingOracle:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("routing oracle root must be an object")
    raw_cases = payload.get("cases") or []
    if not isinstance(raw_cases, list):
        raise ValueError("routing oracle cases must be a list")
    cases = [
        RoutingOracleCase(
            id=str(raw.get("id") or "").strip(),
            domain=str(raw.get("domain") or "").strip(),
            query=str(raw.get("query") or "").strip(),
            expected_skills=_as_string_tuple(raw.get("expected_skills")),
            expected_coverage=_as_string_tuple(raw.get("expected_coverage")),
            decision=str(raw.get("decision") or "").strip(),
            file_path=str(raw.get("file_path") or "").strip(),
            domain_hint=str(raw.get("domain_hint") or "").strip(),
            input_profile=(
                dict(raw["input_profile"])
                if isinstance(raw.get("input_profile"), dict)
                else None
            ),
            expected_precondition_status=str(
                raw.get("expected_precondition_status") or ""
            ).strip(),
            expected_execution_ready=(
                bool(raw["expected_execution_ready"])
                if "expected_execution_ready" in raw
                else None
            ),
        )
        for raw in raw_cases
        if isinstance(raw, dict)
    ]
    raw_thresholds = payload.get("thresholds") or {}
    thresholds = (
        {str(key): float(value) for key, value in raw_thresholds.items()}
        if isinstance(raw_thresholds, dict)
        else {}
    )
    raw_per_domain_thresholds = payload.get("per_domain_thresholds") or {}
    per_domain_thresholds = (
        {str(key): float(value) for key, value in raw_per_domain_thresholds.items()}
        if isinstance(raw_per_domain_thresholds, dict)
        else {}
    )
    oracle = RoutingOracle(
        schema_version=int(payload.get("schema_version") or 0),
        oracle_version=str(payload.get("oracle_version") or "").strip(),
        description=str(payload.get("description") or "").strip(),
        minimum_cases_per_domain=int(payload.get("minimum_cases_per_domain") or 0),
        thresholds=thresholds,
        per_domain_thresholds=per_domain_thresholds,
        cases=cases,
        source_path=str(source),
    )
    oracle.validation_errors = _validate_oracle(oracle)
    return oracle


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def evaluate_routing_oracle(oracle: RoutingOracle) -> RoutingOracleReport:
    registry = ensure_registry_loaded()
    canonical_aliases = {alias for alias, _info in registry.iter_primary_skills()}
    results: list[RoutingOracleCaseResult] = []
    alias_predictions = 0
    alias_hallucinations = 0

    for case in oracle.cases:
        try:
            decision = resolve_capability(
                case.query,
                file_path=case.file_path,
                domain_hint=case.domain_hint,
                input_profile=case.input_profile,
            )
            observed_top3 = tuple(
                candidate.skill for candidate in decision.skill_candidates[:3]
            )
            predicted_aliases = tuple(
                dict.fromkeys(
                    ([decision.chosen_skill] if decision.chosen_skill else [])
                    + list(observed_top3)
                )
            )
            hallucinated = tuple(
                alias for alias in predicted_aliases if alias not in canonical_aliases
            )
            alias_predictions += len(predicted_aliases)
            alias_hallucinations += len(hallucinated)
            route_case = bool(case.expected_skills)
            top1_ok = (
                decision.chosen_skill in case.expected_skills
                if route_case
                else not decision.chosen_skill
            )
            top3_ok = (
                bool(set(observed_top3) & set(case.expected_skills))
                if route_case
                else True
            )
            coverage_ok = decision.coverage in case.expected_coverage
            if case.decision == "no_skill":
                decision_ok = not decision.chosen_skill and coverage_ok
            elif case.decision == "partial":
                decision_ok = top1_ok and decision.coverage == "partial_skill"
            else:
                decision_ok = top1_ok and coverage_ok
            precondition_ok = (
                True
                if not case.expected_precondition_status
                else (
                    decision.precondition_evaluated
                    and decision.precondition_status == case.expected_precondition_status
                    and decision.execution_ready == case.expected_execution_ready
                )
            )
            results.append(
                RoutingOracleCaseResult(
                    id=case.id,
                    expected_domain=case.domain,
                    observed_domain=decision.domain,
                    expected_skills=case.expected_skills,
                    observed_skill=decision.chosen_skill,
                    observed_top3=observed_top3,
                    expected_coverage=case.expected_coverage,
                    observed_coverage=decision.coverage,
                    top1_ok=top1_ok,
                    top3_ok=top3_ok,
                    domain_ok=decision.domain == case.domain,
                    decision_ok=decision_ok,
                    expected_precondition_status=case.expected_precondition_status,
                    observed_precondition_status=decision.precondition_status,
                    expected_execution_ready=case.expected_execution_ready,
                    observed_precondition_evaluated=decision.precondition_evaluated,
                    observed_execution_ready=decision.execution_ready,
                    precondition_ok=precondition_ok,
                    hallucinated_aliases=hallucinated,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the oracle reports resolver crashes per case
            results.append(
                RoutingOracleCaseResult(
                    id=case.id,
                    expected_domain=case.domain,
                    observed_domain="",
                    expected_skills=case.expected_skills,
                    observed_skill="",
                    observed_top3=(),
                    expected_coverage=case.expected_coverage,
                    observed_coverage="",
                    top1_ok=False,
                    top3_ok=False,
                    domain_ok=False,
                    decision_ok=False,
                    expected_precondition_status=case.expected_precondition_status,
                    expected_execution_ready=case.expected_execution_ready,
                    precondition_ok=not case.expected_precondition_status,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    routed = [result for result in results if result.expected_skills]
    precondition_cases = [
        result for result in results if result.expected_precondition_status
    ]
    metrics = {
        "precision_at_1": _ratio(sum(result.top1_ok for result in routed), len(routed)),
        "top3_recall": _ratio(sum(result.top3_ok for result in routed), len(routed)),
        "domain_accuracy": _ratio(sum(result.domain_ok for result in results), len(results)),
        "decision_accuracy": _ratio(sum(result.decision_ok for result in results), len(results)),
        "hallucinated_alias_rate": _ratio(alias_hallucinations, alias_predictions),
        "precondition_accuracy": (
            _ratio(
                sum(result.precondition_ok for result in precondition_cases),
                len(precondition_cases),
            )
            if precondition_cases
            else 1.0
        ),
    }
    threshold_failures: list[str] = []
    for name, threshold in oracle.thresholds.items():
        actual = metrics.get(name, 0.0)
        if name == "hallucinated_alias_rate":
            if actual > threshold:
                threshold_failures.append(f"{name}={actual:.3f} exceeds maximum {threshold:.3f}")
        elif actual < threshold:
            threshold_failures.append(f"{name}={actual:.3f} is below minimum {threshold:.3f}")

    per_domain: dict[str, dict[str, float]] = {}
    for domain in sorted({case.domain for case in oracle.cases}):
        bucket = [result for result in results if result.expected_domain == domain]
        bucket_routed = [result for result in bucket if result.expected_skills]
        per_domain[domain] = {
            "cases": float(len(bucket)),
            "precision_at_1": _ratio(
                sum(result.top1_ok for result in bucket_routed), len(bucket_routed)
            ),
            "top3_recall": _ratio(
                sum(result.top3_ok for result in bucket_routed), len(bucket_routed)
            ),
            "domain_accuracy": _ratio(
                sum(result.domain_ok for result in bucket), len(bucket)
            ),
            "decision_accuracy": _ratio(
                sum(result.decision_ok for result in bucket), len(bucket)
            ),
        }
    for domain, domain_metrics in per_domain.items():
        for name, threshold in oracle.per_domain_thresholds.items():
            actual = domain_metrics.get(name, 0.0)
            if actual < threshold:
                threshold_failures.append(
                    f"{domain}.{name}={actual:.3f} is below per-domain minimum "
                    f"{threshold:.3f}"
                )

    return RoutingOracleReport(
        oracle_version=oracle.oracle_version,
        metrics=metrics,
        thresholds=dict(oracle.thresholds),
        per_domain_thresholds=dict(oracle.per_domain_thresholds),
        case_results=results,
        per_domain=per_domain,
        validation_errors=list(oracle.validation_errors),
        threshold_failures=threshold_failures,
    )


__all__ = [
    "RoutingOracle",
    "RoutingOracleCase",
    "RoutingOracleCaseResult",
    "RoutingOracleReport",
    "evaluate_routing_oracle",
    "load_routing_oracle",
]
