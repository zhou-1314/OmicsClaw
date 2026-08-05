"""Evaluation execution + result store (ADR 0074 M-C, shared-runner phase).

Phased implementation (approved 2026-07-23): evaluations run through the
existing shared-runner / bounded protocol-entry execution primitive rather than
the RunRuntime governed queue that design §10 targets. Migrating to the
RunRuntime queue — which adds resource scheduling, cancellation and full
AuditOperation observability — is a follow-up; the ADR §10 "Deferred" note
records the phasing.

This module owns three things:

- ``EvaluationArtifactStore``: a local content-addressed store that keeps
  bounded logs, structured verifier output, and referenced trace/report bytes
  resolvable after evaluator scratch is deleted.
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
import hashlib
import hmac
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from omicsclaw.skill.evolution import (
    _atomic_write,
    _exclusive_file_lock,
    _fsync_directory,
)
from omicsclaw.skill.skill_audit import ProtocolEvaluationResult, SkillRevision

__all__ = [
    "PROTOCOL_OUTCOMES",
    "PROTOCOL_REASON_CODES",
    "ProtocolRunOutcome",
    "EvaluationArtifactStore",
    "EvaluationArtifactStoreError",
    "EvaluationResultConflictError",
    "EvaluationStoreCorruptError",
    "EvaluationResultStore",
    "run_protocol_evaluations",
    "default_evaluation_artifact_store",
    "default_evaluation_result_store",
]

PROTOCOL_OUTCOMES = frozenset({"succeeded", "failed"})
PROTOCOL_REASON_CODES = frozenset(
    {
        "none",
        "protocol_failed",
        "timeout",
        "execution_error",
        "invalid_entry",
        "invalid_runner",
        "output_not_fresh",
        "output_unavailable",
        "result_invalid",
        "revision_unverified",
        "revision_mismatch",
        "skill_failed",
        "dataset_invalid",
        "dataset_integrity_failed",
        "semantic_contract_failed",
        "omicbench_failed",
        "missing_dependency",
        "bad_input",
        "resource_exhausted",
        "cancelled",
        "script_defect",
        "contract_failure",
        "contract_validator_failed",
        "upstream_failed",
        "unknown",
    }
)
_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_BARE_SHA256_REF = re.compile(r"^sha256:([0-9a-f]{64})$")
_ARTIFACT_REF = re.compile(r"^evaluation-artifact:sha256:([0-9a-f]{64})$")
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_CAPTURE_FILES = 4096
_MAX_BUNDLE_BYTES = 1024 * 1024
_V1_RESULT_FIELDS = frozenset(
    {
        "protocol_id",
        "kind",
        "protocol_digest",
        "outcome",
        "occurred_at",
        "run_index",
        "repeats",
        "metrics",
    }
)

_SCHEMA_VERSION = 2
_ROW_FIELDS = frozenset({"schema_version", "revision", "result"})
_REVISION_FIELDS = frozenset(
    {"skill_id", "version", "manifest_hash", "source_hash"}
)
_RESULT_FIELDS = frozenset(
    {
        "protocol_id",
        "kind",
        "protocol_digest",
        "outcome",
        "occurred_at",
        "run_index",
        "repeats",
        "metrics",
        "result_id",
        "evaluation_id",
        "environment_id",
        "dataset_digest",
        "reason_code",
        "evidence_refs",
        "duration_seconds",
    }
)
_RESULT_STRING_FIELDS = frozenset(
    {
        "protocol_id",
        "kind",
        "protocol_digest",
        "outcome",
        "occurred_at",
        "result_id",
        "evaluation_id",
        "environment_id",
        "dataset_digest",
        "reason_code",
    }
)


@dataclass(frozen=True, slots=True)
class ProtocolRunOutcome:
    """Structured result returned by one Evaluation Protocol execution.

    Bare outcome strings and ``(outcome, metrics)`` pairs remain accepted by
    :func:`run_protocol_evaluations` for the phased evaluator's existing
    callers. New executors use this value so attribution and reproducibility
    evidence survive orchestration without passing an unbounded result dict.
    """

    outcome: str
    reason_code: str = "none"
    metrics: Mapping[str, object] = field(default_factory=dict)
    environment_id: str = ""
    dataset_digest: str = ""
    evidence_refs: tuple[str, ...] = ()
    duration_seconds: float = 0.0


class EvaluationResultConflictError(RuntimeError):
    """A result id was reused for different evaluation evidence."""


class EvaluationStoreCorruptError(RuntimeError):
    """A stored evaluation row could not be parsed (fail closed, ADR 0074 §11.2)."""


class EvaluationArtifactStoreError(RuntimeError):
    """Evaluation provenance could not be durably stored or verified."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvaluationArtifactStore:
    """Local content-addressed evidence store for Evaluation Protocol runs.

    The append-only result schema keeps only opaque ``evidence_refs``. This
    store makes one such reference resolvable without embedding logs, paths, or
    verifier output in the audit ledger. Objects are immutable SHA-256 blobs;
    one small bundle object maps bounded run-local roles to their blob refs.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def _lock_path(self) -> Path:
        return self.root / ".store.lock"

    def _blob_path(self, digest: str) -> Path:
        return self.root / "objects" / digest[:2] / digest[2:]

    @staticmethod
    def _ref(digest: str) -> str:
        return f"evaluation-artifact:sha256:{digest}"

    @staticmethod
    def _digest_from_ref(reference: str) -> str:
        match = _ARTIFACT_REF.fullmatch(reference)
        if match is None:
            raise EvaluationArtifactStoreError("invalid evaluation artifact reference")
        return match.group(1)

    def put_bytes(self, payload: bytes) -> str:
        """Durably publish one immutable blob and return its opaque reference."""
        if not isinstance(payload, bytes):
            raise TypeError("evaluation artifact payload must be bytes")
        if len(payload) > _MAX_ARTIFACT_BYTES:
            raise EvaluationArtifactStoreError("evaluation artifact exceeds size limit")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._blob_path(digest)
        with _exclusive_file_lock(self._lock_path()):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise EvaluationArtifactStoreError(
                        "evaluation artifact object path is not a regular file"
                    )
                try:
                    existing = path.read_bytes()
                except OSError as exc:
                    raise EvaluationArtifactStoreError(
                        "evaluation artifact object could not be read"
                    ) from exc
                if existing != payload:
                    raise EvaluationArtifactStoreError(
                        "evaluation artifact object conflicts with its digest"
                    )
                return self._ref(digest)
            try:
                _atomic_write(path, payload, mode=0o600)
            except OSError as exc:
                raise EvaluationArtifactStoreError(
                    "evaluation artifact object could not be published"
                ) from exc
        return self._ref(digest)

    def read_bytes(self, reference: str) -> bytes:
        """Resolve and integrity-check one artifact reference."""
        digest = self._digest_from_ref(reference)
        path = self._blob_path(digest)
        if path.is_symlink() or not path.is_file():
            raise EvaluationArtifactStoreError("evaluation artifact object is missing")
        try:
            if path.stat().st_size > _MAX_ARTIFACT_BYTES:
                raise EvaluationArtifactStoreError(
                    "evaluation artifact object exceeds size limit"
                )
            payload = path.read_bytes()
        except OSError as exc:
            raise EvaluationArtifactStoreError(
                "evaluation artifact object could not be read"
            ) from exc
        observed = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(observed, digest):
            raise EvaluationArtifactStoreError(
                "evaluation artifact object failed integrity verification"
            )
        return payload

    def read_json(self, reference: str) -> dict[str, object]:
        """Resolve one bounded JSON object, primarily an artifact bundle."""
        payload = self.read_bytes(reference)
        if len(payload) > _MAX_BUNDLE_BYTES:
            raise EvaluationArtifactStoreError("evaluation artifact JSON is too large")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise EvaluationArtifactStoreError(
                "evaluation artifact is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise EvaluationArtifactStoreError(
                "evaluation artifact JSON must be an object"
            )
        return value

    def capture_protocol_output(
        self,
        output_dir: str | Path,
        evidence_refs: Sequence[str],
        *,
        protocol_id: str = "",
        protocol_kind: str = "",
    ) -> str:
        """Persist bounded logs/results and hash-addressed verifier evidence.

        Only files inside the fresh evaluator output directory are considered.
        Symlinks are never followed. Bare ``sha256:...`` verifier references are
        resolved by hashing bounded regular output files; unmatched references
        remain explicit in the bundle rather than being mistaken for stored
        evidence.
        """
        root_path = Path(output_dir)
        if root_path.is_symlink() or not root_path.is_dir():
            raise EvaluationArtifactStoreError(
                "evaluation output is not a regular directory"
            )
        try:
            root = root_path.resolve(strict=True)
        except OSError as exc:
            raise EvaluationArtifactStoreError(
                "evaluation output could not be resolved"
            ) from exc

        requested = {
            match.group(1): reference
            for reference in evidence_refs
            if (match := _BARE_SHA256_REF.fullmatch(reference)) is not None
        }
        matched: set[str] = set()
        artifacts: list[dict[str, object]] = []
        captured_paths: set[Path] = set()

        def capture(path: Path, *, role: str) -> None:
            if path in captured_paths:
                return
            captured_paths.add(path)
            if path.is_symlink() or not path.is_file():
                return
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise EvaluationArtifactStoreError(
                    "evaluation artifact metadata could not be read"
                ) from exc
            if size > _MAX_ARTIFACT_BYTES:
                return
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise EvaluationArtifactStoreError(
                    "evaluation artifact bytes could not be read"
                ) from exc
            reference = self.put_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            if digest in requested:
                matched.add(digest)
            artifacts.append(
                {
                    "role": role,
                    "relative_path": path.relative_to(root).as_posix(),
                    "ref": reference,
                    "content_sha256": f"sha256:{digest}",
                    "byte_size": len(payload),
                }
            )

        for name, role in (
            ("stdout.log", "stdout"),
            ("stderr.log", "stderr"),
            ("evaluation_result.json", "evaluation_result"),
            ("result.json", "skill_result"),
        ):
            capture(root / name, role=role)

        seen_files = 0

        def enumeration_error(error: OSError) -> None:
            raise EvaluationArtifactStoreError(
                "evaluation output could not be enumerated"
            ) from error

        output_walk = (
            os.walk(root, topdown=True, followlinks=False, onerror=enumeration_error)
            if requested
            else ()
        )
        for current, directories, files in output_walk:
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if not (current_path / name).is_symlink()
            )
            for name in sorted(files):
                seen_files += 1
                if seen_files > _MAX_CAPTURE_FILES:
                    raise EvaluationArtifactStoreError(
                        "evaluation output contains too many files"
                    )
                path = current_path / name
                if path in captured_paths or path.is_symlink() or not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    raise EvaluationArtifactStoreError(
                        "evaluation artifact metadata could not be read"
                    ) from exc
                if size > _MAX_ARTIFACT_BYTES or not requested:
                    continue
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    raise EvaluationArtifactStoreError(
                        "evaluation artifact bytes could not be read"
                    ) from exc
                digest = hashlib.sha256(payload).hexdigest()
                if digest not in requested:
                    continue
                reference = self.put_bytes(payload)
                matched.add(digest)
                captured_paths.add(path)
                artifacts.append(
                    {
                        "role": "evidence",
                        "relative_path": path.relative_to(root).as_posix(),
                        "ref": reference,
                        "content_sha256": f"sha256:{digest}",
                        "byte_size": len(payload),
                    }
                )
            if matched == set(requested):
                break

        bundle = {
            "schema_version": 1,
            "protocol_id": protocol_id,
            "protocol_kind": protocol_kind,
            "artifacts": sorted(
                artifacts,
                key=lambda item: (str(item["role"]), str(item["relative_path"])),
            ),
            "unresolved_evidence_refs": [
                requested[digest]
                for digest in sorted(requested)
                if digest not in matched
            ],
        }
        payload = json.dumps(
            bundle,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_BUNDLE_BYTES:
            raise EvaluationArtifactStoreError(
                "evaluation artifact bundle exceeds size limit"
            )
        return self.put_bytes(payload)


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
        candidate = {
            "schema_version": _SCHEMA_VERSION,
            "revision": revision.to_dict(),
            "result": asdict(result),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            line = json.dumps(
                candidate,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            row = self._validate_row(self._decode_json_row(line))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid evaluation result") from exc
        with _exclusive_file_lock(self._lock_path()):
            rows = self._rows_unlocked()
            for stored in rows:
                stored_result = stored["result"]
                if stored_result["result_id"] != result.result_id:
                    continue
                if stored == row:
                    return
                raise EvaluationResultConflictError(
                    f"evaluation result id already has different evidence: "
                    f"{result.result_id}"
                )
            existed = self.path.exists()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                _fsync_directory(self.path.parent)

    @staticmethod
    def _validate_row(
        value: object,
        *,
        allow_legacy_unbound: bool = False,
    ) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != _ROW_FIELDS:
            raise ValueError("row must contain exactly schema_version, revision, result")
        schema_version = value["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != _SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported schema_version: {schema_version!r}")

        revision = value["revision"]
        if not isinstance(revision, dict) or set(revision) != _REVISION_FIELDS:
            raise ValueError("revision has an invalid shape")
        if any(not isinstance(revision[name], str) for name in _REVISION_FIELDS):
            raise ValueError("revision fields must be strings")
        if not revision["skill_id"] or not revision["version"]:
            raise ValueError("revision identity fields must be non-empty")
        if not allow_legacy_unbound and any(
            _BARE_SHA256_REF.fullmatch(revision[name]) is None
            for name in ("manifest_hash", "source_hash")
        ):
            raise ValueError("revision hashes must be sha256 identities")

        result = value["result"]
        if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
            raise ValueError("result has an invalid shape")
        if any(not isinstance(result[name], str) for name in _RESULT_STRING_FIELDS):
            raise ValueError("result identity and classification fields must be strings")
        if result["kind"] not in {"demo", "fixture", "benchmark", "stability"}:
            raise ValueError("result kind is not recognized")
        if result["outcome"] not in PROTOCOL_OUTCOMES:
            raise ValueError("result outcome is not recognized")
        if result["reason_code"] not in PROTOCOL_REASON_CODES:
            raise ValueError("result reason_code is not recognized")
        if not result["protocol_id"] or len(result["protocol_id"]) > 128:
            raise ValueError("protocol_id must be a non-empty bounded string")
        if not result["occurred_at"] or len(result["occurred_at"]) > 128:
            raise ValueError("occurred_at must be a non-empty bounded string")
        if not allow_legacy_unbound:
            if _BARE_SHA256_REF.fullmatch(result["protocol_digest"]) is None:
                raise ValueError("protocol_digest must be a sha256 identity")
            if _BARE_SHA256_REF.fullmatch(result["environment_id"]) is None:
                raise ValueError("environment_id must be a sha256 identity")
            if result["dataset_digest"] and _BARE_SHA256_REF.fullmatch(
                result["dataset_digest"]
            ) is None:
                raise ValueError("dataset_digest must be empty or a sha256 identity")
            if result["kind"] == "benchmark" and not result["dataset_digest"]:
                raise ValueError("benchmark results require a dataset identity")
        if (result["outcome"] == "succeeded") != (result["reason_code"] == "none"):
            raise ValueError("outcome and reason_code contradict each other")
        if not _OPAQUE_ID.fullmatch(result["result_id"]):
            raise ValueError("result_id must be 32 lowercase hexadecimal characters")
        if not _OPAQUE_ID.fullmatch(result["evaluation_id"]):
            raise ValueError("evaluation_id must be 32 lowercase hexadecimal characters")

        run_index = result["run_index"]
        repeats = result["repeats"]
        if (
            isinstance(run_index, bool)
            or not isinstance(run_index, int)
            or run_index < 0
        ):
            raise ValueError("run_index must be a non-negative integer")
        if (
            isinstance(repeats, bool)
            or not isinstance(repeats, int)
            or not 1 <= repeats <= 100
        ):
            raise ValueError("repeats must be an integer in 1..100")
        if run_index >= repeats:
            raise ValueError("run_index must be less than repeats")

        metrics = result["metrics"]
        if not isinstance(metrics, dict) or len(metrics) > 32:
            raise ValueError("metrics must be a mapping with at most 32 entries")
        for name, metric in metrics.items():
            if not isinstance(name, str) or not name or len(name) > 64:
                raise ValueError("metric names must be non-empty bounded strings")
            if isinstance(metric, bool) or not isinstance(metric, (int, float)):
                raise ValueError("metric values must be numeric")
            try:
                finite_metric = math.isfinite(float(metric))
            except OverflowError as exc:
                raise ValueError("metric values must be finite") from exc
            if not finite_metric:
                raise ValueError("metric values must be finite")

        evidence_refs = result["evidence_refs"]
        if (
            not isinstance(evidence_refs, list)
            or len(evidence_refs) > 20
            or any(
                not isinstance(item, str) or not item or len(item) > 256
                for item in evidence_refs
            )
        ):
            raise ValueError("evidence_refs must be a bounded list of references")

        duration = result["duration_seconds"]
        try:
            numeric_duration = float(duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "duration_seconds must be a finite non-negative number"
            ) from exc
        if (
            isinstance(duration, bool)
            or not math.isfinite(numeric_duration)
            or numeric_duration < 0.0
        ):
            raise ValueError("duration_seconds must be a finite non-negative number")
        return value

    @classmethod
    def _migrate_v1_row(cls, value: object) -> dict[str, object]:
        """Conservatively project a legacy v1 row into the strict v2 shape.

        v1 had no batch, environment, dataset, reason, or result identities.
        Deterministic per-row IDs keep reads stable, while using a distinct
        evaluation ID per row deliberately prevents separate legacy repeats
        from being assembled into a newly trusted batch.
        """
        if not isinstance(value, dict) or set(value) != _ROW_FIELDS:
            raise ValueError("legacy row has an invalid shape")
        revision = value.get("revision")
        result = value.get("result")
        if (
            value.get("schema_version") != 1
            or not isinstance(revision, dict)
            or set(revision) != _REVISION_FIELDS
            or any(not isinstance(revision[name], str) for name in _REVISION_FIELDS)
            or not isinstance(result, dict)
            or set(result) != _V1_RESULT_FIELDS
        ):
            raise ValueError("legacy row has an invalid shape")
        identity_bytes = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        result_id = hashlib.sha256(b"evaluation-v1-row\0" + identity_bytes).hexdigest()[:32]
        migrated = {
            "schema_version": _SCHEMA_VERSION,
            "revision": revision,
            "result": {
                **result,
                "result_id": result_id,
                "evaluation_id": result_id,
                "environment_id": "",
                "dataset_digest": "",
                "reason_code": (
                    "none" if result.get("outcome") == "succeeded" else "protocol_failed"
                ),
                "evidence_refs": [],
                "duration_seconds": 0.0,
            },
        }
        return cls._validate_row(migrated, allow_legacy_unbound=True)

    @staticmethod
    def _decode_json_row(raw: str) -> object:
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

    def _rows_unlocked(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise EvaluationStoreCorruptError(
                f"evaluation store {self.path} is not valid UTF-8"
            ) from exc
        rows: list[dict[str, object]] = []
        identified: dict[str, dict[str, object]] = {}
        for lineno, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                decoded = self._decode_json_row(stripped)
                if isinstance(decoded, dict) and decoded.get("schema_version") == 1:
                    row = self._migrate_v1_row(decoded)
                else:
                    row = self._validate_row(decoded)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise EvaluationStoreCorruptError(
                    f"evaluation store {self.path} corrupt at line {lineno}"
                ) from exc
            result = row["result"]
            assert isinstance(result, dict)  # established by _validate_row
            result_id = result["result_id"]
            assert isinstance(result_id, str)
            previous = identified.get(result_id)
            if previous is not None:
                if previous != row:
                    raise EvaluationStoreCorruptError(
                        f"evaluation store {self.path} has conflicting result id "
                        f"at line {lineno}"
                    )
                continue
            identified[result_id] = row
            rows.append(row)
        return rows

    def _rows(self) -> list[dict[str, object]]:
        with _exclusive_file_lock(self._lock_path()):
            return self._rows_unlocked()

    def results_for(self, revision: SkillRevision) -> list[ProtocolEvaluationResult]:
        """Every stored result bound to this exact revision, in append order."""
        want = revision.to_dict()
        out: list[ProtocolEvaluationResult] = []
        for row in self._rows():
            if row["revision"] != want:
                continue
            result = row["result"]
            assert isinstance(result, dict)  # established by _validate_row
            raw_metrics = result["metrics"]
            assert isinstance(raw_metrics, dict)
            metrics = {
                str(name): float(value)
                for name, value in raw_metrics.items()
            }
            out.append(
                ProtocolEvaluationResult(
                    protocol_id=str(result["protocol_id"]),
                    kind=str(result["kind"]),
                    protocol_digest=str(result["protocol_digest"]),
                    outcome=str(result["outcome"]),
                    occurred_at=str(result["occurred_at"]),
                    run_index=int(result["run_index"]),
                    repeats=int(result["repeats"]),
                    metrics=metrics,
                    result_id=str(result["result_id"]),
                    evaluation_id=str(result["evaluation_id"]),
                    environment_id=str(result["environment_id"]),
                    dataset_digest=str(result["dataset_digest"]),
                    reason_code=str(result["reason_code"]),
                    evidence_refs=tuple(result["evidence_refs"]),
                    duration_seconds=float(result["duration_seconds"]),
                )
            )
        return out


# A ``run_one`` maps one declared protocol spec to a run outcome. It returns
# either the outcome string ("succeeded" earns; anything else does not) or a
# ``(outcome, metrics)`` pair whose metrics map is allowlist-filtered here.
ProtocolRunner = Callable[
    [Mapping[str, object]],
    "str | tuple[str, Mapping[str, object]] | ProtocolRunOutcome",
]


def _bounded_repeats(value: object) -> int:
    """A protocol's repeat count, clamped to the schema's 1..100 bound."""
    try:
        return max(1, min(int(value), 100))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 1


def _normalize_run(raw: object) -> ProtocolRunOutcome:
    """Normalize legacy runner returns to a structured protocol outcome.

    Back-compatible: a bare string is an outcome with no metrics; a
    ``(outcome, metrics)`` pair carries a metrics map to be allowlist-filtered.
    """
    if isinstance(raw, ProtocolRunOutcome):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Mapping):
        return ProtocolRunOutcome(outcome=str(raw[0]), metrics=raw[1])
    return ProtocolRunOutcome(outcome=str(raw))


def _declared_dataset_digest(spec: Mapping[str, object]) -> str:
    direct = spec.get("dataset_digest")
    if isinstance(direct, str) and direct:
        return direct
    dataset_ref = spec.get("dataset_ref")
    if isinstance(dataset_ref, Mapping):
        value = dataset_ref.get("content_sha256")
    else:
        value = getattr(dataset_ref, "content_sha256", "")
    return value if isinstance(value, str) else ""


def _spec_string(spec: Mapping[str, object], name: str) -> str:
    value = spec.get(name)
    return value if isinstance(value, str) else ""


def _evidence_refs(
    spec: Mapping[str, object], run: ProtocolRunOutcome
) -> tuple[str, ...]:
    declared = spec.get("evidence_refs")
    values: list[object] = []
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        values.extend(declared)
    values.extend(run.evidence_refs)
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))


def _bounded_duration(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return duration if math.isfinite(duration) and duration >= 0.0 else 0.0


def _result_reason(spec: Mapping[str, object], run: ProtocolRunOutcome) -> str:
    reason = (
        run.reason_code
        if run.reason_code and run.reason_code != "none"
        else (_spec_string(spec, "reason_code") or run.reason_code or "none")
    )
    if run.outcome != "succeeded" and reason == "none":
        return "protocol_failed"
    return reason


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
        except (TypeError, ValueError, OverflowError):
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
    evaluation_id = uuid4().hex
    result_ids: set[str] = set()
    for spec, digest in protocols:
        repeats = _bounded_repeats(spec.get("repeats", 1))
        allowlist = frozenset(str(name) for name in (spec.get("metrics") or ()))
        for run_index in range(repeats):
            run = _normalize_run(run_one(spec))
            result_id = uuid4().hex
            while result_id in result_ids:  # defensive against an injected UUID source
                result_id = uuid4().hex
            result_ids.add(result_id)
            results.append(
                ProtocolEvaluationResult(
                    protocol_id=str(spec.get("id", "")),
                    kind=str(spec.get("kind", "")),
                    protocol_digest=digest,
                    outcome=run.outcome,
                    occurred_at=now(),
                    run_index=run_index,
                    repeats=repeats,
                    metrics=_allowlisted_metrics(run.metrics, allowlist),
                    result_id=result_id,
                    evaluation_id=evaluation_id,
                    environment_id=(
                        run.environment_id or _spec_string(spec, "environment_id")
                    ),
                    dataset_digest=(
                        run.dataset_digest or _declared_dataset_digest(spec)
                    ),
                    reason_code=_result_reason(spec, run),
                    evidence_refs=_evidence_refs(spec, run),
                    duration_seconds=_bounded_duration(
                        run.duration_seconds
                        if run.duration_seconds
                        else spec.get("duration_seconds", 0.0)
                    ),
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


def default_evaluation_artifact_store(
    result_store: EvaluationResultStore | None = None,
) -> EvaluationArtifactStore:
    """Local artifact store beside the result ledger unless explicitly configured."""
    configured = os.environ.get("OMICSCLAW_EVALUATION_ARTIFACT_STORE", "").strip()
    if configured:
        return EvaluationArtifactStore(configured)
    store = result_store or default_evaluation_result_store()
    return EvaluationArtifactStore(store.path.with_name(store.path.name + ".artifacts"))
