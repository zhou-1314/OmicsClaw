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
            out.append(
                ProtocolEvaluationResult(
                    protocol_id=str(result.get("protocol_id", "")),
                    kind=str(result.get("kind", "")),
                    protocol_digest=str(result.get("protocol_digest", "")),
                    outcome=str(result.get("outcome", "")),
                    occurred_at=str(result.get("occurred_at", "")),
                )
            )
        return out


# A ``run_one`` maps one declared protocol spec to a run outcome string
# ("succeeded" earns; anything else does not).
ProtocolRunner = Callable[[Mapping[str, object]], str]


def run_protocol_evaluations(
    revision: SkillRevision,
    protocols: Sequence[tuple[Mapping[str, object], str]],
    run_one: ProtocolRunner,
    *,
    now: Callable[[], str] = _utc_now_iso,
) -> list[ProtocolEvaluationResult]:
    """Run each ``(protocol_spec, protocol_digest)`` and build bound results.

    ``run_one(spec)`` returns the outcome string; ``now()`` stamps the result.
    Pure orchestration — persisting the results is the caller's decision, so this
    stays deterministic under an injected clock and runner.
    """
    results: list[ProtocolEvaluationResult] = []
    for spec, digest in protocols:
        outcome = run_one(spec)
        results.append(
            ProtocolEvaluationResult(
                protocol_id=str(spec.get("id", "")),
                kind=str(spec.get("kind", "")),
                protocol_digest=digest,
                outcome=str(outcome),
                occurred_at=now(),
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
