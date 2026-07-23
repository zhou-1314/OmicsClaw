"""Evaluation execution + result store (ADR 0074 M-C, shared-runner phase).

Phased implementation (approved 2026-07-23): evaluations run through the
existing shared-runner / bounded protocol-entry execution primitive rather than
the RunRuntime governed queue that design §10 targets. Migrating to the
RunRuntime queue — which adds resource scheduling, cancellation and full
AuditOperation observability — is a follow-up; the ADR §10 "Deferred" note
records the phasing.

This module owns two things:

- ``EvaluationResultStore``: an append-only JSONL store of protocol evaluation
  results, keyed by exact Skill revision, that the audit derivation reads to
  earn ``fixture-validated`` / ``benchmarked`` (via
  ``skill_audit.derive_experience_view``'s ``protocol_results``).
- ``run_protocol_evaluations``: pure orchestration that turns an injected
  per-protocol run outcome into digest-bound ``ProtocolEvaluationResult`` values.
  The concrete "run this protocol" primitive (shared runner for ``demo``, bounded
  subprocess for a test-backed protocol) is injected by the caller, so this core
  is deterministic and testable without a real subprocess.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from omicsclaw.skill.evolution import _exclusive_file_lock
from omicsclaw.skill.skill_audit import ProtocolEvaluationResult, SkillRevision

__all__ = [
    "EvaluationStoreCorruptError",
    "EvaluationResultStore",
    "run_protocol_evaluations",
    "default_evaluation_result_store",
]

_SCHEMA_VERSION = 1


class EvaluationStoreCorruptError(RuntimeError):
    """A stored evaluation row could not be parsed (fail closed, ADR 0074 §11.2)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvaluationResultStore:
    """Append-only JSONL store of protocol evaluation results, per revision.

    Mirrors ``SkillHealthLedger``: one row per result, an exclusive file lock
    around each read/write, and a corrupt row that fails closed rather than being
    silently skipped (a partial read must never masquerade as complete evidence).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def append(self, revision: SkillRevision, result: ProtocolEvaluationResult) -> None:
        row = {
            "schema_version": _SCHEMA_VERSION,
            "revision": revision.to_dict(),
            "result": asdict(result),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, sort_keys=True, ensure_ascii=False)
        with _exclusive_file_lock(self._lock_path()):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with _exclusive_file_lock(self._lock_path()):
            text = self.path.read_text(encoding="utf-8")
        rows: list[dict] = []
        for lineno, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise EvaluationStoreCorruptError(
                    f"evaluation store {self.path} corrupt at line {lineno}"
                ) from exc
        return rows

    def results_for(self, revision: SkillRevision) -> list[ProtocolEvaluationResult]:
        """Every stored result bound to this exact revision, in append order."""
        want = revision.to_dict()
        out: list[ProtocolEvaluationResult] = []
        for row in self._rows():
            if row.get("revision") != want:
                continue
            result = row.get("result") or {}
            raw_metrics = result.get("metrics") or {}
            metrics = {
                str(name): float(value)
                for name, value in raw_metrics.items()
                if isinstance(value, (int, float))
            }
            out.append(
                ProtocolEvaluationResult(
                    protocol_id=str(result.get("protocol_id", "")),
                    kind=str(result.get("kind", "")),
                    protocol_digest=str(result.get("protocol_digest", "")),
                    outcome=str(result.get("outcome", "")),
                    occurred_at=str(result.get("occurred_at", "")),
                    run_index=int(result.get("run_index", 0) or 0),
                    repeats=int(result.get("repeats", 1) or 1),
                    metrics=metrics,
                )
            )
        return out


# A ``run_one`` maps one declared protocol spec to a run outcome. It returns
# either the outcome string ("succeeded" earns; anything else does not) or a
# ``(outcome, metrics)`` pair whose metrics map is allowlist-filtered here.
ProtocolRunner = Callable[
    [Mapping[str, object]], "str | tuple[str, Mapping[str, object]]"
]


def _bounded_repeats(value: object) -> int:
    """A protocol's repeat count, clamped to the schema's 1..100 bound."""
    try:
        return max(1, min(int(value), 100))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _normalize_run(raw: object) -> tuple[str, Mapping[str, object]]:
    """Split a runner return into ``(outcome, raw_metrics)``.

    Back-compatible: a bare string is an outcome with no metrics; a
    ``(outcome, metrics)`` pair carries a metrics map to be allowlist-filtered.
    """
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Mapping):
        return str(raw[0]), raw[1]
    return str(raw), {}


def _allowlisted_metrics(
    raw_metrics: Mapping[str, object], allowlist: frozenset[str]
) -> dict[str, float]:
    """Keep only allowlisted names with finite numeric (non-bool) values."""
    if not allowlist:
        return {}
    out: dict[str, float] = {}
    for name, value in raw_metrics.items():
        if name not in allowlist or isinstance(value, bool):
            continue
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")):
            continue
        out[str(name)] = number
    return out


def run_protocol_evaluations(
    revision: SkillRevision,
    protocols: Sequence[tuple[Mapping[str, object], str]],
    run_one: ProtocolRunner,
    *,
    now: Callable[[], str] = _utc_now_iso,
) -> list[ProtocolEvaluationResult]:
    """Run each ``(protocol_spec, protocol_digest)`` and build bound results.

    A protocol declaring ``repeats > 1`` (a stability protocol) is run that many
    times, one result per run, so the audit derivation can aggregate repeated-run
    success rate and metric dispersion (ADR 0074 §6.3). Each run's published
    metrics are filtered to the protocol's ``metrics`` allowlist, so a runner can
    never inject arbitrary keys into the read model. ``now()`` stamps each result.
    Pure orchestration — persisting the results is the caller's decision, so this
    stays deterministic under an injected clock and runner.
    """
    results: list[ProtocolEvaluationResult] = []
    for spec, digest in protocols:
        repeats = _bounded_repeats(spec.get("repeats", 1))
        allowlist = frozenset(str(name) for name in (spec.get("metrics") or ()))
        for run_index in range(repeats):
            outcome, raw_metrics = _normalize_run(run_one(spec))
            results.append(
                ProtocolEvaluationResult(
                    protocol_id=str(spec.get("id", "")),
                    kind=str(spec.get("kind", "")),
                    protocol_digest=digest,
                    outcome=outcome,
                    occurred_at=now(),
                    run_index=run_index,
                    repeats=repeats,
                    metrics=_allowlisted_metrics(raw_metrics, allowlist),
                )
            )
    return results


def default_evaluation_result_store() -> EvaluationResultStore:
    """The process-default evaluation store (``OMICSCLAW_EVALUATION_STORE`` or a default path)."""
    configured = os.environ.get("OMICSCLAW_EVALUATION_STORE", "").strip()
    if configured:
        return EvaluationResultStore(configured)
    from omicsclaw.skill.evolution import default_skill_health_ledger

    ledger_path = default_skill_health_ledger().path
    return EvaluationResultStore(ledger_path.with_name("skill_evaluations.jsonl"))
