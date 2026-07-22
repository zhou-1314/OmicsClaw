"""Deep SQLite repository for authoritative local Control Plane State.

This Module deliberately exposes domain commands rather than raw CRUD or a
shared SQL connection. It has no Surface integration and contains no executable
Turn/Run payloads.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence

from omicsclaw.attachments.models import AttachmentBatchCommitment

from .delivery_content import DEFAULT_MAX_TEXT_ITEMS
from .errors import (
    AutoAgentActiveCapacityError,
    AutoAgentCapacityError,
    ControlIntegrityError,
    DeliveryCapacityExceededError,
    DeliveryResendNotSettledError,
    RepositoryClosedError,
    RunIntegrityIncidentError,
)
from .locking import ControlDatabaseLock
from .models import (
    AutoAgentCancellationResult,
    AutoAgentSessionRecord,
    AutoAgentStartupReconciliationResult,
    AssignmentResult,
    AssignmentStatus,
    AttemptStartResult,
    ConversationRecord,
    DeliveryAttemptClaim,
    DeliveryAttemptOutcome,
    DeliveryAttemptRequest,
    DeliveryAttemptRecord,
    DeliveryCandidate,
    DeliveryCapacitySnapshot,
    DeliveryStartupRecoveryResult,
    DeliveryStatusSummary,
    DeliveryItemPlan,
    DeliveryItemRecord,
    DeliveryPlan,
    DeliveryRecord,
    IdempotencyInspection,
    ProjectLifecycleResult,
    ProjectLifecycleStatus,
    ProjectRecord,
    ProjectionIntentInput,
    ProjectionIntentRecord,
    RunAcceptancePlan,
    RunAcceptanceIntent,
    RunAcceptanceResult,
    RunAcceptanceStatus,
    RunAssignmentRecord,
    RunIntegrityEvidenceCode,
    RunIntegrityIncidentAppendResult,
    RunIntegrityIncidentIntent,
    RunIntegrityIncidentPage,
    RunIntegrityIncidentRecord,
    RunIntegrityIncidentType,
    RunObservationPage,
    RunObservationSnapshot,
    RunRecord,
    RunReport,
    RunStartupReconciliationResult,
    StateChangeResult,
    TerminalizeTurnResult,
    TurnStartupReconciliationResult,
    TurnAcceptancePlan,
    TurnAcceptanceIntent,
    TurnAcceptanceResult,
    TurnAcceptanceStatus,
    TurnObservationRecord,
    TurnRecord,
    TurnTranscriptRef,
    validate_delivery_provider_evidence,
)
from .schema import apply_migrations
from .terminal_codes import (
    RunTerminalCode,
    TurnTerminalCode,
    is_allowed_run_terminal_code,
    is_allowed_turn_terminal_code,
)


_TERMINAL_TURNS = frozenset({"succeeded", "failed", "canceled", "interrupted"})
_TERMINAL_RUNS = frozenset({"succeeded", "failed", "canceled", "interrupted"})
_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "cancel_requested",
        "succeeded",
        "failed",
        "canceled",
        "interrupted",
    }
)
_NONTERMINAL_DELIVERY_ITEMS = frozenset({"queued", "sending", "retry_wait"})
# The store's own bound on one Delivery's provider-call plan. It tracks the text
# renderer's default so a plan that renderer accepts is always committable,
# while still refusing an unbounded plan from any other producer.
MAX_DELIVERY_ITEMS = DEFAULT_MAX_TEXT_ITEMS
_SOURCE_STORES = frozenset({"transcript", "run", "attachment", "tool_result"})
_SURFACES = frozenset({"cli", "desktop", "channel"})
_AUTOAGENT_TERMINAL_STATUSES = frozenset(
    {"done", "error", "cancelled", "interrupted"}
)
_AUTOAGENT_ERROR_CODES = frozenset(
    {
        "harness_failed",
        "invalid_terminal_result",
        "worker_crashed",
        "worker_start_failed",
        "cancelled",
        "backend_restart_interrupted",
        "backend_shutdown_interrupted",
        "result_capacity_exhausted",
        "repository_failure",
    }
)
_AUTOAGENT_ERROR_DETAILS = {
    "harness_failed": "Harness evolution failed",
    "invalid_terminal_result": "Optimization result failed validation",
    "worker_crashed": "Optimization worker crashed",
    "worker_start_failed": "Optimization worker could not start",
    "cancelled": "Optimization cancelled",
    "backend_restart_interrupted": "Optimization interrupted by Backend restart",
    "backend_shutdown_interrupted": "Optimization interrupted by Backend shutdown",
    "result_capacity_exhausted": "AutoAgent durable result capacity is exhausted",
    "repository_failure": "Optimization terminal state could not be committed",
}
_AUTOAGENT_SESSION_CAPACITY = 100_000
_AUTOAGENT_ACTIVE_SESSION_CAPACITY = 4
_AUTOAGENT_RESULT_BYTES_CAPACITY = 1_073_741_824
_AUTOAGENT_CANCELLATION_CAPACITY = 100_000
_AUTOAGENT_EXECUTION_REFERENCE_TYPE = "linux-user-systemd-bwrap-v1"
_AUTOAGENT_EXECUTION_REFERENCE_RE = re.compile(
    r"^omicsclaw-run-[0-9a-f]{24}\.scope$"
)
_AUTOAGENT_OWNER_STOP_EVIDENCE = "process_tree_absent_v1"
_AUTOAGENT_RESULT_FIELDS = frozenset(
    {
        "success",
        "mode",
        "skill",
        "method",
        "evolution_goal",
        "total_iterations",
        "total_trials",
        "patches_accepted",
        "patches_rejected",
        "improvement_pct",
        "converged",
        "output_dir",
        "accepted_files",
        "accepted_patches",
        "accepted_patch_commits",
        "accepted_patch_artifacts",
        "promotion",
        "sandbox_repo",
        "source_project_commit",
        "best_score",
        "baseline_score",
        "best_metrics",
        "best_params",
    }
)
_CREDENTIAL_SHAPED_AUTOAGENT_KEYS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "provider_config",
    }
)


def _is_credential_shaped_autoagent_key(value: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
    if normalized in _CREDENTIAL_SHAPED_AUTOAGENT_KEYS:
        return True
    segments = tuple(segment for segment in normalized.split("_") if segment)
    if any(
        segment
        in {
            "authorization",
            "bearer",
            "credential",
            "credentials",
            "password",
            "secret",
            "token",
            "apikey",
        }
        for segment in segments
    ):
        return True
    collapsed = "".join(segments)
    return collapsed in {
        "accesskey",
        "accesstoken",
        "apikey",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "privatekey",
        "refreshtoken",
    }


def _normalize_autoagent_owner(
    execution_reference_type: str | None,
    execution_reference: str | None,
) -> tuple[str | None, str | None]:
    if execution_reference_type is None and execution_reference is None:
        return None, None
    if execution_reference_type != _AUTOAGENT_EXECUTION_REFERENCE_TYPE:
        raise ValueError("unsupported AutoAgent execution reference type")
    if (
        not isinstance(execution_reference, str)
        or _AUTOAGENT_EXECUTION_REFERENCE_RE.fullmatch(execution_reference) is None
    ):
        raise ValueError("invalid AutoAgent execution reference")
    return execution_reference_type, execution_reference


def _normalize_autoagent_output_dir(
    value: str | None,
    *,
    cwd: str,
    session_id: str,
) -> str:
    if value is None:
        base = Path(cwd) if cwd else Path.cwd()
        candidate = base / ".omicsclaw" / "autoagent-internal" / session_id
    else:
        if not isinstance(value, str) or not value:
            raise ValueError("AutoAgent output_dir must be a non-empty string")
        candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError("AutoAgent output_dir must be absolute")
    canonical = os.fspath(candidate.resolve(strict=False))
    if canonical != os.fspath(candidate) or len(canonical) > 4_096:
        raise ValueError("AutoAgent output_dir must be bounded and canonical")
    if "\x00" in canonical:
        raise ValueError("AutoAgent output_dir must not contain NUL")
    return canonical


def _normalize_autoagent_cwd(value: str) -> str:
    bound = _require_bounded_autoagent_text(
        value,
        "cwd",
        minimum=1,
        maximum=4_096,
    )
    candidate = Path(bound)
    if not candidate.is_absolute():
        raise ValueError("AutoAgent cwd must be absolute")
    try:
        canonical = os.fspath(candidate.resolve(strict=True))
        metadata = os.lstat(candidate)
    except OSError as exc:
        raise ValueError("AutoAgent cwd must be an existing directory") from exc
    if canonical != bound or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("AutoAgent cwd must be canonical")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("AutoAgent cwd must be a directory")
    return canonical


def _default_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _new_id() -> str:
    """Return 128 cryptographically random bits as an opaque lowercase ID."""

    return secrets.token_hex(16)


def _validate_terminal_code(
    *,
    terminal_status: str,
    terminal_code: object,
    record_kind: str,
) -> None:
    """Reject caller detail at the authoritative lifecycle boundary."""

    if terminal_status == "succeeded":
        if terminal_code is not None:
            raise ValueError(f"succeeded {record_kind} must not have terminal_code")
        return
    if terminal_code is None:
        return
    if record_kind == "Turn":
        allowed = is_allowed_turn_terminal_code(terminal_status, terminal_code)
    elif record_kind == "Run":
        allowed = is_allowed_run_terminal_code(terminal_status, terminal_code)
    else:  # pragma: no cover - internal programming error
        raise AssertionError(f"unsupported terminal record kind: {record_kind}")
    if not allowed:
        raise ValueError(
            f"{record_kind} terminal_code must be a closed non-secret code "
            f"for status {terminal_status}"
        )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reply_target(value: Mapping[str, Any]) -> tuple[int, str, str]:
    encoded = _canonical_json(value)
    version = int(value.get("schema_version", 1))
    if version < 1:
        raise ValueError("reply_target schema_version must be positive")
    key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return version, key, encoded


def _decode_channel_reply_target(raw: object) -> Mapping[str, Any]:
    try:
        value = json.loads(str(raw))
        required = {
            "kind",
            "adapter",
            "account_namespace",
            "destination_id",
        }
        optional = {"schema_version", "thread_id", "destination_kind"}
        if not isinstance(value, dict) or set(value) - (required | optional):
            raise ValueError
        if not required.issubset(value):
            raise ValueError
        schema_version = value.get("schema_version", 1)
        if type(schema_version) is not int:
            raise ValueError
        if schema_version != 1 or value.get("kind") != "channel":
            raise ValueError
        for field_name in ("adapter", "account_namespace", "destination_id"):
            field = value.get(field_name)
            if not isinstance(field, str) or not field.strip():
                raise ValueError
        for field_name in ("thread_id", "destination_kind"):
            field = value.get(field_name)
            if field is not None and (
                not isinstance(field, str) or not field.strip()
            ):
                raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ControlIntegrityError(
            "resend source Delivery has an invalid Channel Reply Target"
        ) from exc
    return value


def _validate_adapter_accounts(
    value: Sequence[tuple[str, str]] | None,
) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError("adapter_accounts must contain (adapter, account) pairs")
    normalized: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("adapter_accounts must contain (adapter, account) pairs")
        adapter = _require_nonempty(item[0], "adapter")
        account = _require_nonempty(item[1], "account_namespace")
        normalized.add((adapter, account))
    return tuple(sorted(normalized))


def _adapter_account_sql(
    scopes: Sequence[tuple[str, str]],
) -> tuple[str, tuple[str, ...]]:
    if not scopes:
        return "", ()
    clause = " OR ".join(
        "(json_extract(d.reply_target_json, '$.adapter') = ? "
        "AND json_extract(d.reply_target_json, '$.account_namespace') = ?)"
        for _scope in scopes
    )
    parameters = tuple(part for scope in scopes for part in scope)
    return f"AND ({clause})", parameters


def _require_nonempty(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    return normalized


def _require_digest(value: str, name: str) -> str:
    original = str(value)
    normalized = original.lower()
    if (
        original != normalized
        or len(normalized) != 64
        or any(ch not in "0123456789abcdef" for ch in normalized)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return normalized


def _require_proposed_id(value: str, name: str) -> str:
    """Accept only opaque IDs in the same shape as Backend-generated UUID hex."""

    normalized = str(value)
    if len(normalized) != 32 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{name} must be 32 lowercase hexadecimal characters")
    return normalized


def _require_autoagent_session_id(value: str) -> str:
    candidate = str(value)
    if len(candidate) != 32 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise ValueError("session_id must be exactly 32 lowercase hex characters")
    return candidate


def _require_bounded_autoagent_text(
    value: str,
    name: str,
    *,
    minimum: int = 0,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not minimum <= len(value) <= maximum or "\x00" in value:
        raise ValueError(f"{name} must be bounded text")
    return value


def _canonical_autoagent_result(value: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(value, Mapping) or value.get("success") is not True:
        raise ValueError("AutoAgent terminal result requires exact success=true")
    unknown = set(value) - _AUTOAGENT_RESULT_FIELDS
    if unknown:
        raise ValueError("AutoAgent terminal result contains unsupported fields")
    node_count = 0

    def validate(item: Any, *, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > 20_000 or depth > 8:
            raise ValueError("AutoAgent terminal result exceeds structural bounds")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("AutoAgent terminal result must be finite JSON")
            return
        if isinstance(item, str):
            if len(item) > 65_536 or "\x00" in item:
                raise ValueError("AutoAgent terminal result string is not bounded")
            return
        if isinstance(item, Mapping):
            if len(item) > 4_096:
                raise ValueError("AutoAgent terminal result mapping is not bounded")
            for key, child in item.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 256:
                    raise ValueError("AutoAgent terminal result keys must be bounded")
                if _is_credential_shaped_autoagent_key(key):
                    raise ValueError(
                        "AutoAgent terminal result must not contain credentials"
                    )
                validate(child, depth=depth + 1)
            return
        if isinstance(item, (list, tuple)):
            if len(item) > 4_096:
                raise ValueError("AutoAgent terminal result list is not bounded")
            for child in item:
                validate(child, depth=depth + 1)
            return
        raise ValueError("AutoAgent terminal result must be finite JSON")

    validate(value, depth=0)
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("AutoAgent terminal result must be finite JSON") from exc
    payload = encoded.encode("utf-8")
    if not 2 <= len(payload) <= 4_194_304:
        raise ValueError("AutoAgent terminal result exceeds its bounded size")
    return encoded, hashlib.sha256(payload).hexdigest()


def _run_integrity_evidence_sha256(
    *,
    incident_type: RunIntegrityIncidentType,
    evidence_code: RunIntegrityEvidenceCode,
    run_id: str,
    assignment_id: str,
    receipt_revision: int,
    receipt_status: str,
    receipt_terminal_code: RunTerminalCode | None,
    observed_terminal_status: str | None = None,
    observed_terminal_code: RunTerminalCode | None = None,
) -> str:
    """Hash only closed, content-free facts from one authoritative snapshot."""

    if receipt_status not in _RUN_STATUSES:
        raise ControlIntegrityError("Run Receipt has an invalid status")
    if receipt_status in _TERMINAL_RUNS:
        _validate_terminal_code(
            terminal_status=receipt_status,
            terminal_code=receipt_terminal_code,
            record_kind="Run",
        )
    elif receipt_terminal_code is not None:
        raise ControlIntegrityError("nonterminal Run Receipt has a terminal code")
    if observed_terminal_status is not None:
        if observed_terminal_status not in _TERMINAL_RUNS:
            raise ValueError("observed terminal status must be terminal")
        _validate_terminal_code(
            terminal_status=observed_terminal_status,
            terminal_code=observed_terminal_code,
            record_kind="Run",
        )
    elif observed_terminal_code is not None:
        raise ValueError("observed terminal code requires a status")
    payload = {
        "schema_version": 1,
        "incident_type": incident_type.value,
        "evidence_code": evidence_code.value,
        "run_id": run_id,
        "assignment_id": assignment_id,
        "receipt": {
            "revision": receipt_revision,
            "status": receipt_status,
            "terminal_code": receipt_terminal_code,
        },
        "observed_terminal": (
            None
            if observed_terminal_status is None
            else {
                "status": observed_terminal_status,
                "terminal_code": observed_terminal_code,
            }
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class ControlStateRepository:
    """Single-owner, single-process authoritative control-state repository."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        clock_ms: Callable[[], int] = _default_clock_ms,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        configured_root = Path(state_root).expanduser().absolute()
        if configured_root.is_symlink():
            raise ControlIntegrityError("Control state root must not be a symlink")
        configured_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root = configured_root.resolve()
        _require_private_path(self.state_root, directory=True)
        self.database_path = self.state_root / "control.db"
        self.lock_path = self.state_root / "control.lock"
        for protected_path in (self.database_path, self.lock_path):
            if protected_path.is_symlink():
                raise ControlIntegrityError(
                    f"Control state file must not be a symlink: {protected_path}"
                )
            if protected_path.exists():
                _require_private_path(protected_path, directory=False)
        self._clock_ms = clock_ms
        self._fault_hook = fault_hook
        self._mutex = threading.RLock()
        self._closed = False
        self._ownership_lock = ControlDatabaseLock(self.lock_path)
        self._ownership_lock.acquire()
        self._connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                isolation_level=None,
                check_same_thread=False,
                timeout=max(0.001, busy_timeout_ms / 1_000),
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._connection = connection
            apply_migrations(connection, now_ms=self._now())
            self._harden_database_files()
        except sqlite3.DatabaseError as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._ownership_lock.release()
            raise ControlIntegrityError(
                f"Control Database could not be opened safely: {self.database_path}"
            ) from exc
        except Exception:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._ownership_lock.release()
            raise

    def _harden_database_files(self) -> None:
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            if path.exists():
                with suppress(OSError):
                    os.chmod(path, 0o600)

    def _now(self) -> int:
        return int(self._clock_ms())

    @property
    def control_authority_id(self) -> str:
        """Return the opaque, immutable identity of this Control Database."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT control_authority_id FROM control_authority "
                "WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise ControlIntegrityError("Control authority identity is unavailable")
        authority_id = str(row["control_authority_id"])
        if len(authority_id) != 64 or any(
            character not in "0123456789abcdef" for character in authority_id
        ):
            raise ControlIntegrityError("Control authority identity is invalid")
        return authority_id

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise RepositoryClosedError("ControlStateRepository is closed")
        return self._connection

    def _checkpoint(self, name: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(name)

    @contextmanager
    def _transaction(self, name: str) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            connection = self._conn
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                self._checkpoint(f"{name}.before_commit")
                connection.commit()
                self._harden_database_files()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            yield self._conn

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                self._harden_database_files()
                self._connection.close()
                self._connection = None
            self._ownership_lock.release()

    def __enter__(self) -> "ControlStateRepository":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # AutoAgent lifecycle authority
    # ------------------------------------------------------------------

    def accept_autoagent_session(
        self,
        *,
        session_id: str,
        cwd: str,
        output_dir: str | None = None,
        skill: str,
        method: str,
        evolution_goal: str,
        creation_receipt_sha256: str | None,
        execution_reference_type: str | None = None,
        execution_reference: str | None = None,
    ) -> AutoAgentSessionRecord:
        """Atomically accept one immutable AutoAgent start authority."""

        bound_session_id = _require_autoagent_session_id(session_id)
        bound_cwd = _normalize_autoagent_cwd(cwd)
        bound_output_dir = _normalize_autoagent_output_dir(
            output_dir,
            cwd=bound_cwd,
            session_id=bound_session_id,
        )
        bound_skill = _require_bounded_autoagent_text(
            skill, "skill", minimum=1, maximum=256
        )
        bound_method = _require_bounded_autoagent_text(
            method, "method", minimum=1, maximum=256
        )
        bound_goal = _require_bounded_autoagent_text(
            evolution_goal, "evolution_goal", maximum=16_384
        )
        receipt_digest = (
            _require_digest(
                creation_receipt_sha256,
                "creation_receipt_sha256",
            )
            if creation_receipt_sha256 is not None
            else None
        )
        owner_type, owner_reference = _normalize_autoagent_owner(
            execution_reference_type,
            execution_reference,
        )
        with self._transaction("accept_autoagent_session") as connection:
            existing = connection.execute(
                "SELECT 1 FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            if existing is not None:
                raise KeyError(bound_session_id)
            budget = connection.execute(
                "SELECT session_count, result_bytes, cancellation_count "
                "FROM autoagent_capacity "
                "WHERE singleton_id = 1"
            ).fetchone()
            if budget is None:
                raise ControlIntegrityError(
                    "AutoAgent capacity authority is unavailable"
                )
            if int(budget["session_count"]) >= _AUTOAGENT_SESSION_CAPACITY:
                raise AutoAgentCapacityError(
                    "AutoAgent durable session capacity is exhausted"
                )
            preaccept_cancelled = bool(
                receipt_digest is not None
                and connection.execute(
                    "SELECT 1 FROM autoagent_start_cancellations "
                    "WHERE session_id = ? AND creation_receipt_sha256 = ?",
                    (bound_session_id, receipt_digest),
                ).fetchone()
            )
            # BEGIN IMMEDIATE serializes this handoff with receipt cancellation.
            # Before the session row exists, an exact abort needs one tombstone
            # slot. After this transaction commits, the receipt-bound session
            # row itself is the cancellation authority and no tombstone slot is
            # needed. A racing abort therefore either writes first and is found
            # above, or waits and updates the accepted session row.
            if (
                receipt_digest is not None
                and not preaccept_cancelled
                and int(budget["cancellation_count"])
                >= _AUTOAGENT_CANCELLATION_CAPACITY
            ):
                raise AutoAgentCapacityError(
                    "AutoAgent durable cancellation capacity is exhausted"
                )
            if not preaccept_cancelled and owner_reference is None:
                raise ValueError(
                    "AutoAgent running admission requires a governed execution owner"
                )
            if not preaccept_cancelled:
                active_count = connection.execute(
                    "SELECT count(*) FROM autoagent_sessions "
                    "WHERE status = 'running'"
                ).fetchone()
                if (
                    active_count is None
                    or int(active_count[0]) >= _AUTOAGENT_ACTIVE_SESSION_CAPACITY
                ):
                    raise AutoAgentActiveCapacityError(
                        "AutoAgent active session capacity is exhausted"
                    )
            if (
                not preaccept_cancelled
                and int(budget["result_bytes"])
                >= _AUTOAGENT_RESULT_BYTES_CAPACITY
            ):
                raise AutoAgentCapacityError(
                    "AutoAgent durable result capacity is exhausted"
                )
            now = self._now()
            if preaccept_cancelled:
                connection.execute(
                    """
                    INSERT INTO autoagent_sessions (
                        session_id, cwd, output_dir, skill, method, evolution_goal,
                        creation_receipt_sha256, cancel_requested_at_ms,
                        execution_reference_type, execution_reference,
                        owner_stopped_at_ms, owner_stop_evidence,
                        status, result_json, result_sha256, error_code,
                        error_detail, created_at_ms, updated_at_ms,
                        finished_at_ms, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                              'cancelled', NULL, NULL, 'cancelled', ?, ?, ?, ?, 1)
                    """,
                    (
                        bound_session_id,
                        bound_cwd,
                        bound_output_dir,
                        bound_skill,
                        bound_method,
                        bound_goal,
                        receipt_digest,
                        now,
                        _AUTOAGENT_ERROR_DETAILS["cancelled"],
                        now,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO autoagent_sessions (
                        session_id, cwd, output_dir, skill, method, evolution_goal,
                        creation_receipt_sha256, cancel_requested_at_ms,
                        execution_reference_type, execution_reference,
                        owner_stopped_at_ms, owner_stop_evidence,
                        status, result_json, result_sha256, error_code,
                        error_detail, created_at_ms, updated_at_ms,
                        finished_at_ms, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL,
                              'running', NULL, NULL, NULL, NULL, ?, ?, NULL, 1)
                    """,
                    (
                        bound_session_id,
                        bound_cwd,
                        bound_output_dir,
                        bound_skill,
                        bound_method,
                        bound_goal,
                        receipt_digest,
                        owner_type,
                        owner_reference,
                        now,
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            assert row is not None
            return _autoagent_session_record(row)

    def request_autoagent_cancellation(
        self,
        *,
        session_id: str,
        creation_receipt_sha256: str,
    ) -> AutoAgentCancellationResult:
        """Persist one exact receipt-bound pre-accept tombstone or run intent."""

        bound_session_id = _require_autoagent_session_id(session_id)
        receipt_digest = _require_digest(
            creation_receipt_sha256,
            "creation_receipt_sha256",
        )
        with self._transaction("request_autoagent_cancellation") as connection:
            row = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT 1 FROM autoagent_start_cancellations "
                    "WHERE session_id = ? AND creation_receipt_sha256 = ?",
                    (bound_session_id, receipt_digest),
                ).fetchone()
                if existing is None:
                    budget = connection.execute(
                        "SELECT cancellation_count FROM autoagent_capacity "
                        "WHERE singleton_id = 1"
                    ).fetchone()
                    if budget is None:
                        raise ControlIntegrityError(
                            "AutoAgent capacity authority is unavailable"
                        )
                    if (
                        int(budget["cancellation_count"])
                        >= _AUTOAGENT_CANCELLATION_CAPACITY
                    ):
                        raise AutoAgentCapacityError(
                            "AutoAgent durable cancellation capacity is exhausted"
                        )
                    connection.execute(
                        "INSERT INTO autoagent_start_cancellations "
                        "(session_id, creation_receipt_sha256, created_at_ms) "
                        "VALUES (?, ?, ?)",
                        (bound_session_id, receipt_digest, self._now()),
                    )
                return AutoAgentCancellationResult("cancelled", None)

            record = _autoagent_session_record(row)
            if (
                record.creation_receipt_sha256 is None
                or not secrets.compare_digest(
                    record.creation_receipt_sha256,
                    receipt_digest,
                )
            ):
                raise ValueError("AutoAgent creation receipt does not match")
            if record.status != "running":
                return AutoAgentCancellationResult(record.status, record)
            if record.cancel_requested_at_ms is None:
                now = self._now()
                connection.execute(
                    """
                    UPDATE autoagent_sessions
                    SET cancel_requested_at_ms = ?, updated_at_ms = ?,
                        revision = revision + 1
                    WHERE session_id = ? AND status = 'running'
                      AND cancel_requested_at_ms IS NULL
                    """,
                    (now, now, bound_session_id),
                )
                row = connection.execute(
                    "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                    (bound_session_id,),
                ).fetchone()
                assert row is not None
                record = _autoagent_session_record(row)
            return AutoAgentCancellationResult("cancel_requested", record)

    def get_autoagent_session(self, session_id: str) -> AutoAgentSessionRecord:
        bound_session_id = _require_autoagent_session_id(session_id)
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(bound_session_id)
        return _autoagent_session_record(row)

    def verify_autoagent_creation_receipt(
        self,
        session_id: str,
        creation_receipt_sha256: str,
    ) -> AutoAgentSessionRecord:
        """Verify one caller-held receipt digest without exposing it."""

        expected = _require_digest(
            creation_receipt_sha256,
            "creation_receipt_sha256",
        )
        record = self.get_autoagent_session(session_id)
        if (
            record.creation_receipt_sha256 is None
            or not secrets.compare_digest(record.creation_receipt_sha256, expected)
        ):
            raise ValueError("AutoAgent creation receipt does not match")
        return record

    def list_running_autoagent_sessions(self) -> tuple[AutoAgentSessionRecord, ...]:
        """Observe running AutoAgent owners without reconstructing payloads."""

        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE status = 'running' "
                "ORDER BY created_at_ms, session_id"
            ).fetchall()
        return tuple(_autoagent_session_record(row) for row in rows)

    def confirm_autoagent_owner_stopped(
        self,
        session_id: str,
        *,
        evidence_code: str = _AUTOAGENT_OWNER_STOP_EVIDENCE,
    ) -> AutoAgentSessionRecord:
        """Persist exact process-tree absence before any owned terminal state."""

        bound_session_id = _require_autoagent_session_id(session_id)
        if evidence_code != _AUTOAGENT_OWNER_STOP_EVIDENCE:
            raise ValueError("unsupported AutoAgent owner stop evidence")
        with self._transaction("confirm_autoagent_owner_stopped") as connection:
            row = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(bound_session_id)
            record = _autoagent_session_record(row)
            if record.execution_reference is None:
                raise ValueError("AutoAgent session has no governed execution owner")
            if record.status != "running":
                if record.owner_stop_evidence == evidence_code:
                    return record
                raise ValueError("AutoAgent session is already terminal")
            if record.owner_stop_evidence is not None:
                if record.owner_stop_evidence != evidence_code:
                    raise ControlIntegrityError(
                        "AutoAgent owner stop evidence is inconsistent"
                    )
                return record
            now = self._now()
            connection.execute(
                """
                UPDATE autoagent_sessions
                SET owner_stopped_at_ms = ?, owner_stop_evidence = ?,
                    updated_at_ms = ?, revision = revision + 1
                WHERE session_id = ? AND status = 'running'
                  AND owner_stopped_at_ms IS NULL
                  AND owner_stop_evidence IS NULL
                """,
                (now, evidence_code, now, bound_session_id),
            )
            updated = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            assert updated is not None
            return _autoagent_session_record(updated)

    def complete_autoagent_session_success(
        self,
        session_id: str,
        result: Mapping[str, Any],
    ) -> AutoAgentSessionRecord:
        """Commit one validated successful terminal result exactly once."""

        bound_session_id = _require_autoagent_session_id(session_id)
        result_json, result_sha256 = _canonical_autoagent_result(result)
        result_size = len(result_json.encode("utf-8"))
        with self._transaction("complete_autoagent_session_success") as connection:
            current = connection.execute(
                "SELECT status, output_dir, skill, method, evolution_goal, "
                "execution_reference, owner_stop_evidence "
                "FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            if current is None:
                raise KeyError(bound_session_id)
            if str(current["status"]) != "running":
                raise ValueError("AutoAgent session is already terminal")
            if (
                current["execution_reference"] is not None
                and current["owner_stop_evidence"]
                != _AUTOAGENT_OWNER_STOP_EVIDENCE
            ):
                raise ValueError("AutoAgent execution owner is not confirmed stopped")
            expected_identity = {
                "mode": "harness_evolution",
                "skill": str(current["skill"]),
                "method": str(current["method"]),
                "evolution_goal": str(current["evolution_goal"]),
                "output_dir": str(current["output_dir"]),
            }
            for key, expected in expected_identity.items():
                if result.get(key) != expected:
                    raise ValueError(
                        f"AutoAgent terminal result {key} does not match authority"
                    )
            promotion = result.get("promotion")
            if not isinstance(promotion, Mapping) or promotion.get("status") != "skipped":
                raise ValueError(
                    "AutoAgent terminal result requires skipped manual promotion"
                )
            budget = connection.execute(
                "SELECT result_bytes FROM autoagent_capacity "
                "WHERE singleton_id = 1"
            ).fetchone()
            if budget is None:
                raise ControlIntegrityError(
                    "AutoAgent capacity authority is unavailable"
                )
            if (
                int(budget["result_bytes"]) + result_size
                > _AUTOAGENT_RESULT_BYTES_CAPACITY
            ):
                raise AutoAgentCapacityError(
                    "AutoAgent durable result capacity is exhausted"
                )
            now = self._now()
            connection.execute(
                """
                UPDATE autoagent_sessions
                SET status = 'done', result_json = ?, result_sha256 = ?,
                    error_code = NULL, error_detail = NULL,
                    updated_at_ms = ?, finished_at_ms = ?, revision = revision + 1
                WHERE session_id = ? AND status = 'running'
                """,
                (result_json, result_sha256, now, now, bound_session_id),
            )
            row = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            assert row is not None
            return _autoagent_session_record(row)

    def complete_autoagent_session_error(
        self,
        session_id: str,
        *,
        status: str,
        error_code: str,
        error_detail: str,
    ) -> AutoAgentSessionRecord:
        """Commit one closed non-success terminal outcome exactly once."""

        bound_session_id = _require_autoagent_session_id(session_id)
        if status not in {"error", "cancelled", "interrupted"}:
            raise ValueError("unsupported AutoAgent terminal error status")
        if error_code not in _AUTOAGENT_ERROR_CODES:
            raise ValueError("unsupported AutoAgent terminal error code")
        expected_detail = _AUTOAGENT_ERROR_DETAILS[error_code]
        if error_detail != expected_detail:
            raise ValueError("AutoAgent terminal error detail must match its closed code")
        if status == "cancelled" and error_code != "cancelled":
            raise ValueError("cancelled AutoAgent session requires cancelled code")
        if status == "interrupted" and error_code not in {
            "backend_restart_interrupted",
            "backend_shutdown_interrupted",
        }:
            raise ValueError("interrupted AutoAgent session requires interruption code")
        if status == "error" and error_code in {
            "cancelled",
            "backend_restart_interrupted",
            "backend_shutdown_interrupted",
        }:
            raise ValueError("AutoAgent error code does not match terminal status")
        bound_detail = _require_bounded_autoagent_text(
            expected_detail,
            "error_detail",
            minimum=1,
            maximum=512,
        )
        with self._transaction("complete_autoagent_session_error") as connection:
            current = connection.execute(
                "SELECT status, execution_reference, owner_stop_evidence "
                "FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            if current is None:
                raise KeyError(bound_session_id)
            if str(current["status"]) != "running":
                raise ValueError("AutoAgent session is already terminal")
            if (
                current["execution_reference"] is not None
                and current["owner_stop_evidence"]
                != _AUTOAGENT_OWNER_STOP_EVIDENCE
            ):
                raise ValueError("AutoAgent execution owner is not confirmed stopped")
            now = self._now()
            connection.execute(
                """
                UPDATE autoagent_sessions
                SET status = ?, result_json = NULL, result_sha256 = NULL,
                    error_code = ?, error_detail = ?,
                    updated_at_ms = ?, finished_at_ms = ?, revision = revision + 1
                WHERE session_id = ? AND status = 'running'
                """,
                (
                    status,
                    error_code,
                    bound_detail,
                    now,
                    now,
                    bound_session_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM autoagent_sessions WHERE session_id = ?",
                (bound_session_id,),
            ).fetchone()
            assert row is not None
            return _autoagent_session_record(row)

    def reconcile_autoagent_sessions(
        self,
        *,
        error_code: str = "backend_restart_interrupted",
    ) -> AutoAgentStartupReconciliationResult:
        """Close only ownerless/stopped runs; retain unconfirmed owners running."""

        if error_code not in {
            "backend_restart_interrupted",
            "backend_shutdown_interrupted",
        }:
            raise ValueError("unsupported AutoAgent reconciliation code")
        detail = _AUTOAGENT_ERROR_DETAILS[error_code]
        with self._transaction("reconcile_autoagent_sessions") as connection:
            safe_rows = connection.execute(
                """
                SELECT session_id FROM autoagent_sessions
                WHERE status = 'running'
                  AND owner_stop_evidence = 'process_tree_absent_v1'
                ORDER BY created_at_ms, session_id
                """
            ).fetchall()
            unconfirmed_rows = connection.execute(
                """
                SELECT session_id FROM autoagent_sessions
                WHERE status = 'running'
                  AND execution_reference IS NOT NULL
                  AND owner_stop_evidence IS NULL
                ORDER BY created_at_ms, session_id
                """
            ).fetchall()
            session_ids = tuple(str(row["session_id"]) for row in safe_rows)
            unconfirmed_ids = tuple(
                str(row["session_id"]) for row in unconfirmed_rows
            )
            if session_ids:
                now = self._now()
                connection.execute(
                    """
                    UPDATE autoagent_sessions
                    SET status = 'interrupted', error_code = ?, error_detail = ?,
                        updated_at_ms = ?, finished_at_ms = ?, revision = revision + 1
                    WHERE status = 'running'
                      AND (
                      owner_stop_evidence = 'process_tree_absent_v1'
                      )
                    """,
                    (error_code, detail, now, now),
                )
            return AutoAgentStartupReconciliationResult(
                session_ids,
                unconfirmed_ids,
            )

    # ------------------------------------------------------------------
    # Transcript Store identity and legacy cutover
    # ------------------------------------------------------------------

    def bind_transcript_store(
        self,
        transcript_store_id: str,
        *,
        import_run_id: str | None = None,
    ) -> StateChangeResult:
        """Bind this Control Database to exactly one Transcript Store.

        A normal Runtime may initialize the singleton only while Control has no
        conversational authority.  The offline importer may initialize it for
        one planned import after all imported Conversations have explicit
        aliases, but before validation or cutover.
        """

        store_id = _require_proposed_id(transcript_store_id, "transcript_store_id")
        run_id = (
            _require_proposed_id(import_run_id, "import_run_id")
            if import_run_id is not None
            else None
        )
        with self._transaction("bind_transcript_store") as connection:
            existing = connection.execute(
                "SELECT transcript_store_id FROM transcript_store_bindings "
                "WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                if str(existing["transcript_store_id"]) != store_id:
                    raise ControlIntegrityError(
                        "Control Database is bound to a different Transcript Store"
                    )
                return StateChangeResult(False, "bound")

            if run_id is None:
                authority = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM conversations) "
                    "OR EXISTS(SELECT 1 FROM turns) "
                    "OR EXISTS(SELECT 1 FROM legacy_import_runs)"
                ).fetchone()
                if bool(authority[0]):
                    raise ControlIntegrityError(
                        "existing conversational Control state has no Transcript Store binding"
                    )
            else:
                run = connection.execute(
                    "SELECT state FROM legacy_import_runs WHERE import_run_id = ?",
                    (run_id,),
                ).fetchone()
                if run is None or str(run["state"]) != "planned":
                    raise ControlIntegrityError(
                        "Transcript Store can only be bound by a planned legacy import"
                    )
                foreign_run = connection.execute(
                    "SELECT 1 FROM legacy_import_runs "
                    "WHERE import_run_id != ? LIMIT 1",
                    (run_id,),
                ).fetchone()
                existing_turn = connection.execute(
                    "SELECT 1 FROM turns LIMIT 1"
                ).fetchone()
                foreign_conversation = connection.execute(
                    """
                    SELECT 1 FROM conversations AS c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM legacy_identity_map AS m
                        WHERE m.import_run_id = ?
                          AND m.canonical_kind = 'conversation'
                          AND m.canonical_id = c.conversation_id
                          AND m.status = 'mapped'
                    )
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if (
                    foreign_run is not None
                    or existing_turn is not None
                    or foreign_conversation is not None
                ):
                    raise ControlIntegrityError(
                        "legacy import cannot bind a Transcript Store over existing authority"
                    )

            connection.execute(
                "INSERT INTO transcript_store_bindings "
                "(singleton, transcript_store_id, bound_at_ms) VALUES (1, ?, ?)",
                (store_id, self._now()),
            )
            return StateChangeResult(True, "bound")

    def verify_transcript_store_binding(self, transcript_store_id: str) -> None:
        """Fail closed unless Control is bound to this exact Store identity."""

        store_id = _require_proposed_id(transcript_store_id, "transcript_store_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT transcript_store_id FROM transcript_store_bindings "
                "WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ControlIntegrityError(
                "Control Database has no Transcript Store binding"
            )
        if str(row["transcript_store_id"]) != store_id:
            raise ControlIntegrityError(
                "Control Database is bound to a different Transcript Store"
            )

    def get_transcript_store_binding(self) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT transcript_store_id FROM transcript_store_bindings "
                "WHERE singleton = 1"
            ).fetchone()
        return str(row["transcript_store_id"]) if row is not None else None

    # ------------------------------------------------------------------
    # Attachment Store identity
    # ------------------------------------------------------------------

    def bind_attachment_store(self, store_id: str) -> StateChangeResult:
        """Bind this Control Database to exactly one Attachment Store."""

        attachment_store_id = _require_proposed_id(store_id, "store_id")
        with self._transaction("bind_attachment_store") as connection:
            existing = connection.execute(
                "SELECT store_id FROM attachment_store_bindings WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                if str(existing["store_id"]) != attachment_store_id:
                    raise ControlIntegrityError(
                        "Control Database is bound to a different Attachment Store"
                    )
                return StateChangeResult(False, "bound")

            connection.execute(
                "INSERT INTO attachment_store_bindings "
                "(singleton, store_id, bound_at_ms) VALUES (1, ?, ?)",
                (attachment_store_id, self._now()),
            )
            return StateChangeResult(True, "bound")

    def verify_attachment_store_binding(self, store_id: str) -> None:
        """Fail closed unless Control is bound to this Attachment Store."""

        attachment_store_id = _require_proposed_id(store_id, "store_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT store_id FROM attachment_store_bindings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ControlIntegrityError(
                "Control Database has no Attachment Store binding"
            )
        if str(row["store_id"]) != attachment_store_id:
            raise ControlIntegrityError(
                "Control Database is bound to a different Attachment Store"
            )

    def get_attachment_store_binding(self) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT store_id FROM attachment_store_bindings WHERE singleton = 1"
            ).fetchone()
        return str(row["store_id"]) if row is not None else None

    def begin_legacy_import(
        self,
        import_run_id: str,
        *,
        source_manifest_sha256: str,
        report_ref: str,
    ) -> StateChangeResult:
        """Persist the recoverable pre-cutover marker for one offline import."""

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        manifest = _require_digest(
            source_manifest_sha256,
            "source_manifest_sha256",
        )
        report = _require_nonempty(report_ref, "report_ref")
        with self._transaction("begin_legacy_import") as connection:
            committed = connection.execute(
                "SELECT source_manifest_sha256 FROM legacy_import_runs "
                "WHERE state = 'committed' LIMIT 1"
            ).fetchone()
            if committed is not None and str(committed[0]) != manifest:
                raise ControlIntegrityError(
                    "a different legacy import already owns the cutover marker"
                )
            existing = connection.execute(
                "SELECT * FROM legacy_import_runs WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["source_manifest_sha256"]) != manifest
                    or str(existing["report_ref"]) != report
                ):
                    raise ControlIntegrityError(
                        "legacy import identity was reused with different evidence"
                    )
                return StateChangeResult(False, str(existing["state"]))
            connection.execute(
                """
                INSERT INTO legacy_import_runs (
                    import_run_id, source_manifest_sha256, state, started_at_ms,
                    finished_at_ms, cutover_at_ms, report_ref
                ) VALUES (?, ?, 'planned', ?, NULL, NULL, ?)
                """,
                (run_id, manifest, self._now(), report),
            )
            return StateChangeResult(True, "planned")

    def commit_legacy_import_cutover(self, import_run_id: str) -> StateChangeResult:
        """Atomically make a previously verified canonical import authoritative."""

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        with self._transaction("commit_legacy_import_cutover") as connection:
            row = connection.execute(
                "SELECT state FROM legacy_import_runs WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "import_not_found")
            if str(row["state"]) == "committed":
                return StateChangeResult(False, "committed")
            if str(row["state"]) != "validated":
                raise ControlIntegrityError("legacy import is not cutover-ready")
            identity = connection.execute(
                "SELECT 1 FROM legacy_transcript_cutovers WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if identity is None:
                raise ControlIntegrityError(
                    "legacy import has no immutable Transcript cutover identity"
                )
            now = self._now()
            connection.execute(
                """
                UPDATE legacy_import_runs
                SET state = 'committed', finished_at_ms = ?, cutover_at_ms = ?
                WHERE import_run_id = ?
                """,
                (now, now, run_id),
            )
            return StateChangeResult(True, "committed")

    def mark_legacy_import_validated(self, import_run_id: str) -> StateChangeResult:
        """Record that all staged canonical evidence passed offline verification."""

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        with self._transaction("validate_legacy_import") as connection:
            row = connection.execute(
                "SELECT state FROM legacy_import_runs WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "import_not_found")
            state = str(row["state"])
            if state in {"validated", "committed"}:
                return StateChangeResult(False, state)
            if state != "planned":
                raise ControlIntegrityError("legacy import cannot become validated")
            identity = connection.execute(
                "SELECT 1 FROM legacy_transcript_cutovers WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if identity is None:
                raise ControlIntegrityError(
                    "legacy import has no immutable Transcript cutover identity"
                )
            connection.execute(
                "UPDATE legacy_import_runs SET state = 'validated' "
                "WHERE import_run_id = ? AND state = 'planned'",
                (run_id,),
            )
            return StateChangeResult(True, "validated")

    def get_legacy_import_state(self, import_run_id: str) -> str | None:
        run_id = _require_proposed_id(import_run_id, "import_run_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT state FROM legacy_import_runs WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
        return str(row["state"]) if row is not None else None

    def list_legacy_import_states(self) -> tuple[str, ...]:
        """Return persisted cutover states for the startup authority barrier."""

        with self._read() as connection:
            rows = connection.execute(
                "SELECT state FROM legacy_import_runs ORDER BY import_run_id"
            ).fetchall()
        return tuple(str(row["state"]) for row in rows)

    def record_legacy_transcript_cutover(
        self,
        import_run_id: str,
        *,
        cutover_manifest_sha256: str,
        transcript_store_id: str,
        import_baseline_sha256: str,
        source_identity: str,
    ) -> StateChangeResult:
        """Bind one planned import to its immutable Transcript Store evidence."""

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        cutover_manifest = _require_digest(
            cutover_manifest_sha256,
            "cutover_manifest_sha256",
        )
        store_id = _require_proposed_id(transcript_store_id, "transcript_store_id")
        baseline = _require_digest(
            import_baseline_sha256,
            "import_baseline_sha256",
        )
        source = _require_nonempty(source_identity, "source_identity")
        with self._transaction("record_legacy_transcript_cutover") as connection:
            run = connection.execute(
                "SELECT state, source_manifest_sha256 FROM legacy_import_runs "
                "WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                return StateChangeResult(False, "import_not_found")
            if str(run["source_manifest_sha256"]) != cutover_manifest:
                raise ControlIntegrityError(
                    "Transcript cutover manifest does not match its Control import"
                )
            store_binding = connection.execute(
                "SELECT transcript_store_id FROM transcript_store_bindings "
                "WHERE singleton = 1"
            ).fetchone()
            if (
                store_binding is None
                or str(store_binding["transcript_store_id"]) != store_id
            ):
                raise ControlIntegrityError(
                    "legacy cutover does not match the bound Transcript Store"
                )
            existing = connection.execute(
                "SELECT * FROM legacy_transcript_cutovers WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["cutover_manifest_sha256"]) != cutover_manifest
                    or str(existing["transcript_store_id"]) != store_id
                    or str(existing["import_baseline_sha256"]) != baseline
                    or str(existing["source_identity"]) != source
                ):
                    raise ControlIntegrityError(
                        "legacy Transcript cutover identity was reused with different evidence"
                    )
                return StateChangeResult(False, str(run["state"]))
            if str(run["state"]) != "planned":
                raise ControlIntegrityError(
                    "legacy Transcript cutover identity can only be recorded while planned"
                )
            connection.execute(
                """
                INSERT INTO legacy_transcript_cutovers (
                    import_run_id, cutover_manifest_sha256, transcript_store_id,
                    import_baseline_sha256, source_identity, recorded_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, cutover_manifest, store_id, baseline, source, self._now()),
            )
            return StateChangeResult(True, "recorded")

    def get_legacy_transcript_cutover(
        self,
        import_run_id: str,
    ) -> dict[str, str] | None:
        """Return immutable cross-store cutover evidence for one import."""

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM legacy_transcript_cutovers WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "import_run_id": str(row["import_run_id"]),
            "cutover_manifest_sha256": str(row["cutover_manifest_sha256"]),
            "transcript_store_id": str(row["transcript_store_id"]),
            "import_baseline_sha256": str(row["import_baseline_sha256"]),
            "source_identity": str(row["source_identity"]),
        }

    def list_committed_legacy_transcript_cutovers(
        self,
    ) -> tuple[dict[str, str], ...]:
        """Return committed cutovers for the Runtime cross-store barrier."""

        with self._read() as connection:
            missing = connection.execute(
                """
                SELECT r.import_run_id
                FROM legacy_import_runs AS r
                LEFT JOIN legacy_transcript_cutovers AS c USING (import_run_id)
                WHERE r.state = 'committed' AND c.import_run_id IS NULL
                ORDER BY r.import_run_id
                LIMIT 1
                """
            ).fetchone()
            if missing is not None:
                raise ControlIntegrityError(
                    "committed legacy import has no immutable Transcript cutover identity"
                )
            rows = connection.execute(
                """
                SELECT c.*
                FROM legacy_transcript_cutovers AS c
                JOIN legacy_import_runs AS r USING (import_run_id)
                WHERE r.state = 'committed'
                ORDER BY c.import_run_id
                """
            ).fetchall()
        return tuple(
            {
                "import_run_id": str(row["import_run_id"]),
                "cutover_manifest_sha256": str(row["cutover_manifest_sha256"]),
                "transcript_store_id": str(row["transcript_store_id"]),
                "import_baseline_sha256": str(row["import_baseline_sha256"]),
                "source_identity": str(row["source_identity"]),
            }
            for row in rows
        )

    def import_legacy_conversations(
        self,
        import_run_id: str,
        mappings: Sequence[Mapping[str, Any]],
    ) -> StateChangeResult:
        """Create explicit legacy aliases and their canonical Conversations.

        The offline importer supplies a frozen, Owner-reviewed mapping plan.
        This command never derives identity from timestamps, display names or
        Transcript content.
        """

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        prepared: list[tuple[str, str, str, int, str, str, bool, str]] = []
        active_addresses: set[tuple[str, str]] = set()
        for raw in mappings:
            legacy_key = _require_nonempty(str(raw.get("legacy_key", "")), "legacy_key")
            conversation_id = _require_proposed_id(
                str(raw.get("conversation_id", "")),
                "conversation_id",
            )
            surface = _require_nonempty(str(raw.get("surface", "")), "surface")
            if surface not in _SURFACES:
                raise ValueError("legacy Conversation surface is unsupported")
            reply_target = raw.get("reply_target")
            if not isinstance(reply_target, Mapping):
                raise ValueError("legacy Conversation reply_target must be a mapping")
            target_version, target_key, target_json = _reply_target(reply_target)
            active = bool(raw.get("active", False))
            address = (surface, target_key)
            if active and address in active_addresses:
                raise ControlIntegrityError(
                    "multiple imported Conversations claim one active ReplyTarget"
                )
            if active:
                active_addresses.add(address)
            evidence = raw.get("evidence", {})
            if not isinstance(evidence, Mapping):
                raise ValueError("legacy mapping evidence must be a mapping")
            evidence_json = _canonical_json(evidence)
            prepared.append(
                (
                    legacy_key,
                    conversation_id,
                    surface,
                    target_version,
                    target_key,
                    target_json,
                    active,
                    evidence_json,
                )
            )

        with self._transaction("import_legacy_conversations") as connection:
            run = connection.execute(
                "SELECT state FROM legacy_import_runs WHERE import_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                return StateChangeResult(False, "import_not_found")
            if str(run["state"]) != "planned":
                raise ControlIntegrityError("legacy import is not mappable")
            now = self._now()
            changed = False
            for (
                legacy_key,
                conversation_id,
                surface,
                target_version,
                target_key,
                target_json,
                active,
                evidence_json,
            ) in prepared:
                existing_conversation = connection.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if existing_conversation is None:
                    connection.execute(
                        """
                        INSERT INTO conversations (
                            conversation_id, surface, reply_target_version,
                            reply_target_key, reply_target_json, project_id,
                            revision, created_at_ms, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)
                        """,
                        (
                            conversation_id,
                            surface,
                            target_version,
                            target_key,
                            target_json,
                            now,
                            now,
                        ),
                    )
                    changed = True
                elif (
                    str(existing_conversation["surface"]) != surface
                    or int(existing_conversation["reply_target_version"])
                    != target_version
                    or str(existing_conversation["reply_target_key"]) != target_key
                    or str(existing_conversation["reply_target_json"]) != target_json
                    or existing_conversation["project_id"] is not None
                ):
                    raise ControlIntegrityError(
                        "legacy mapping collides with a different Conversation"
                    )

                existing_alias = connection.execute(
                    """
                    SELECT * FROM legacy_identity_map
                    WHERE source_system = 'legacy_transcript_db'
                      AND legacy_kind = 'chat_key' AND legacy_key = ?
                    """,
                    (legacy_key,),
                ).fetchone()
                if existing_alias is None:
                    connection.execute(
                        """
                        INSERT INTO legacy_identity_map (
                            import_run_id, source_system, legacy_kind, legacy_key,
                            canonical_kind, canonical_id, evidence_json, status
                        ) VALUES (?, 'legacy_transcript_db', 'chat_key', ?,
                                  'conversation', ?, ?, 'mapped')
                        """,
                        (run_id, legacy_key, conversation_id, evidence_json),
                    )
                    changed = True
                elif (
                    str(existing_alias["import_run_id"]) != run_id
                    or str(existing_alias["canonical_id"]) != conversation_id
                    or str(existing_alias["evidence_json"]) != evidence_json
                    or str(existing_alias["status"]) != "mapped"
                ):
                    raise ControlIntegrityError(
                        "legacy chat identity already has different mapping evidence"
                    )

                if active:
                    current = connection.execute(
                        "SELECT conversation_id FROM active_conversation_bindings "
                        "WHERE surface = ? AND reply_target_key = ?",
                        (surface, target_key),
                    ).fetchone()
                    if (
                        current is None
                        or str(current["conversation_id"]) != conversation_id
                    ):
                        self._activate_conversation(
                            connection,
                            surface=surface,
                            target_version=target_version,
                            target_key=target_key,
                            target_json=target_json,
                            conversation_id=conversation_id,
                            now=now,
                        )
                        changed = True
            return StateChangeResult(changed, "mapped" if changed else "unchanged")

    def verify_legacy_conversations(
        self,
        import_run_id: str,
        mappings: Sequence[Mapping[str, Any]],
        *,
        require_active_binding: bool = True,
    ) -> None:
        """Read-only M7 verification for imported Conversation identities."""

        run_id = _require_proposed_id(import_run_id, "import_run_id")
        with self._read() as connection:
            for raw in mappings:
                legacy_key = _require_nonempty(
                    str(raw.get("legacy_key", "")),
                    "legacy_key",
                )
                conversation_id = _require_proposed_id(
                    str(raw.get("conversation_id", "")),
                    "conversation_id",
                )
                surface = _require_nonempty(
                    str(raw.get("surface", "")),
                    "surface",
                )
                reply_target = raw.get("reply_target")
                if not isinstance(reply_target, Mapping):
                    raise ValueError(
                        "legacy Conversation reply_target must be a mapping"
                    )
                target_version, target_key, target_json = _reply_target(reply_target)
                evidence = raw.get("evidence", {})
                if not isinstance(evidence, Mapping):
                    raise ValueError("legacy mapping evidence must be a mapping")
                evidence_json = _canonical_json(evidence)

                conversation = connection.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                alias = connection.execute(
                    """
                    SELECT * FROM legacy_identity_map
                    WHERE source_system = 'legacy_transcript_db'
                      AND legacy_kind = 'chat_key' AND legacy_key = ?
                    """,
                    (legacy_key,),
                ).fetchone()
                if (
                    conversation is None
                    or str(conversation["surface"]) != surface
                    or int(conversation["reply_target_version"]) != target_version
                    or str(conversation["reply_target_key"]) != target_key
                    or str(conversation["reply_target_json"]) != target_json
                    or alias is None
                    or str(alias["import_run_id"]) != run_id
                    or str(alias["canonical_id"]) != conversation_id
                    or str(alias["evidence_json"]) != evidence_json
                    or str(alias["status"]) != "mapped"
                ):
                    raise ControlIntegrityError(
                        f"legacy Conversation mapping drift for {legacy_key}"
                    )
                if require_active_binding and bool(raw.get("active", False)):
                    binding = connection.execute(
                        "SELECT conversation_id FROM active_conversation_bindings "
                        "WHERE surface = ? AND reply_target_key = ?",
                        (surface, target_key),
                    ).fetchone()
                    if (
                        binding is None
                        or str(binding["conversation_id"]) != conversation_id
                    ):
                        raise ControlIntegrityError(
                            f"legacy active binding drift for {legacy_key}"
                        )

    def has_conversational_state(self) -> bool:
        """Whether losing ``transcripts.db`` could discard accepted authority."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM conversations) "
                "OR EXISTS(SELECT 1 FROM turns) "
                "OR EXISTS(SELECT 1 FROM legacy_import_runs) "
                "OR EXISTS(SELECT 1 FROM transcript_store_bindings)"
            ).fetchone()
        return bool(row[0])

    def create_project(self, display_name: str) -> ProjectRecord:
        name = _require_nonempty(display_name, "display_name")
        project_id = _new_id()
        now = self._now()
        with self._transaction("create_project") as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, lifecycle, revision,
                    created_at_ms, updated_at_ms, lifecycle_at_ms
                ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                """,
                (project_id, name, now, now, now),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return _project_record(row)

    def archive_project(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> ProjectLifecycleResult:
        return self._change_project_lifecycle(
            project_id,
            target="archived",
            expected_revision=expected_revision,
        )

    def restore_project(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> ProjectLifecycleResult:
        return self._change_project_lifecycle(
            project_id,
            target="active",
            expected_revision=expected_revision,
        )

    def _change_project_lifecycle(
        self,
        project_id: str,
        *,
        target: str,
        expected_revision: int | None,
    ) -> ProjectLifecycleResult:
        with self._transaction(f"project_{target}") as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return ProjectLifecycleResult(ProjectLifecycleStatus.NOT_FOUND)
            current = _project_record(row)
            if expected_revision is not None and current.revision != expected_revision:
                return ProjectLifecycleResult(
                    ProjectLifecycleStatus.REVISION_CONFLICT,
                    current,
                    "revision_conflict",
                )
            if current.lifecycle == target:
                return ProjectLifecycleResult(
                    ProjectLifecycleStatus.UNCHANGED,
                    current,
                )
            if target == "archived" and self._project_has_nonterminal_work(
                connection, project_id
            ):
                return ProjectLifecycleResult(
                    ProjectLifecycleStatus.BUSY,
                    current,
                    "project_busy",
                )
            now = self._now()
            connection.execute(
                """
                UPDATE projects
                SET lifecycle = ?, revision = revision + 1,
                    updated_at_ms = ?, lifecycle_at_ms = ?
                WHERE project_id = ?
                """,
                (target, now, now, project_id),
            )
            changed = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            assert changed is not None
            return ProjectLifecycleResult(
                ProjectLifecycleStatus.CHANGED,
                _project_record(changed),
            )

    @staticmethod
    def _project_has_nonterminal_work(
        connection: sqlite3.Connection, project_id: str
    ) -> bool:
        turn = connection.execute(
            """
            SELECT 1
            FROM turns AS t
            JOIN conversations AS c USING (conversation_id)
            WHERE c.project_id = ? AND t.status IN ('queued', 'running')
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if turn is not None:
            return True
        run = connection.execute(
            """
            SELECT 1 FROM runs
            WHERE project_id = ?
              AND status IN ('queued', 'running', 'cancel_requested')
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        return run is not None

    # ------------------------------------------------------------------
    # Conversational admission and lifecycle
    # ------------------------------------------------------------------

    def lookup_ingress_turn_id(
        self,
        *,
        surface: str,
        source_namespace: str,
        source_request_id: str,
    ) -> str | None:
        """Return the Turn already bound to one ingress key, if any."""

        if surface not in _SURFACES:
            raise ValueError(f"unsupported Surface: {surface}")
        _require_nonempty(source_namespace, "source_namespace")
        _require_nonempty(source_request_id, "source_request_id")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT turn_id
                FROM ingress_bindings
                WHERE surface = ? AND source_namespace = ? AND source_request_id = ?
                """,
                (surface, source_namespace, source_request_id),
            ).fetchone()
        return str(row["turn_id"]) if row is not None else None

    def inspect_ingress(
        self,
        *,
        surface: str,
        source_namespace: str,
        source_request_id: str,
        fingerprint_version: int,
        fingerprint_sha256: str,
    ) -> IdempotencyInspection:
        if surface not in _SURFACES:
            raise ValueError(f"unsupported Surface: {surface}")
        if fingerprint_version < 1:
            raise ValueError("fingerprint_version must be positive")
        _require_nonempty(source_namespace, "source_namespace")
        _require_nonempty(source_request_id, "source_request_id")
        _require_digest(fingerprint_sha256, "fingerprint_sha256")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT fingerprint_version, fingerprint_sha256, turn_id
                FROM ingress_bindings
                WHERE surface = ? AND source_namespace = ? AND source_request_id = ?
                """,
                (surface, source_namespace, source_request_id),
            ).fetchone()
        if row is None:
            return IdempotencyInspection("novel")
        if (
            int(row["fingerprint_version"]) == fingerprint_version
            and str(row["fingerprint_sha256"]) == fingerprint_sha256
        ):
            return IdempotencyInspection("duplicate", str(row["turn_id"]))
        return IdempotencyInspection(
            "conflict",
            str(row["turn_id"]),
            "idempotency_conflict",
        )

    def plan_turn_acceptance(
        self,
        intent: TurnAcceptanceIntent,
        *,
        proposed_turn_id: str | None = None,
        proposed_conversation_id: str | None = None,
    ) -> TurnAcceptancePlan:
        """Resolve a read-only identity plan; it grants no durable authority."""

        _validate_turn_intent(intent)
        _, target_key, _ = _reply_target(intent.reply_target)
        turn_proposal = (
            _new_id()
            if proposed_turn_id is None
            else _require_proposed_id(proposed_turn_id, "proposed_turn_id")
        )
        conversation_proposal = (
            _new_id()
            if proposed_conversation_id is None
            else _require_proposed_id(
                proposed_conversation_id, "proposed_conversation_id"
            )
        )
        with self._read() as connection:
            duplicate = connection.execute(
                """
                SELECT b.fingerprint_version, b.fingerprint_sha256, b.turn_id,
                       t.conversation_id, c.project_id
                FROM ingress_bindings AS b
                JOIN turns AS t USING (turn_id)
                JOIN conversations AS c USING (conversation_id)
                WHERE b.surface = ? AND b.source_namespace = ?
                  AND b.source_request_id = ?
                """,
                (intent.surface, intent.source_namespace, intent.source_request_id),
            ).fetchone()
            if duplicate is not None:
                matching = (
                    int(duplicate["fingerprint_version"]) == intent.fingerprint_version
                    and str(duplicate["fingerprint_sha256"])
                    == intent.fingerprint_sha256
                )
                return TurnAcceptancePlan(
                    state="duplicate" if matching else "conflict",
                    turn_id=str(duplicate["turn_id"]),
                    conversation_id=str(duplicate["conversation_id"]),
                    project_id=(
                        str(duplicate["project_id"])
                        if duplicate["project_id"] is not None
                        else None
                    ),
                    code="" if matching else "idempotency_conflict",
                )

            row: sqlite3.Row | None = None
            if intent.explicit_conversation_id is not None:
                row = connection.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?",
                    (intent.explicit_conversation_id,),
                ).fetchone()
                if row is None:
                    return TurnAcceptancePlan(
                        state="rejected", code="conversation_not_found"
                    )
                if (
                    row["surface"] != intent.surface
                    or row["reply_target_key"] != target_key
                ):
                    return TurnAcceptancePlan(
                        state="rejected",
                        conversation_id=str(row["conversation_id"]),
                        code="conversation_address_mismatch",
                    )
            elif not intent.new_conversation:
                row = connection.execute(
                    """
                    SELECT c.*
                    FROM active_conversation_bindings AS b
                    JOIN conversations AS c USING (conversation_id)
                    WHERE b.surface = ? AND b.reply_target_key = ?
                    """,
                    (intent.surface, target_key),
                ).fetchone()

            if row is None:
                if intent.project_id is not None and not self._project_is_active(
                    connection, intent.project_id
                ):
                    return TurnAcceptancePlan(
                        state="rejected",
                        code=self._project_rejection_code(
                            connection, intent.project_id
                        ),
                    )
                return TurnAcceptancePlan(
                    state="novel",
                    turn_id=turn_proposal,
                    conversation_id=conversation_proposal,
                    project_id=intent.project_id,
                    proposed_turn_id=turn_proposal,
                    proposed_conversation_id=conversation_proposal,
                )

            conversation_id = str(row["conversation_id"])
            current_project = str(row["project_id"]) if row["project_id"] else None
            requested_project = intent.project_id
            if requested_project is None or requested_project == current_project:
                return TurnAcceptancePlan(
                    state="novel",
                    turn_id=turn_proposal,
                    conversation_id=conversation_id,
                    project_id=current_project,
                    proposed_turn_id=turn_proposal,
                )
            if not self._project_is_active(connection, requested_project):
                return TurnAcceptancePlan(
                    state="rejected",
                    conversation_id=conversation_id,
                    code=self._project_rejection_code(connection, requested_project),
                )
            if current_project is None:
                return TurnAcceptancePlan(
                    state="novel",
                    turn_id=turn_proposal,
                    conversation_id=conversation_id,
                    project_id=requested_project,
                    proposed_turn_id=turn_proposal,
                )
            return TurnAcceptancePlan(
                state="novel",
                turn_id=turn_proposal,
                conversation_id=conversation_proposal,
                project_id=requested_project,
                proposed_turn_id=turn_proposal,
                proposed_conversation_id=conversation_proposal,
            )

    def accept_turn(
        self,
        intent: TurnAcceptanceIntent,
        *,
        proposed_turn_id: str | None = None,
        proposed_conversation_id: str | None = None,
        expected_conversation_id: str | None = None,
        attachment_commitment: AttachmentBatchCommitment | None = None,
    ) -> TurnAcceptanceResult:
        _validate_turn_intent(intent)
        if proposed_turn_id is not None:
            proposed_turn_id = _require_proposed_id(
                proposed_turn_id, "proposed_turn_id"
            )
        if proposed_conversation_id is not None:
            proposed_conversation_id = _require_proposed_id(
                proposed_conversation_id, "proposed_conversation_id"
            )
        if expected_conversation_id is not None:
            expected_conversation_id = _require_proposed_id(
                expected_conversation_id, "expected_conversation_id"
            )
        target_version, target_key, target_json = _reply_target(intent.reply_target)
        with self._transaction("accept_turn") as connection:
            duplicate = connection.execute(
                """
                SELECT fingerprint_version, fingerprint_sha256, turn_id
                FROM ingress_bindings
                WHERE surface = ? AND source_namespace = ? AND source_request_id = ?
                """,
                (intent.surface, intent.source_namespace, intent.source_request_id),
            ).fetchone()
            if duplicate is not None:
                turn = connection.execute(
                    "SELECT conversation_id FROM turns WHERE turn_id = ?",
                    (duplicate["turn_id"],),
                ).fetchone()
                if (
                    int(duplicate["fingerprint_version"]) == intent.fingerprint_version
                    and str(duplicate["fingerprint_sha256"])
                    == intent.fingerprint_sha256
                ):
                    if turn is None:
                        raise ControlIntegrityError(
                            "Ingress Binding references a missing Turn"
                        )
                    return TurnAcceptanceResult(
                        TurnAcceptanceStatus.DUPLICATE,
                        str(duplicate["turn_id"]),
                        str(turn["conversation_id"]),
                    )
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.CONFLICT,
                    str(duplicate["turn_id"]),
                    str(turn["conversation_id"]) if turn else "",
                    "idempotency_conflict",
                )

            # A caller-proposed Turn ID is an internal reservation token.  Its
            # collision must fail before Conversation creation or Project
            # binding can make any durable mutation in this transaction.
            turn_id = proposed_turn_id or _new_id()
            if (
                connection.execute(
                    "SELECT 1 FROM turns WHERE turn_id = ?", (turn_id,)
                ).fetchone()
                is not None
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.CONFLICT, code="proposed_turn_id_conflict"
                )

            conversation_result = self._resolve_conversation(
                connection,
                intent,
                target_version=target_version,
                target_key=target_key,
                target_json=target_json,
                proposed_conversation_id=proposed_conversation_id,
                expected_conversation_id=expected_conversation_id,
            )
            if isinstance(conversation_result, TurnAcceptanceResult):
                return conversation_result
            conversation_id, project_id = conversation_result
            if project_id is not None and not self._project_is_active(
                connection, project_id
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    conversation_id=conversation_id,
                    code="project_archived",
                )

            now = self._now()
            connection.execute(
                """
                INSERT INTO turns (
                    turn_id, conversation_id, turn_kind, status,
                    retry_of_turn_id, terminal_code, created_at_ms,
                    started_at_ms, finished_at_ms, revision
                ) VALUES (?, ?, ?, 'queued', ?, NULL, ?, NULL, NULL, 1)
                """,
                (
                    turn_id,
                    conversation_id,
                    intent.turn_kind,
                    intent.retry_of_turn_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO ingress_bindings (
                    surface, source_namespace, source_request_id,
                    fingerprint_version, fingerprint_sha256, turn_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.surface,
                    intent.source_namespace,
                    intent.source_request_id,
                    intent.fingerprint_version,
                    intent.fingerprint_sha256,
                    turn_id,
                    now,
                ),
            )
            if attachment_commitment is not None:
                self._insert_turn_attachment_commitment(
                    connection,
                    attachment_commitment,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    now=now,
                )
            self._activate_conversation(
                connection,
                surface=intent.surface,
                target_version=target_version,
                target_key=target_key,
                target_json=target_json,
                conversation_id=conversation_id,
                now=now,
            )
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.ACCEPTED,
                turn_id,
                conversation_id,
            )

    @staticmethod
    def _insert_turn_attachment_commitment(
        connection: sqlite3.Connection,
        commitment: AttachmentBatchCommitment,
        *,
        turn_id: str,
        conversation_id: str,
        now: int,
    ) -> None:
        if not isinstance(commitment, AttachmentBatchCommitment):
            raise TypeError(
                "attachment_commitment must be an AttachmentBatchCommitment"
            )
        if commitment.schema_version != 1:
            raise ValueError("attachment commitment schema_version must be 1")
        store_id = _require_proposed_id(commitment.store_id, "commitment.store_id")
        batch_id = _require_proposed_id(commitment.batch_id, "commitment.batch_id")
        committed_turn_id = _require_proposed_id(
            commitment.turn_id, "commitment.turn_id"
        )
        committed_conversation_id = _require_proposed_id(
            commitment.conversation_id,
            "commitment.conversation_id",
        )
        manifest_sha256 = _require_digest(
            commitment.records_sha256,
            "commitment.records_sha256",
        )
        attachment_count = commitment.record_count
        if (
            not isinstance(attachment_count, int)
            or isinstance(attachment_count, bool)
            or attachment_count <= 0
            or attachment_count > 0xFFFFFFFF
        ):
            raise ValueError(
                "commitment.record_count must be a positive unsigned integer"
            )
        if committed_turn_id != turn_id:
            raise ValueError(
                "Attachment commitment Turn ID does not match accepted Turn"
            )
        if committed_conversation_id != conversation_id:
            raise ValueError(
                "Attachment commitment Conversation ID does not match accepted Conversation"
            )

        binding = connection.execute(
            "SELECT store_id FROM attachment_store_bindings WHERE singleton = 1"
        ).fetchone()
        if binding is None:
            raise ControlIntegrityError(
                "Control Database has no Attachment Store binding"
            )
        if str(binding["store_id"]) != store_id:
            raise ControlIntegrityError(
                "Control Database is bound to a different Attachment Store"
            )
        existing_batch = connection.execute(
            "SELECT turn_id FROM turn_attachment_commitments WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if existing_batch is not None:
            raise ControlIntegrityError(
                "Attachment batch is already committed to a different Turn"
            )

        connection.execute(
            """
            INSERT INTO turn_attachment_commitments (
                turn_id, attachment_store_id, batch_id, manifest_sha256,
                attachment_count, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                store_id,
                batch_id,
                manifest_sha256,
                attachment_count,
                now,
            ),
        )

    def _resolve_conversation(
        self,
        connection: sqlite3.Connection,
        intent: TurnAcceptanceIntent,
        *,
        target_version: int,
        target_key: str,
        target_json: str,
        proposed_conversation_id: str | None,
        expected_conversation_id: str | None,
    ) -> tuple[str, str | None] | TurnAcceptanceResult:
        row: sqlite3.Row | None = None
        if intent.explicit_conversation_id is not None:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (intent.explicit_conversation_id,),
            ).fetchone()
            if row is None:
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    code="conversation_not_found",
                )
            if (
                row["surface"] != intent.surface
                or row["reply_target_key"] != target_key
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    conversation_id=str(row["conversation_id"]),
                    code="conversation_address_mismatch",
                )
        elif not intent.new_conversation:
            row = connection.execute(
                """
                SELECT c.*
                FROM active_conversation_bindings AS b
                JOIN conversations AS c USING (conversation_id)
                WHERE b.surface = ? AND b.reply_target_key = ?
                """,
                (intent.surface, target_key),
            ).fetchone()

        now = self._now()
        if row is None:
            if intent.project_id is not None and not self._project_is_active(
                connection, intent.project_id
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    code=self._project_rejection_code(connection, intent.project_id),
                )
            conversation_id = proposed_conversation_id or _new_id()
            if (
                expected_conversation_id is not None
                and conversation_id != expected_conversation_id
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    code="acceptance_plan_stale",
                )
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                is not None
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.CONFLICT,
                    code="proposed_conversation_id_conflict",
                )
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, surface, reply_target_version,
                    reply_target_key, reply_target_json, project_id,
                    revision, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    conversation_id,
                    intent.surface,
                    target_version,
                    target_key,
                    target_json,
                    intent.project_id,
                    now,
                    now,
                ),
            )
            return conversation_id, intent.project_id

        conversation_id = str(row["conversation_id"])
        current_project = row["project_id"]
        requested_project = intent.project_id
        if requested_project is None or requested_project == current_project:
            if (
                expected_conversation_id is not None
                and conversation_id != expected_conversation_id
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    conversation_id=conversation_id,
                    code="acceptance_plan_stale",
                )
            return conversation_id, str(current_project) if current_project else None
        if not self._project_is_active(connection, requested_project):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                conversation_id=conversation_id,
                code=self._project_rejection_code(connection, requested_project),
            )
        if current_project is None:
            if (
                expected_conversation_id is not None
                and conversation_id != expected_conversation_id
            ):
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    conversation_id=conversation_id,
                    code="acceptance_plan_stale",
                )
            connection.execute(
                """
                UPDATE conversations
                SET project_id = ?, revision = revision + 1, updated_at_ms = ?
                WHERE conversation_id = ? AND project_id IS NULL
                """,
                (requested_project, now, conversation_id),
            )
            return conversation_id, requested_project

        replacement_id = proposed_conversation_id or _new_id()
        if (
            expected_conversation_id is not None
            and replacement_id != expected_conversation_id
        ):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                conversation_id=conversation_id,
                code="acceptance_plan_stale",
            )
        if (
            connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (replacement_id,),
            ).fetchone()
            is not None
        ):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.CONFLICT,
                code="proposed_conversation_id_conflict",
            )
        connection.execute(
            """
            INSERT INTO conversations (
                conversation_id, surface, reply_target_version,
                reply_target_key, reply_target_json, project_id,
                revision, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                replacement_id,
                intent.surface,
                target_version,
                target_key,
                target_json,
                requested_project,
                now,
                now,
            ),
        )
        return replacement_id, requested_project

    @staticmethod
    def _activate_conversation(
        connection: sqlite3.Connection,
        *,
        surface: str,
        target_version: int,
        target_key: str,
        target_json: str,
        conversation_id: str,
        now: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO active_conversation_bindings (
                surface, reply_target_key, reply_target_version,
                reply_target_json, conversation_id, revision, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(surface, reply_target_key) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                revision = active_conversation_bindings.revision + 1,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                surface,
                target_key,
                target_version,
                target_json,
                conversation_id,
                now,
            ),
        )

    @staticmethod
    def _project_is_active(connection: sqlite3.Connection, project_id: str) -> bool:
        row = connection.execute(
            "SELECT lifecycle FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return row is not None and row["lifecycle"] == "active"

    @staticmethod
    def _project_rejection_code(connection: sqlite3.Connection, project_id: str) -> str:
        row = connection.execute(
            "SELECT lifecycle FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return "project_not_found" if row is None else "project_archived"

    def start_turn(self, turn_id: str) -> StateChangeResult:
        with self._transaction("start_turn") as connection:
            row = connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "turn_not_found")
            if row["status"] == "running":
                return StateChangeResult(False, "already_running")
            if row["status"] != "queued":
                return StateChangeResult(False, "turn_terminal")
            now = self._now()
            connection.execute(
                """
                UPDATE turns
                SET status = 'running', started_at_ms = ?, revision = revision + 1
                WHERE turn_id = ? AND status = 'queued'
                """,
                (now, turn_id),
            )
            return StateChangeResult(True, "started")

    def list_nonterminal_turns(self) -> tuple[TurnRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE status IN ('queued','running') "
                "ORDER BY created_at_ms, rowid"
            ).fetchall()
        return tuple(_turn_record(row) for row in rows)

    def list_terminal_turns(self) -> tuple[TurnRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM turns "
                "WHERE status IN ('succeeded','failed','canceled','interrupted') "
                "ORDER BY turn_id"
            ).fetchall()
        return tuple(_turn_record(row) for row in rows)

    def reconcile_nonterminal_turns(
        self,
        *,
        transcript_refs: Mapping[str, TurnTranscriptRef] | None = None,
        delivery_plans: Mapping[str, DeliveryPlan] | None = None,
    ) -> TurnStartupReconciliationResult:
        """Interrupt queued/running receipts without rebuilding execution.

        Channel receipts require a frozen terminal Delivery plan so the same
        transaction that records interruption also creates their canonical
        Outbox fact.  Callers may never terminalize a Channel receipt while
        omitting that plan.
        """

        with self._transaction("reconcile_nonterminal_turns") as connection:
            rows = connection.execute(
                """
                SELECT t.*, c.surface, c.reply_target_version,
                       c.reply_target_key, c.reply_target_json, c.project_id
                FROM turns AS t
                JOIN conversations AS c USING (conversation_id)
                WHERE t.status IN ('queued', 'running')
                ORDER BY t.created_at_ms, t.rowid
                """
            ).fetchall()
            channel_turn_ids = tuple(
                str(row["turn_id"]) for row in rows if str(row["surface"]) == "channel"
            )
            turn_ids = tuple(str(row["turn_id"]) for row in rows)
            if transcript_refs is not None:
                if set(transcript_refs) != set(turn_ids):
                    raise ValueError(
                        "startup Transcript refs must cover every nonterminal Turn"
                    )
                for turn_id, transcript_ref in transcript_refs.items():
                    if not isinstance(transcript_ref, TurnTranscriptRef):
                        raise TypeError(
                            "startup transcript refs must be TurnTranscriptRef values"
                        )
                    _require_proposed_id(
                        transcript_ref.entry_id,
                        "transcript entry_id",
                    )
                    _require_digest(
                        transcript_ref.content_sha256,
                        "transcript content_sha256",
                    )
            plans = dict(delivery_plans or {})
            if set(plans) != set(channel_turn_ids):
                raise ControlIntegrityError(
                    "startup Delivery plans must cover exactly every nonterminal "
                    "Channel Turn"
                )
            if plans and transcript_refs is None:
                raise ControlIntegrityError(
                    "startup Delivery plans require Transcript references"
                )
            if turn_ids:
                now = self._now()
                for row in rows:
                    turn_id = str(row["turn_id"])
                    if transcript_refs is not None:
                        transcript_ref = transcript_refs[turn_id]
                        connection.execute(
                            """
                            INSERT INTO turn_terminal_refs (
                                turn_id, entry_id, content_sha256, created_at_ms
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                turn_id,
                                transcript_ref.entry_id,
                                transcript_ref.content_sha256,
                                now,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE turns
                        SET status = 'interrupted',
                            terminal_code = 'control_plane_restarted',
                            finished_at_ms = ?,
                            revision = revision + 1
                        WHERE turn_id = ? AND status IN ('queued', 'running')
                        """,
                        (now, turn_id),
                    )
                    if str(row["surface"]) == "channel":
                        assert transcript_refs is not None
                        _validate_terminal_delivery_reference(
                            plans[turn_id],
                            transcript_refs[turn_id],
                        )
                        self._insert_terminal_delivery(
                            connection,
                            row=row,
                            terminal_status="interrupted",
                            plan=plans[turn_id],
                            now=now,
                        )
            return TurnStartupReconciliationResult(turn_ids)

    def terminalize_turn(
        self,
        turn_id: str,
        *,
        terminal_status: str,
        terminal_code: TurnTerminalCode | None = None,
        transcript_ref: TurnTranscriptRef | None = None,
        delivery_plan: DeliveryPlan | None = None,
        projections: Sequence[ProjectionIntentInput] = (),
    ) -> TerminalizeTurnResult:
        if terminal_status not in _TERMINAL_TURNS:
            raise ValueError(f"invalid terminal Turn status: {terminal_status}")
        _validate_terminal_code(
            terminal_status=terminal_status,
            terminal_code=terminal_code,
            record_kind="Turn",
        )
        if transcript_ref is not None:
            if not isinstance(transcript_ref, TurnTranscriptRef):
                raise TypeError("transcript_ref must be a TurnTranscriptRef")
            _require_proposed_id(transcript_ref.entry_id, "transcript entry_id")
            _require_digest(
                transcript_ref.content_sha256,
                "transcript content_sha256",
            )
        with self._transaction("terminalize_turn") as connection:
            row = connection.execute(
                """
                SELECT t.*, c.surface, c.reply_target_version,
                       c.reply_target_key, c.reply_target_json, c.project_id
                FROM turns AS t
                JOIN conversations AS c USING (conversation_id)
                WHERE t.turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
            if row is None:
                return TerminalizeTurnResult(False, "turn_not_found")
            if row["status"] in _TERMINAL_TURNS:
                delivery = self._find_terminal_delivery(connection, turn_id)
                return TerminalizeTurnResult(False, "already_terminal", delivery)
            if terminal_status == "succeeded" and row["status"] != "running":
                return TerminalizeTurnResult(False, "turn_not_running")
            if row["surface"] == "channel" and delivery_plan is None:
                raise ValueError("Channel terminalization requires a Delivery plan")
            if row["surface"] == "channel":
                if transcript_ref is None:
                    raise ValueError(
                        "Channel terminalization requires a Transcript reference"
                    )
                assert delivery_plan is not None
                _validate_terminal_delivery_reference(
                    delivery_plan,
                    transcript_ref,
                )
            if row["surface"] != "channel" and delivery_plan is not None:
                raise ValueError("Only Channel Turns may create an Outbound Delivery")
            project_id = str(row["project_id"]) if row["project_id"] else None
            if projections and project_id is None:
                raise ValueError(
                    "Project-scoped projections require a Project-bound Turn"
                )
            if project_id is not None and projections:
                if not self._project_is_active(connection, project_id):
                    raise ControlIntegrityError(
                        "nonterminal Project-bound Turn exists under archived Project"
                    )
                self._insert_projection_intents(
                    connection,
                    project_id=project_id,
                    origin_kind="turn",
                    origin_id=turn_id,
                    projections=projections,
                )

            now = self._now()
            if transcript_ref is not None:
                connection.execute(
                    """
                    INSERT INTO turn_terminal_refs (
                        turn_id, entry_id, content_sha256, created_at_ms
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        transcript_ref.entry_id,
                        transcript_ref.content_sha256,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE turns
                SET status = ?, terminal_code = ?, finished_at_ms = ?,
                    revision = revision + 1
                WHERE turn_id = ?
                """,
                (terminal_status, terminal_code, now, turn_id),
            )
            delivery = None
            if delivery_plan is not None:
                delivery = self._insert_terminal_delivery(
                    connection,
                    row=row,
                    terminal_status=terminal_status,
                    plan=delivery_plan,
                    now=now,
                )
            return TerminalizeTurnResult(True, "terminalized", delivery)

    def get_turn(self, turn_id: str) -> TurnRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return _turn_record(row)

    def get_turn_observation(self, turn_id: str) -> TurnObservationRecord:
        """Read Receipt, Conversation Project and terminal ref in one snapshot."""

        opaque_turn_id = _require_proposed_id(turn_id, "turn_id")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT
                    t.*,
                    c.project_id AS observation_project_id,
                    r.entry_id AS terminal_entry_id,
                    r.content_sha256 AS terminal_content_sha256
                FROM turns AS t
                JOIN conversations AS c USING (conversation_id)
                LEFT JOIN turn_terminal_refs AS r USING (turn_id)
                WHERE t.turn_id = ?
                """,
                (opaque_turn_id,),
            ).fetchone()
        if row is None:
            raise KeyError(opaque_turn_id)
        transcript_ref = (
            TurnTranscriptRef(
                entry_id=str(row["terminal_entry_id"]),
                content_sha256=str(row["terminal_content_sha256"]),
            )
            if row["terminal_entry_id"] is not None
            else None
        )
        return TurnObservationRecord(
            receipt=_turn_record(row),
            project_id=(
                str(row["observation_project_id"])
                if row["observation_project_id"] is not None
                else None
            ),
            transcript_ref=transcript_ref,
        )

    def get_turn_attachment_commitment(
        self, turn_id: str
    ) -> AttachmentBatchCommitment | None:
        """Return the content-free Attachment commitment for one Turn."""

        opaque_turn_id = _require_proposed_id(turn_id, "turn_id")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT a.*, t.conversation_id
                FROM turn_attachment_commitments AS a
                JOIN turns AS t USING (turn_id)
                WHERE a.turn_id = ?
                """,
                (opaque_turn_id,),
            ).fetchone()
        return _attachment_batch_commitment(row) if row is not None else None

    def list_turn_attachment_commitments(
        self,
        *,
        conversation_id: str | None = None,
    ) -> tuple[AttachmentBatchCommitment, ...]:
        """List content-free commitments, optionally for one Conversation."""

        query = (
            "SELECT a.*, t.conversation_id "
            "FROM turn_attachment_commitments AS a "
            "JOIN turns AS t USING (turn_id)"
        )
        parameters: tuple[str, ...] = ()
        if conversation_id is not None:
            opaque_conversation_id = _require_proposed_id(
                conversation_id, "conversation_id"
            )
            query += " WHERE t.conversation_id = ?"
            parameters = (opaque_conversation_id,)
        query += " ORDER BY a.created_at_ms, a.turn_id"
        with self._read() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_attachment_batch_commitment(row) for row in rows)

    def get_turn_terminal_ref(self, turn_id: str) -> TurnTranscriptRef | None:
        """Return the immutable Transcript reference of a terminal Receipt."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT entry_id, content_sha256 FROM turn_terminal_refs "
                "WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return TurnTranscriptRef(
            entry_id=str(row["entry_id"]),
            content_sha256=str(row["content_sha256"]),
        )

    def list_conversations(self) -> tuple[ConversationRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations ORDER BY created_at_ms, conversation_id"
            ).fetchall()
        return tuple(_conversation_record(row) for row in rows)

    def get_conversation(self, conversation_id: str) -> ConversationRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return _conversation_record(row)

    # ------------------------------------------------------------------
    # Run admission, Assignment fencing, and terminal reports
    # ------------------------------------------------------------------

    def _append_run_integrity_incident(
        self,
        connection: sqlite3.Connection,
        *,
        run: sqlite3.Row,
        assignment_id: str,
        incident_type: RunIntegrityIncidentType,
        evidence_code: RunIntegrityEvidenceCode,
        observed_terminal_status: str | None = None,
        observed_terminal_code: RunTerminalCode | None = None,
    ) -> RunIntegrityIncidentAppendResult:
        """Insert one incident using only facts already proven in this transaction."""

        run_id = _require_proposed_id(str(run["run_id"]), "run_id")
        opaque_assignment_id = _require_proposed_id(assignment_id, "assignment_id")
        receipt_revision = int(run["revision"])
        receipt_status = str(run["status"])
        raw_terminal_code = run["terminal_code"]
        receipt_terminal_code = (
            str(raw_terminal_code) if raw_terminal_code is not None else None
        )
        evidence_sha256 = _run_integrity_evidence_sha256(
            incident_type=incident_type,
            evidence_code=evidence_code,
            run_id=run_id,
            assignment_id=opaque_assignment_id,
            receipt_revision=receipt_revision,
            receipt_status=receipt_status,
            receipt_terminal_code=receipt_terminal_code,
            observed_terminal_status=observed_terminal_status,
            observed_terminal_code=observed_terminal_code,
        )
        existing = connection.execute(
            """
            SELECT * FROM run_integrity_incidents
            WHERE run_id = ? AND evidence_sha256 = ?
            """,
            (run_id, evidence_sha256),
        ).fetchone()
        if existing is not None:
            return RunIntegrityIncidentAppendResult(
                False, _run_integrity_incident_record(existing)
            )

        incident_id = _new_id()
        connection.execute(
            """
            INSERT INTO run_integrity_incidents (
                incident_id, run_id, assignment_id, incident_type,
                evidence_code, receipt_revision, evidence_schema_version,
                evidence_sha256, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                incident_id,
                run_id,
                opaque_assignment_id,
                incident_type.value,
                evidence_code.value,
                receipt_revision,
                evidence_sha256,
                self._now(),
            ),
        )
        inserted = connection.execute(
            "SELECT * FROM run_integrity_incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        if inserted is None:  # pragma: no cover - SQLite contract failure
            raise ControlIntegrityError("Run integrity incident disappeared")
        return RunIntegrityIncidentAppendResult(
            True, _run_integrity_incident_record(inserted)
        )

    def record_run_integrity_incident(
        self,
        intent: RunIntegrityIncidentIntent,
    ) -> RunIntegrityIncidentAppendResult:
        """Append one trusted cross-Store/runtime incident idempotently."""

        if not isinstance(intent, RunIntegrityIncidentIntent):
            raise TypeError("intent must be RunIntegrityIncidentIntent")
        run_id = _require_proposed_id(intent.run_id, "run_id")
        assignment_id = _require_proposed_id(intent.assignment_id, "assignment_id")
        with self._transaction("record_run_integrity_incident") as connection:
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            assignment = connection.execute(
                """
                SELECT assignment_id FROM run_execution_assignments
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if assignment is None or assignment["assignment_id"] != assignment_id:
                raise ValueError(
                    "runtime incident assignment must match canonical Assignment"
                )
            return self._append_run_integrity_incident(
                connection,
                run=run,
                assignment_id=assignment_id,
                incident_type=intent.incident_type,
                evidence_code=intent.evidence_code,
            )

    def list_run_integrity_incidents(
        self,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunIntegrityIncidentPage:
        """Return a bounded, newest-first keyset page without side effects."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("incident page limit must be between 1 and 100")
        opaque_run_id = (
            _require_proposed_id(run_id, "run_id") if run_id is not None else None
        )
        opaque_cursor = (
            _require_proposed_id(cursor, "cursor") if cursor is not None else None
        )
        with self._read() as connection:
            before_sequence: int | None = None
            if opaque_cursor is not None:
                anchor = connection.execute(
                    """
                    SELECT incident_sequence, run_id
                    FROM run_integrity_incidents WHERE incident_id = ?
                    """,
                    (opaque_cursor,),
                ).fetchone()
                if anchor is None or (
                    opaque_run_id is not None and anchor["run_id"] != opaque_run_id
                ):
                    raise ValueError("invalid incident cursor")
                before_sequence = int(anchor["incident_sequence"])

            conditions: list[str] = []
            parameters: list[object] = []
            if opaque_run_id is not None:
                conditions.append("run_id = ?")
                parameters.append(opaque_run_id)
            if before_sequence is not None:
                conditions.append("incident_sequence < ?")
                parameters.append(before_sequence)
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            rows = connection.execute(
                "SELECT * FROM run_integrity_incidents"
                + where
                + " ORDER BY incident_sequence DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        incidents = tuple(_run_integrity_incident_record(row) for row in visible)
        next_cursor = incidents[-1].incident_id if has_more and incidents else None
        return RunIntegrityIncidentPage(incidents, next_cursor)

    def inspect_run_submission(
        self,
        *,
        run_submission_id: str,
        fingerprint_version: int,
        fingerprint_sha256: str,
    ) -> IdempotencyInspection:
        _require_nonempty(run_submission_id, "run_submission_id")
        if fingerprint_version < 1:
            raise ValueError("fingerprint_version must be positive")
        _require_digest(fingerprint_sha256, "fingerprint_sha256")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT fingerprint_version, fingerprint_sha256, run_id
                FROM run_submission_bindings WHERE run_submission_id = ?
                """,
                (run_submission_id,),
            ).fetchone()
        if row is None:
            return IdempotencyInspection("novel")
        if (
            int(row["fingerprint_version"]) == fingerprint_version
            and str(row["fingerprint_sha256"]) == fingerprint_sha256
        ):
            return IdempotencyInspection("duplicate", str(row["run_id"]))
        return IdempotencyInspection(
            "conflict",
            str(row["run_id"]),
            "run_idempotency_conflict",
        )

    def plan_run_acceptance(
        self,
        intent: RunAcceptanceIntent,
        *,
        proposed_run_id: str | None = None,
    ) -> RunAcceptancePlan:
        """Resolve a read-only canonical Run identity and admission view."""

        _validate_run_intent(intent)
        run_proposal = (
            _new_id()
            if proposed_run_id is None
            else _require_proposed_id(proposed_run_id, "proposed_run_id")
        )
        with self._read() as connection:
            duplicate = connection.execute(
                """
                SELECT fingerprint_version, fingerprint_sha256, run_id
                FROM run_submission_bindings WHERE run_submission_id = ?
                """,
                (intent.run_submission_id,),
            ).fetchone()
            if duplicate is not None:
                matching = (
                    int(duplicate["fingerprint_version"]) == intent.fingerprint_version
                    and str(duplicate["fingerprint_sha256"])
                    == intent.fingerprint_sha256
                )
                return RunAcceptancePlan(
                    state="duplicate" if matching else "conflict",
                    run_id=str(duplicate["run_id"]),
                    code="" if matching else "run_idempotency_conflict",
                )

            collision = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_proposal,)
            ).fetchone()
            if collision is not None:
                return RunAcceptancePlan(
                    state="conflict",
                    code="proposed_run_id_conflict",
                )
            if intent.scope_kind == "project":
                assert intent.project_id is not None
                if not self._project_is_active(connection, intent.project_id):
                    return RunAcceptancePlan(
                        state="rejected",
                        code=self._project_rejection_code(
                            connection, intent.project_id
                        ),
                    )
            if intent.parent_turn_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM turns WHERE turn_id = ?", (intent.parent_turn_id,)
                ).fetchone()
                if parent is None:
                    return RunAcceptancePlan(
                        state="rejected",
                        code="parent_turn_not_found",
                    )
            retry_rejection = self._validate_run_retry(connection, intent)
            if retry_rejection:
                return RunAcceptancePlan(
                    state="rejected",
                    code=retry_rejection,
                )
            return RunAcceptancePlan(
                state="novel",
                run_id=run_proposal,
                proposed_run_id=run_proposal,
            )

    def accept_run(
        self,
        intent: RunAcceptanceIntent,
        *,
        proposed_run_id: str | None = None,
    ) -> RunAcceptanceResult:
        _validate_run_intent(intent)
        if proposed_run_id is not None:
            proposed_run_id = _require_proposed_id(proposed_run_id, "proposed_run_id")
        with self._transaction("accept_run") as connection:
            duplicate = connection.execute(
                """
                SELECT fingerprint_version, fingerprint_sha256, run_id
                FROM run_submission_bindings WHERE run_submission_id = ?
                """,
                (intent.run_submission_id,),
            ).fetchone()
            if duplicate is not None:
                if (
                    int(duplicate["fingerprint_version"]) == intent.fingerprint_version
                    and str(duplicate["fingerprint_sha256"])
                    == intent.fingerprint_sha256
                ):
                    return RunAcceptanceResult(
                        RunAcceptanceStatus.DUPLICATE,
                        str(duplicate["run_id"]),
                    )
                return RunAcceptanceResult(
                    RunAcceptanceStatus.CONFLICT,
                    str(duplicate["run_id"]),
                    "run_idempotency_conflict",
                )

            run_id = proposed_run_id or _new_id()
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                is not None
            ):
                return RunAcceptanceResult(
                    RunAcceptanceStatus.CONFLICT,
                    code="proposed_run_id_conflict",
                )

            if intent.scope_kind == "project":
                assert intent.project_id is not None
                if not self._project_is_active(connection, intent.project_id):
                    return RunAcceptanceResult(
                        RunAcceptanceStatus.REJECTED,
                        code=self._project_rejection_code(
                            connection, intent.project_id
                        ),
                    )
            if intent.parent_turn_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM turns WHERE turn_id = ?", (intent.parent_turn_id,)
                ).fetchone()
                if parent is None:
                    return RunAcceptanceResult(
                        RunAcceptanceStatus.REJECTED,
                        code="parent_turn_not_found",
                    )
            retry_rejection = self._validate_run_retry(connection, intent)
            if retry_rejection:
                return RunAcceptanceResult(
                    RunAcceptanceStatus.REJECTED,
                    code=retry_rejection,
                )

            now = self._now()
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, scope_kind, project_id, run_kind, parent_turn_id,
                    retry_of_run_id, status, terminal_code, manifest_ref,
                    created_at_ms, started_at_ms, finished_at_ms, revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?, NULL, NULL, 1)
                """,
                (
                    run_id,
                    intent.scope_kind,
                    intent.project_id,
                    intent.run_kind,
                    intent.parent_turn_id,
                    intent.retry_of_run_id,
                    intent.manifest_ref,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_submission_bindings (
                    run_submission_id, fingerprint_version,
                    fingerprint_sha256, run_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    intent.run_submission_id,
                    intent.fingerprint_version,
                    intent.fingerprint_sha256,
                    run_id,
                    now,
                ),
            )
            return RunAcceptanceResult(RunAcceptanceStatus.ACCEPTED, run_id)

    @staticmethod
    def _validate_run_retry(
        connection: sqlite3.Connection, intent: RunAcceptanceIntent
    ) -> str:
        if intent.retry_of_run_id is None:
            return ""
        prior = connection.execute(
            "SELECT scope_kind, project_id, status FROM runs WHERE run_id = ?",
            (intent.retry_of_run_id,),
        ).fetchone()
        if prior is None:
            return "retry_run_not_found"
        if prior["status"] not in _TERMINAL_RUNS:
            return "retry_run_not_terminal"
        if (
            prior["scope_kind"] != intent.scope_kind
            or prior["project_id"] != intent.project_id
        ):
            return "retry_scope_mismatch"
        return ""

    def assign_run(
        self,
        run_id: str,
        *,
        executor_kind: str,
        execution_reference_type: str | None = None,
        execution_reference: str | None = None,
    ) -> AssignmentResult:
        executor = _require_nonempty(executor_kind, "executor_kind")
        if (execution_reference_type is None) != (execution_reference is None):
            raise ValueError("execution reference type and value must appear together")
        with self._transaction("assign_run") as connection:
            existing = connection.execute(
                "SELECT assignment_id FROM run_execution_assignments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                return AssignmentResult(
                    AssignmentStatus.ALREADY_ASSIGNED,
                    str(existing["assignment_id"]),
                )
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return AssignmentResult(
                    AssignmentStatus.NOT_FOUND, code="run_not_found"
                )
            if run["status"] != "queued":
                return AssignmentResult(
                    AssignmentStatus.STATE_CONFLICT,
                    code="run_not_queued",
                )
            assignment_id = _new_id()
            now = self._now()
            connection.execute(
                """
                INSERT INTO run_execution_assignments (
                    run_id, assignment_id, executor_kind,
                    execution_reference_type, execution_reference, assigned_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    assignment_id,
                    executor,
                    execution_reference_type,
                    execution_reference,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'running', started_at_ms = ?, revision = revision + 1
                WHERE run_id = ? AND status = 'queued'
                """,
                (now, run_id),
            )
            return AssignmentResult(AssignmentStatus.ASSIGNED, assignment_id)

    def update_execution_reference(
        self,
        run_id: str,
        assignment_id: str,
        *,
        reference_type: str,
        reference: str,
    ) -> StateChangeResult:
        opaque_run_id = _require_proposed_id(run_id, "run_id")
        opaque_assignment_id = _require_proposed_id(assignment_id, "assignment_id")
        reference_kind = _require_nonempty(reference_type, "reference_type")
        reference_value = _require_nonempty(reference, "reference")
        with self._transaction("update_execution_reference") as connection:
            row = connection.execute(
                """
                SELECT a.assignment_id, a.execution_reference_type,
                       a.execution_reference, r.run_id, r.status,
                       r.terminal_code, r.revision
                FROM runs AS r
                LEFT JOIN run_execution_assignments AS a USING (run_id)
                WHERE r.run_id = ?
                """,
                (opaque_run_id,),
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "assignment_not_found")
            if row["assignment_id"] is None:
                self._append_run_integrity_incident(
                    connection,
                    run=row,
                    assignment_id=opaque_assignment_id,
                    incident_type=RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION,
                    evidence_code=RunIntegrityEvidenceCode.ASSIGNMENT_MISSING,
                )
                return StateChangeResult(False, "assignment_not_found")
            if row["assignment_id"] != opaque_assignment_id:
                self._append_run_integrity_incident(
                    connection,
                    run=row,
                    assignment_id=opaque_assignment_id,
                    incident_type=RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION,
                    evidence_code=RunIntegrityEvidenceCode.ASSIGNMENT_ID_MISMATCH,
                )
                return StateChangeResult(False, "assignment_mismatch")
            if row["status"] in _TERMINAL_RUNS:
                return StateChangeResult(False, "run_terminal")
            if (
                row["execution_reference_type"] == reference_kind
                and row["execution_reference"] == reference_value
            ):
                return StateChangeResult(False, "unchanged")
            connection.execute(
                """
                UPDATE run_execution_assignments
                SET execution_reference_type = ?, execution_reference = ?
                WHERE run_id = ? AND assignment_id = ?
                """,
                (
                    reference_kind,
                    reference_value,
                    opaque_run_id,
                    opaque_assignment_id,
                ),
            )
            return StateChangeResult(True, "updated")

    def fail_queued_run(
        self,
        run_id: str,
        *,
        terminal_code: RunTerminalCode = "submission_failed",
    ) -> StateChangeResult:
        """Close an accepted Run whose process-local submission did not enqueue."""

        _validate_terminal_code(
            terminal_status="failed",
            terminal_code=terminal_code,
            record_kind="Run",
        )
        with self._transaction("fail_queued_run") as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "run_not_found")
            if row["status"] in _TERMINAL_RUNS:
                return StateChangeResult(False, "already_terminal")
            if row["status"] != "queued":
                return StateChangeResult(False, "run_not_queued")
            now = self._now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', terminal_code = ?, finished_at_ms = ?,
                    revision = revision + 1
                WHERE run_id = ? AND status = 'queued'
                """,
                (terminal_code, now, run_id),
            )
            return StateChangeResult(True, "failed")

    def request_run_cancel(self, run_id: str) -> StateChangeResult:
        with self._transaction("request_run_cancel") as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "run_not_found")
            status = str(row["status"])
            if status == "cancel_requested":
                return StateChangeResult(False, "already_cancel_requested")
            if status in _TERMINAL_RUNS:
                return StateChangeResult(False, "run_terminal")
            now = self._now()
            if status == "queued":
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'canceled', terminal_code = 'canceled_before_assignment',
                        finished_at_ms = ?, revision = revision + 1
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (now, run_id),
                )
                return StateChangeResult(True, "canceled")
            connection.execute(
                """
                UPDATE runs SET status = 'cancel_requested', revision = revision + 1
                WHERE run_id = ? AND status = 'running'
                """,
                (run_id,),
            )
            return StateChangeResult(True, "cancel_requested")

    def apply_run_report(self, report: RunReport) -> StateChangeResult:
        if report.terminal_status not in _TERMINAL_RUNS:
            raise ValueError(f"invalid terminal Run status: {report.terminal_status}")
        run_id = _require_proposed_id(report.run_id, "run_id")
        assignment_id = _require_proposed_id(report.assignment_id, "assignment_id")
        _validate_terminal_code(
            terminal_status=report.terminal_status,
            terminal_code=report.terminal_code,
            record_kind="Run",
        )
        conflict_incident_id: str | None = None
        with self._transaction("apply_run_report") as connection:
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return StateChangeResult(False, "run_not_found")
            assignment = connection.execute(
                """
                SELECT assignment_id FROM run_execution_assignments WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if assignment is None:
                self._append_run_integrity_incident(
                    connection,
                    run=run,
                    assignment_id=assignment_id,
                    incident_type=RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION,
                    evidence_code=RunIntegrityEvidenceCode.ASSIGNMENT_MISSING,
                    observed_terminal_status=report.terminal_status,
                    observed_terminal_code=report.terminal_code,
                )
                return StateChangeResult(False, "assignment_mismatch")
            if assignment["assignment_id"] != assignment_id:
                self._append_run_integrity_incident(
                    connection,
                    run=run,
                    assignment_id=assignment_id,
                    incident_type=RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION,
                    evidence_code=RunIntegrityEvidenceCode.ASSIGNMENT_ID_MISMATCH,
                    observed_terminal_status=report.terminal_status,
                    observed_terminal_code=report.terminal_code,
                )
                return StateChangeResult(False, "assignment_mismatch")
            if run["status"] in _TERMINAL_RUNS:
                if (
                    str(run["status"]) == report.terminal_status
                    and run["terminal_code"] == report.terminal_code
                ):
                    return StateChangeResult(False, "already_terminal")
                conflict = self._append_run_integrity_incident(
                    connection,
                    run=run,
                    assignment_id=assignment_id,
                    incident_type=RunIntegrityIncidentType.TERMINAL_REPORT_CONFLICT,
                    evidence_code=RunIntegrityEvidenceCode.TERMINAL_STATE_CONFLICT,
                    observed_terminal_status=report.terminal_status,
                    observed_terminal_code=report.terminal_code,
                )
                conflict_incident_id = conflict.incident.incident_id
            else:
                if run["status"] not in {"running", "cancel_requested"}:
                    return StateChangeResult(False, "run_not_running")
                project_id = str(run["project_id"]) if run["project_id"] else None
                if report.projections and project_id is None:
                    raise ValueError("Project-scoped projections require a Project Run")
                if project_id is not None and report.projections:
                    if not self._project_is_active(connection, project_id):
                        raise ControlIntegrityError(
                            "nonterminal Project Run exists under archived Project"
                        )
                    self._insert_projection_intents(
                        connection,
                        project_id=project_id,
                        origin_kind="run",
                        origin_id=run_id,
                        projections=report.projections,
                    )
                now = self._now()
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, terminal_code = ?, finished_at_ms = ?,
                        revision = revision + 1
                    WHERE run_id = ?
                    """,
                    (
                        report.terminal_status,
                        report.terminal_code,
                        now,
                        run_id,
                    ),
                )
                return StateChangeResult(True, "terminalized")
        assert conflict_incident_id is not None
        raise RunIntegrityIncidentError(
            "conflicting terminal report for one Execution Assignment; "
            f"incident_id={conflict_incident_id}",
            incident_id=conflict_incident_id,
        )

    def get_run(self, run_id: str) -> RunRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_record(row)

    def get_run_observation(self, run_id: str) -> RunObservationSnapshot:
        """Read one Run Receipt and its optional Assignment in one snapshot."""

        opaque_run_id = _require_proposed_id(run_id, "run_id")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT r.*,
                       a.assignment_id AS observation_assignment_id,
                       a.executor_kind AS observation_executor_kind,
                       a.execution_reference_type AS observation_reference_type,
                       a.execution_reference AS observation_reference,
                       a.assigned_at_ms AS observation_assigned_at_ms
                FROM runs AS r
                LEFT JOIN run_execution_assignments AS a USING (run_id)
                WHERE r.run_id = ?
                """,
                (opaque_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(opaque_run_id)
        return _run_observation_snapshot(row)

    def list_run_observations(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunObservationPage:
        """Return canonical Unassigned Simple Skill Runs without side effects.

        The opaque cursor is the final Run ID from the preceding page.  Its
        durable ``(created_at_ms, run_id)`` key is resolved in the same read
        snapshot, so equal timestamps remain deterministic and concurrent
        inserts can never duplicate an older row into the next page.
        """

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Run observation page limit must be between 1 and 100")
        normalized_status = None
        if status is not None:
            normalized_status = str(status)
            if normalized_status not in _RUN_STATUSES:
                raise ValueError("unsupported Run observation status")
        opaque_cursor = (
            _require_proposed_id(cursor, "cursor") if cursor is not None else None
        )

        with self._read() as connection:
            cursor_created_at: int | None = None
            if opaque_cursor is not None:
                anchor = connection.execute(
                    """
                    SELECT created_at_ms, status FROM runs
                    WHERE run_id = ?
                      AND scope_kind = 'unassigned'
                      AND run_kind = 'skill'
                    """,
                    (opaque_cursor,),
                ).fetchone()
                if anchor is None or (
                    normalized_status is not None
                    and str(anchor["status"]) != normalized_status
                ):
                    raise ValueError("invalid Run observation cursor")
                cursor_created_at = int(anchor["created_at_ms"])

            conditions: list[str] = [
                "r.scope_kind = 'unassigned'",
                "r.run_kind = 'skill'",
            ]
            parameters: list[object] = []
            if normalized_status is not None:
                conditions.append("r.status = ?")
                parameters.append(normalized_status)
            if cursor_created_at is not None:
                conditions.append(
                    "(r.created_at_ms < ? OR "
                    "(r.created_at_ms = ? AND r.run_id < ?))"
                )
                parameters.extend(
                    (cursor_created_at, cursor_created_at, opaque_cursor)
                )
            where = " WHERE " + " AND ".join(conditions)
            rows = connection.execute(
                """
                SELECT r.*,
                       a.assignment_id AS observation_assignment_id,
                       a.executor_kind AS observation_executor_kind,
                       a.execution_reference_type AS observation_reference_type,
                       a.execution_reference AS observation_reference,
                       a.assigned_at_ms AS observation_assigned_at_ms
                FROM runs AS r
                LEFT JOIN run_execution_assignments AS a USING (run_id)
                """
                + where
                + " ORDER BY r.created_at_ms DESC, r.run_id DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()

        has_more = len(rows) > limit
        visible = rows[:limit]
        observations = tuple(_run_observation_snapshot(row) for row in visible)
        next_cursor = (
            observations[-1].receipt.run_id
            if has_more and observations
            else None
        )
        return RunObservationPage(observations, next_cursor)

    def list_nonterminal_run_observations(
        self,
    ) -> tuple[RunObservationSnapshot, ...]:
        """Read every recovery candidate and durable owner in one snapshot."""

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT r.*,
                       a.assignment_id AS observation_assignment_id,
                       a.executor_kind AS observation_executor_kind,
                       a.execution_reference_type AS observation_reference_type,
                       a.execution_reference AS observation_reference,
                       a.assigned_at_ms AS observation_assigned_at_ms
                FROM runs AS r
                LEFT JOIN run_execution_assignments AS a USING (run_id)
                WHERE r.status IN ('queued', 'running', 'cancel_requested')
                ORDER BY r.created_at_ms, r.rowid
                """
            ).fetchall()
        return tuple(_run_observation_snapshot(row) for row in rows)

    def list_terminal_assigned_run_observations(
        self,
    ) -> tuple[RunObservationSnapshot, ...]:
        """Read assigned terminal Runs for content-free startup consistency audit."""

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT r.*,
                       a.assignment_id AS observation_assignment_id,
                       a.executor_kind AS observation_executor_kind,
                       a.execution_reference_type AS observation_reference_type,
                       a.execution_reference AS observation_reference,
                       a.assigned_at_ms AS observation_assigned_at_ms
                FROM runs AS r
                JOIN run_execution_assignments AS a USING (run_id)
                WHERE r.status IN ('succeeded', 'failed', 'canceled', 'interrupted')
                  AND a.executor_kind = 'local-simple-skill-v1'
                ORDER BY r.created_at_ms, r.rowid
                """
            ).fetchall()
        return tuple(_run_observation_snapshot(row) for row in rows)

    def list_nonterminal_runs(self) -> tuple[RunRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('queued', 'running', 'cancel_requested')
                ORDER BY created_at_ms, rowid
                """
            ).fetchall()
        return tuple(_run_record(row) for row in rows)

    def reconcile_nonterminal_runs(self) -> RunStartupReconciliationResult:
        """Interrupt queued Runs; assigned Runs require a fenced report."""

        with self._transaction("reconcile_nonterminal_runs") as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, a.assignment_id
                FROM runs AS r
                LEFT JOIN run_execution_assignments AS a USING (run_id)
                WHERE r.status IN ('queued', 'running', 'cancel_requested')
                ORDER BY r.created_at_ms, r.rowid
                """
            ).fetchall()
            run_ids = tuple(
                str(row["run_id"]) for row in rows if row["assignment_id"] is None
            )
            unconfirmed_run_ids = tuple(
                str(row["run_id"]) for row in rows if row["assignment_id"] is not None
            )
            if run_ids:
                now = self._now()
                connection.executemany(
                    """
                    UPDATE runs
                    SET status = 'interrupted',
                        terminal_code = 'control_plane_restarted',
                        finished_at_ms = ?, revision = revision + 1
                    WHERE run_id = ?
                      AND status IN ('queued', 'running', 'cancel_requested')
                    """,
                    ((now, run_id) for run_id in run_ids),
                )
            return RunStartupReconciliationResult(run_ids, unconfirmed_run_ids)

    # ------------------------------------------------------------------
    # Project Projection Intents
    # ------------------------------------------------------------------

    def record_project_projection_intent(
        self,
        *,
        project_id: str,
        origin_kind: str,
        origin_id: str,
        projection: ProjectionIntentInput,
    ) -> ProjectionIntentRecord:
        with self._transaction("record_project_projection_intent") as connection:
            if not self._project_is_active(connection, project_id):
                raise ValueError(self._project_rejection_code(connection, project_id))
            self._validate_projection_origin(
                connection,
                project_id=project_id,
                origin_kind=origin_kind,
                origin_id=origin_id,
            )
            records = self._insert_projection_intents(
                connection,
                project_id=project_id,
                origin_kind=origin_kind,
                origin_id=origin_id,
                projections=(projection,),
            )
            return records[0]

    @staticmethod
    def _validate_projection_origin(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        origin_kind: str,
        origin_id: str,
    ) -> None:
        if origin_kind == "turn":
            row = connection.execute(
                """
                SELECT c.project_id
                FROM turns AS t JOIN conversations AS c USING (conversation_id)
                WHERE t.turn_id = ?
                """,
                (origin_id,),
            ).fetchone()
        elif origin_kind == "run":
            row = connection.execute(
                "SELECT project_id FROM runs WHERE run_id = ?", (origin_id,)
            ).fetchone()
        else:
            raise ValueError("origin_kind must be 'turn' or 'run'")
        if row is None or row["project_id"] != project_id:
            raise ValueError("projection origin does not belong to Project")

    def _insert_projection_intents(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        origin_kind: str,
        origin_id: str,
        projections: Sequence[ProjectionIntentInput],
    ) -> tuple[ProjectionIntentRecord, ...]:
        records: list[ProjectionIntentRecord] = []
        for projection in projections:
            _validate_projection(projection)
            now = self._now()
            intent_id = _new_id()
            try:
                connection.execute(
                    """
                    INSERT INTO project_projection_intents (
                        projection_intent_id, project_id, origin_kind, origin_id,
                        projection_kind, projection_schema_version, source_store,
                        source_ref, content_sha256, state, last_error_code,
                        created_at_ms, updated_at_ms, applied_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
                    """,
                    (
                        intent_id,
                        project_id,
                        origin_kind,
                        origin_id,
                        projection.projection_kind,
                        projection.projection_schema_version,
                        projection.source_store,
                        projection.source_ref,
                        projection.content_sha256,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM project_projection_intents
                    WHERE projection_intent_id = ?
                    """,
                    (intent_id,),
                ).fetchone()
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT * FROM project_projection_intents
                    WHERE project_id = ? AND origin_kind = ? AND origin_id = ?
                      AND projection_kind = ? AND source_store = ?
                      AND source_ref = ? AND content_sha256 = ?
                    """,
                    (
                        project_id,
                        origin_kind,
                        origin_id,
                        projection.projection_kind,
                        projection.source_store,
                        projection.source_ref,
                        projection.content_sha256,
                    ),
                ).fetchone()
                if row is None:
                    raise
            assert row is not None
            records.append(_projection_record(row))
        return tuple(records)

    def finish_project_projection(
        self,
        projection_intent_id: str,
        *,
        state: str,
        error_code: str | None = None,
    ) -> StateChangeResult:
        if state not in {"applied", "failed"}:
            raise ValueError("projection terminal state must be applied or failed")
        with self._transaction("finish_project_projection") as connection:
            row = connection.execute(
                """
                SELECT state FROM project_projection_intents
                WHERE projection_intent_id = ?
                """,
                (projection_intent_id,),
            ).fetchone()
            if row is None:
                return StateChangeResult(False, "projection_intent_not_found")
            if row["state"] == state:
                return StateChangeResult(False, "already_terminal")
            if row["state"] != "pending":
                return StateChangeResult(False, "projection_state_conflict")
            now = self._now()
            connection.execute(
                """
                UPDATE project_projection_intents
                SET state = ?, last_error_code = ?, updated_at_ms = ?,
                    applied_at_ms = ?
                WHERE projection_intent_id = ? AND state = 'pending'
                """,
                (
                    state,
                    error_code,
                    now,
                    now if state == "applied" else None,
                    projection_intent_id,
                ),
            )
            return StateChangeResult(True, state)

    def list_projection_intents(
        self, project_id: str
    ) -> tuple[ProjectionIntentRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_projection_intents
                WHERE project_id = ? ORDER BY created_at_ms, projection_intent_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(_projection_record(row) for row in rows)

    def list_pending_projection_intents(
        self, *, limit: int = 100
    ) -> tuple[ProjectionIntentRecord, ...]:
        """Pending Intents across *all* Projects, oldest first, for the driver.

        Deliberately not filtered by Project lifecycle: a pending Intent frozen
        while a Project was active must still be applied after archive (ADR 0064
        "A projector may apply a matching pending Intent after the Project
        becomes archived"). The projector — not this read — enforces that only
        the frozen projection is written.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_projection_intents
                WHERE state = 'pending'
                ORDER BY created_at_ms, projection_intent_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_projection_record(row) for row in rows)

    # ------------------------------------------------------------------
    # Persistent Outbound Delivery
    # ------------------------------------------------------------------

    def _insert_terminal_delivery(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        terminal_status: str,
        plan: DeliveryPlan,
        now: int,
    ) -> DeliveryRecord:
        if not plan.items:
            raise ValueError("Delivery plan must contain at least one Item")
        # ADR 0060 requires a *bounded* provider-call plan. The text renderer
        # applies its own bound, but the store is the authority: any other plan
        # producer -- a future media renderer, a recovery path, a test -- must
        # not be able to commit an unbounded fan-out of provider calls.
        if len(plan.items) > MAX_DELIVERY_ITEMS:
            raise ValueError(
                f"Delivery plan has {len(plan.items)} Items, exceeding the "
                f"bound of {MAX_DELIVERY_ITEMS}"
            )
        if plan.terminal_kind != terminal_status:
            raise ValueError("Delivery terminal kind must match Turn terminal status")
        sequence_row = connection.execute(
            """
            SELECT COALESCE(MAX(target_sequence), 0) + 1 AS next_sequence
            FROM deliveries WHERE surface = 'channel' AND reply_target_key = ?
            """,
            (row["reply_target_key"],),
        ).fetchone()
        assert sequence_row is not None
        delivery_id = _new_id()
        target_sequence = int(sequence_row["next_sequence"])
        connection.execute(
            """
            INSERT INTO deliveries (
                delivery_id, turn_id, conversation_id, purpose, terminal_kind,
                surface, reply_target_version, reply_target_key,
                reply_target_json, target_sequence, resend_of_delivery_id,
                created_at_ms
            ) VALUES (?, ?, ?, 'terminal', ?, 'channel', ?, ?, ?, ?, NULL, ?)
            """,
            (
                delivery_id,
                row["turn_id"],
                row["conversation_id"],
                terminal_status,
                row["reply_target_version"],
                row["reply_target_key"],
                row["reply_target_json"],
                target_sequence,
                now,
            ),
        )
        for ordinal, item in enumerate(plan.items):
            _validate_delivery_item(item)
            connection.execute(
                """
                INSERT INTO delivery_items (
                    item_id, delivery_id, ordinal, item_kind, content_store,
                    content_ref, content_sha256, content_range_json,
                    render_version, media_type, caption_ref, caption_sha256,
                    state, attempt_count, next_attempt_at_ms, last_error_code,
                    provider_evidence_json, blocked_by_item_id,
                    delivered_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'queued', 0, NULL, NULL, NULL, NULL, NULL, ?)
                """,
                (
                    _new_id(),
                    delivery_id,
                    ordinal,
                    item.item_kind,
                    item.content_store,
                    item.content_ref,
                    item.content_sha256,
                    _canonical_json(item.content_range)
                    if item.content_range is not None
                    else None,
                    item.render_version,
                    item.media_type,
                    item.caption_ref,
                    item.caption_sha256,
                    now,
                ),
            )
        delivery_row = connection.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        assert delivery_row is not None
        return _delivery_record(delivery_row)

    @staticmethod
    def _find_terminal_delivery(
        connection: sqlite3.Connection, turn_id: str
    ) -> DeliveryRecord | None:
        row = connection.execute(
            """
            SELECT * FROM deliveries WHERE turn_id = ? AND purpose = 'terminal'
            """,
            (turn_id,),
        ).fetchone()
        return _delivery_record(row) if row is not None else None

    def list_deliveries(
        self, *, turn_id: str | None = None
    ) -> tuple[DeliveryRecord, ...]:
        query = "SELECT * FROM deliveries"
        parameters: tuple[str, ...] = ()
        if turn_id is not None:
            query += " WHERE turn_id = ?"
            parameters = (turn_id,)
        query += " ORDER BY reply_target_key, target_sequence"
        with self._read() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_delivery_record(row) for row in rows)

    def list_delivery_items(self, delivery_id: str) -> tuple[DeliveryItemRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_items
                WHERE delivery_id = ? ORDER BY ordinal
                """,
                (delivery_id,),
            ).fetchall()
        return tuple(_delivery_item_record(row) for row in rows)

    def describe_delivery(self, delivery_id: str) -> DeliveryStatusSummary | None:
        """Return one Delivery, its ordered Items and derived operator state."""

        _require_nonempty(delivery_id, "delivery_id")
        with self._read() as connection:
            delivery_row = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if delivery_row is None:
                return None
            item_rows = connection.execute(
                """
                SELECT * FROM delivery_items
                WHERE delivery_id = ? ORDER BY ordinal
                """,
                (delivery_id,),
            ).fetchall()
        items = tuple(_delivery_item_record(row) for row in item_rows)
        return DeliveryStatusSummary(
            delivery=_delivery_record(delivery_row),
            items=items,
            state=_aggregate_delivery_state(items),
        )

    def list_delivery_attempts(
        self, delivery_id: str
    ) -> tuple[DeliveryAttemptRecord, ...]:
        """Owner/operator audit read: every provider call this Delivery made.

        Ordered by Item ordinal then attempt number, so the sequence reads the
        way the Pump executed it.
        """

        _require_nonempty(delivery_id, "delivery_id")
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM delivery_attempts AS a
                JOIN delivery_items AS i USING (item_id)
                WHERE i.delivery_id = ?
                ORDER BY i.ordinal, a.attempt_no
                """,
                (delivery_id,),
            ).fetchall()
        return tuple(_delivery_attempt_record(row) for row in rows)

    def insert_resend_delivery(
        self,
        delivery_id: str,
        *,
        max_total: int | None = None,
        max_per_account: int | None = None,
    ) -> DeliveryRecord:
        """Create a new ``purpose=resend`` Delivery reusing frozen content.

        An explicit Owner resend re-freezes the source Delivery's immutable
        content references into a fresh queued Delivery with a new opaque ID,
        linked through ``resend_of_delivery_id``.  It allocates the next target
        sequence, never reopens the Turn and never reruns a tool or Run.  The
        source may itself be a resend, forming an auditable chain.

        Two admission rules are enforced HERE rather than by the caller, so a
        concurrent operator action cannot slip between the check and the insert.
        The source must be settled: ADR 0060 scopes resend to an ``unknown`` or
        already-``delivered`` outcome, and duplicating a Delivery that still has
        a live Item would let the original and the copy both reach the Owner.
        And when the caller supplies a capacity bound, the outstanding-delivery
        count is measured in this transaction.
        """

        _require_nonempty(delivery_id, "delivery_id")
        with self._transaction("insert_resend_delivery") as connection:
            source = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if source is None:
                raise KeyError(delivery_id)
            item_rows = connection.execute(
                """
                SELECT * FROM delivery_items
                WHERE delivery_id = ? ORDER BY ordinal
                """,
                (delivery_id,),
            ).fetchall()
            if not item_rows:
                raise ControlIntegrityError("resend source Delivery has no Items")
            if len(item_rows) > MAX_DELIVERY_ITEMS:
                raise ControlIntegrityError(
                    "resend source Delivery has "
                    f"{len(item_rows)} Items, which exceeds the bound of "
                    f"{MAX_DELIVERY_ITEMS}"
                )
            outstanding = [
                str(row["item_id"])
                for row in item_rows
                if str(row["state"]) in _NONTERMINAL_DELIVERY_ITEMS
            ]
            if outstanding:
                raise DeliveryResendNotSettledError(
                    "resend source Delivery still has "
                    f"{len(outstanding)} unsettled Item(s)"
                )
            reply_target = _decode_channel_reply_target(source["reply_target_json"])
            target_version, target_key, target_json = _reply_target(reply_target)
            if (
                source["reply_target_version"] != target_version
                or source["reply_target_key"] != target_key
                or source["reply_target_json"] != target_json
            ):
                raise ControlIntegrityError(
                    "resend source Delivery has an invalid Reply Target identity"
                ) from None
            if max_total is not None and max_per_account is not None:
                within_capacity = self._delivery_capacity_available(
                    connection,
                    reply_target,
                    max_total=max_total,
                    max_per_account=max_per_account,
                )
                if not within_capacity:
                    raise DeliveryCapacityExceededError(
                        "resend would exceed the outstanding-delivery bound"
                    )
            now = self._now()
            sequence_row = connection.execute(
                """
                SELECT COALESCE(MAX(target_sequence), 0) + 1 AS next_sequence
                FROM deliveries WHERE surface = ? AND reply_target_key = ?
                """,
                (source["surface"], source["reply_target_key"]),
            ).fetchone()
            assert sequence_row is not None
            resend_id = _new_id()
            target_sequence = int(sequence_row["next_sequence"])
            connection.execute(
                """
                INSERT INTO deliveries (
                    delivery_id, turn_id, conversation_id, purpose, terminal_kind,
                    surface, reply_target_version, reply_target_key,
                    reply_target_json, target_sequence, resend_of_delivery_id,
                    created_at_ms
                ) VALUES (?, ?, ?, 'resend', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resend_id,
                    source["turn_id"],
                    source["conversation_id"],
                    source["terminal_kind"],
                    source["surface"],
                    source["reply_target_version"],
                    source["reply_target_key"],
                    source["reply_target_json"],
                    target_sequence,
                    delivery_id,
                    now,
                ),
            )
            for row in item_rows:
                connection.execute(
                    """
                    INSERT INTO delivery_items (
                        item_id, delivery_id, ordinal, item_kind, content_store,
                        content_ref, content_sha256, content_range_json,
                        render_version, media_type, caption_ref, caption_sha256,
                        state, attempt_count, next_attempt_at_ms, last_error_code,
                        provider_evidence_json, blocked_by_item_id,
                        delivered_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'queued', 0, NULL, NULL, NULL, NULL, NULL, ?)
                    """,
                    (
                        _new_id(),
                        resend_id,
                        int(row["ordinal"]),
                        row["item_kind"],
                        row["content_store"],
                        row["content_ref"],
                        row["content_sha256"],
                        row["content_range_json"],
                        int(row["render_version"]),
                        row["media_type"],
                        row["caption_ref"],
                        row["caption_sha256"],
                        now,
                    ),
                )
            resend_row = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (resend_id,)
            ).fetchone()
            assert resend_row is not None
            return _delivery_record(resend_row)

    def expedite_delivery_retries(self, delivery_id: str) -> int:
        """Bring every waiting ``retry_wait`` Item's backoff forward to now.

        This is the safe-non-acceptance ``retry_delivery`` primitive: it only
        pulls an already-scheduled retry horizon forward so the Pump claims it
        immediately.  It never reopens a ``failed``/``unknown``/``delivered``
        Item (those are terminal and require an explicit resend) and never
        changes frozen content.  Returns the number of Items re-armed.
        """

        _require_nonempty(delivery_id, "delivery_id")
        with self._transaction("expedite_delivery_retries") as connection:
            exists = connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(delivery_id)
            now = self._now()
            cursor = connection.execute(
                """
                UPDATE delivery_items
                SET next_attempt_at_ms = ?, updated_at_ms = ?
                WHERE delivery_id = ? AND state = 'retry_wait'
                  AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms > ?)
                """,
                (now, now, delivery_id, now),
            )
            return int(cursor.rowcount)

    def list_due_delivery_candidates(
        self,
        *,
        limit: int = 100,
        adapter_accounts: Sequence[tuple[str, str]] | None = None,
    ) -> tuple[DeliveryCandidate, ...]:
        """Return target-local Outbox heads owned by registered Adapter accounts."""

        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("Delivery candidate limit must be a positive integer")
        scopes = _validate_adapter_accounts(adapter_accounts)
        if adapter_accounts is not None and not scopes:
            return ()
        scope_sql, scope_parameters = _adapter_account_sql(scopes)
        now = self._now()
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT d.delivery_id, d.surface, d.reply_target_key,
                       d.reply_target_json, d.target_sequence,
                       i.item_id, i.ordinal, i.item_kind, i.content_store,
                       i.content_ref, i.content_sha256, i.content_range_json,
                       i.render_version, i.media_type, i.caption_ref,
                       i.caption_sha256, i.attempt_count
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE (
                        i.state = 'queued'
                        OR (
                            i.state = 'retry_wait'
                            AND i.next_attempt_at_ms IS NOT NULL
                            AND i.next_attempt_at_ms <= ?
                        )
                      )
                  {scope_sql}
                  AND NOT EXISTS (
                      SELECT 1 FROM delivery_items AS lower_item
                      WHERE lower_item.delivery_id = i.delivery_id
                        AND lower_item.ordinal < i.ordinal
                        AND lower_item.state != 'delivered'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM deliveries AS earlier_delivery
                      JOIN delivery_items AS earlier_item USING (delivery_id)
                      WHERE earlier_delivery.surface = d.surface
                        AND earlier_delivery.reply_target_key = d.reply_target_key
                        AND earlier_delivery.target_sequence < d.target_sequence
                        AND earlier_item.state IN ('queued','sending','retry_wait')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM delivery_items AS active_item
                      JOIN deliveries AS active_delivery USING (delivery_id)
                      WHERE active_delivery.surface = d.surface
                        AND active_delivery.reply_target_key = d.reply_target_key
                        AND active_item.state = 'sending'
                  )
                ORDER BY d.surface, d.reply_target_key, d.target_sequence, i.ordinal
                LIMIT ?
                """,
                (now, *scope_parameters, limit),
            ).fetchall()
        return tuple(_delivery_candidate(row) for row in rows)

    def next_delivery_retry_at_ms(
        self,
        *,
        adapter_accounts: Sequence[tuple[str, str]] | None = None,
    ) -> int | None:
        """Return the next owned target-head retry horizon, if one exists."""

        scopes = _validate_adapter_accounts(adapter_accounts)
        if adapter_accounts is not None and not scopes:
            return None
        scope_sql, scope_parameters = _adapter_account_sql(scopes)

        with self._read() as connection:
            row = connection.execute(
                f"""
                SELECT MIN(i.next_attempt_at_ms) AS retry_at_ms
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE i.state = 'retry_wait'
                  AND i.next_attempt_at_ms IS NOT NULL
                  {scope_sql}
                  AND NOT EXISTS (
                      SELECT 1 FROM delivery_items AS lower_item
                      WHERE lower_item.delivery_id = i.delivery_id
                        AND lower_item.ordinal < i.ordinal
                        AND lower_item.state != 'delivered'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM deliveries AS earlier_delivery
                      JOIN delivery_items AS earlier_item USING (delivery_id)
                      WHERE earlier_delivery.surface = d.surface
                        AND earlier_delivery.reply_target_key = d.reply_target_key
                        AND earlier_delivery.target_sequence < d.target_sequence
                        AND earlier_item.state IN ('queued','sending','retry_wait')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM delivery_items AS active_item
                      JOIN deliveries AS active_delivery USING (delivery_id)
                      WHERE active_delivery.surface = d.surface
                        AND active_delivery.reply_target_key = d.reply_target_key
                        AND active_item.state = 'sending'
                  )
                """,
                scope_parameters,
            ).fetchone()
        assert row is not None
        value = row["retry_at_ms"]
        return int(value) if value is not None else None

    def channel_delivery_capacity(self) -> DeliveryCapacitySnapshot:
        """Count future Channel terminal slots and actual nonterminal Outbox work."""

        with self._read() as connection:
            future_row = connection.execute(
                """
                SELECT COUNT(*) AS future_deliveries
                FROM turns AS t
                JOIN conversations AS c USING (conversation_id)
                WHERE c.surface = 'channel' AND t.status IN ('queued','running')
                """
            ).fetchone()
            actual_row = connection.execute(
                """
                SELECT COUNT(DISTINCT d.delivery_id) AS actual_deliveries,
                       COUNT(*) AS actual_items
                FROM deliveries AS d
                JOIN delivery_items AS i USING (delivery_id)
                WHERE d.surface = 'channel'
                  AND i.state IN ('queued','sending','retry_wait')
                """
            ).fetchone()
        assert future_row is not None and actual_row is not None
        return DeliveryCapacitySnapshot(
            future_deliveries=int(future_row["future_deliveries"]),
            actual_deliveries=int(actual_row["actual_deliveries"]),
            actual_items=int(actual_row["actual_items"]),
        )

    def has_delivery_capacity(
        self,
        reply_target: Mapping[str, Any],
        *,
        max_total: int,
        max_per_account: int,
        reserved_total: int = 0,
        reserved_for_account: int = 0,
    ) -> bool:
        """Check durable and process-reserved Channel units for one account."""

        with self._read() as connection:
            return self._delivery_capacity_available(
                connection,
                reply_target,
                max_total=max_total,
                max_per_account=max_per_account,
                reserved_total=reserved_total,
                reserved_for_account=reserved_for_account,
            )

    def _delivery_capacity_available(
        self,
        connection: sqlite3.Connection,
        reply_target: Mapping[str, Any],
        *,
        max_total: int,
        max_per_account: int,
        reserved_total: int = 0,
        reserved_for_account: int = 0,
    ) -> bool:
        """Evaluate the capacity bound on a caller-supplied connection.

        Taking the connection rather than opening one lets a writer apply the
        bound inside the same transaction that consumes the unit, so two
        concurrent admissions cannot both observe the last free slot.
        """

        for name, value in (
            ("max_total", max_total),
            ("max_per_account", max_per_account),
            ("reserved_total", reserved_total),
            ("reserved_for_account", reserved_for_account),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            not isinstance(reply_target, Mapping)
            or reply_target.get("kind") != "channel"
        ):
            raise ValueError("Delivery capacity requires a Channel Reply Target")
        adapter = _require_nonempty(str(reply_target.get("adapter", "")), "adapter")
        account_namespace = _require_nonempty(
            str(reply_target.get("account_namespace", "")),
            "account_namespace",
        )

        future_rows = connection.execute(
            """
            SELECT c.reply_target_json
            FROM turns AS t
            JOIN conversations AS c USING (conversation_id)
            WHERE c.surface = 'channel' AND t.status IN ('queued','running')
            """
        ).fetchall()
        actual_rows = connection.execute(
            """
            SELECT d.reply_target_json
            FROM deliveries AS d
            WHERE d.surface = 'channel'
              AND EXISTS (
                  SELECT 1 FROM delivery_items AS i
                  WHERE i.delivery_id = d.delivery_id
                    AND i.state IN ('queued','sending','retry_wait')
              )
            """
        ).fetchall()

        targets = [
            json.loads(str(row["reply_target_json"]))
            for row in (*future_rows, *actual_rows)
        ]
        total = len(targets)
        account_total = sum(
            1
            for target in targets
            if target.get("adapter") == adapter
            and target.get("account_namespace") == account_namespace
        )
        return (
            total + reserved_total < max_total
            and account_total + reserved_for_account < max_per_account
        )

    def reconcile_delivery_startup(self) -> DeliveryStartupRecoveryResult:
        """Close every crash-left provider call as acceptance-unknown atomically."""

        unknown_item_ids: list[str] = []
        closed_attempt_ids: list[str] = []
        suppressed_item_ids: list[str] = []
        with self._transaction("reconcile_delivery_startup") as connection:
            open_attempts = connection.execute(
                """
                SELECT a.attempt_id, a.item_id, i.delivery_id, i.ordinal,
                       i.state AS item_state, d.surface, d.reply_target_key,
                       d.target_sequence
                FROM delivery_attempts AS a
                JOIN delivery_items AS i USING (item_id)
                JOIN deliveries AS d USING (delivery_id)
                WHERE a.finished_at_ms IS NULL
                ORDER BY d.surface, d.reply_target_key, d.target_sequence, i.ordinal
                """
            ).fetchall()
            sending_rows = connection.execute(
                "SELECT item_id FROM delivery_items WHERE state = 'sending'"
            ).fetchall()
            open_item_ids = [str(row["item_id"]) for row in open_attempts]
            sending_item_ids = [str(row["item_id"]) for row in sending_rows]
            if (
                len(open_item_ids) != len(set(open_item_ids))
                or set(open_item_ids) != set(sending_item_ids)
                or any(str(row["item_state"]) != "sending" for row in open_attempts)
            ):
                raise ControlIntegrityError(
                    "Delivery startup found inconsistent sending/open Attempt state"
                )

            now = self._now()
            for row in open_attempts:
                suffix_rows = connection.execute(
                    """
                    SELECT item_id FROM delivery_items
                    WHERE delivery_id = ? AND ordinal > ?
                      AND state IN ('queued','retry_wait')
                    ORDER BY ordinal
                    """,
                    (row["delivery_id"], row["ordinal"]),
                ).fetchall()
                suffix_ids = [str(suffix["item_id"]) for suffix in suffix_rows]
                connection.execute(
                    """
                    UPDATE delivery_attempts
                    SET finished_at_ms = ?, outcome = 'acceptance_unknown',
                        error_code = 'control_plane_restarted',
                        provider_evidence_json = NULL
                    WHERE attempt_id = ? AND finished_at_ms IS NULL
                    """,
                    (now, row["attempt_id"]),
                )
                connection.execute(
                    """
                    UPDATE delivery_items
                    SET state = 'unknown', next_attempt_at_ms = NULL,
                        last_error_code = 'control_plane_restarted',
                        provider_evidence_json = NULL, delivered_at_ms = NULL,
                        updated_at_ms = ?
                    WHERE item_id = ? AND state = 'sending'
                    """,
                    (now, row["item_id"]),
                )
                connection.execute(
                    """
                    UPDATE delivery_items
                    SET state = 'suppressed', blocked_by_item_id = ?,
                        next_attempt_at_ms = NULL,
                        last_error_code = 'ordered_prefix_unavailable',
                        updated_at_ms = ?
                    WHERE delivery_id = ? AND ordinal > ?
                      AND state IN ('queued','retry_wait')
                    """,
                    (row["item_id"], now, row["delivery_id"], row["ordinal"]),
                )
                unknown_item_ids.append(str(row["item_id"]))
                closed_attempt_ids.append(str(row["attempt_id"]))
                suppressed_item_ids.extend(suffix_ids)

        return DeliveryStartupRecoveryResult(
            unknown_item_ids=tuple(unknown_item_ids),
            closed_attempt_ids=tuple(closed_attempt_ids),
            suppressed_item_ids=tuple(suppressed_item_ids),
        )

    def fail_delivery_content(
        self,
        item_id: str,
        *,
        error_code: str = "content_integrity_failed",
    ) -> DeliveryItemRecord | None:
        """Fail one still-eligible Outbox head without inventing an Attempt."""

        _require_nonempty(item_id, "item_id")
        _require_nonempty(error_code, "error_code")
        with self._transaction("fail_delivery_content") as connection:
            row = connection.execute(
                """
                SELECT i.*, d.surface, d.reply_target_key, d.target_sequence
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE i.item_id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            now = self._now()
            state = str(row["state"])
            if state == "retry_wait" and (
                row["next_attempt_at_ms"] is None
                or int(row["next_attempt_at_ms"]) > now
            ):
                return None
            if state not in {"queued", "retry_wait"}:
                return None

            lower_item = connection.execute(
                """
                SELECT 1 FROM delivery_items
                WHERE delivery_id = ? AND ordinal < ? AND state != 'delivered'
                LIMIT 1
                """,
                (row["delivery_id"], row["ordinal"]),
            ).fetchone()
            earlier_sequence = connection.execute(
                """
                SELECT 1
                FROM deliveries AS d
                JOIN delivery_items AS i USING (delivery_id)
                WHERE d.surface = ? AND d.reply_target_key = ?
                  AND d.target_sequence < ?
                  AND i.state IN ('queued', 'sending', 'retry_wait')
                LIMIT 1
                """,
                (row["surface"], row["reply_target_key"], row["target_sequence"]),
            ).fetchone()
            active = connection.execute(
                """
                SELECT 1
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE d.surface = ? AND d.reply_target_key = ?
                  AND i.state = 'sending'
                LIMIT 1
                """,
                (row["surface"], row["reply_target_key"]),
            ).fetchone()
            if (
                lower_item is not None
                or earlier_sequence is not None
                or active is not None
            ):
                return None

            connection.execute(
                """
                UPDATE delivery_items
                SET state = 'failed', next_attempt_at_ms = NULL,
                    last_error_code = ?, provider_evidence_json = NULL,
                    delivered_at_ms = NULL, updated_at_ms = ?
                WHERE item_id = ? AND state IN ('queued', 'retry_wait')
                """,
                (error_code, now, item_id),
            )
            connection.execute(
                """
                UPDATE delivery_items
                SET state = 'suppressed', blocked_by_item_id = ?,
                    next_attempt_at_ms = NULL,
                    last_error_code = 'ordered_prefix_unavailable',
                    updated_at_ms = ?
                WHERE delivery_id = ? AND ordinal > ?
                  AND state IN ('queued', 'retry_wait')
                """,
                (item_id, now, row["delivery_id"], row["ordinal"]),
            )
            updated = connection.execute(
                "SELECT * FROM delivery_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            assert updated is not None
            return _delivery_item_record(updated)

    def claim_delivery_attempt(
        self,
        candidate: DeliveryCandidate,
        *,
        text: str = "",
    ) -> DeliveryAttemptClaim:
        """Recheck all durable barriers and return one frozen Adapter request."""

        if not isinstance(candidate, DeliveryCandidate):
            raise TypeError("candidate must be a DeliveryCandidate")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        started = self.begin_delivery_attempt(candidate.item_id)
        if not started.started:
            return DeliveryAttemptClaim(False, started.code)
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT d.delivery_id, d.surface, d.reply_target_key,
                       d.reply_target_json, d.target_sequence,
                       i.item_id, i.ordinal, i.item_kind, i.content_store,
                       i.content_ref, i.content_sha256, i.content_range_json,
                       i.render_version, i.media_type, i.caption_ref,
                       i.caption_sha256, i.attempt_count
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE i.item_id = ?
                """,
                (candidate.item_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the owning transaction
            raise ControlIntegrityError("claimed Delivery Item disappeared")
        claimed_candidate = _delivery_candidate(
            row,
            attempt_count=started.attempt_no - 1,
        )
        return DeliveryAttemptClaim(
            True,
            "claimed",
            DeliveryAttemptRequest(
                attempt_id=started.attempt_id,
                attempt_no=started.attempt_no,
                candidate=claimed_candidate,
                text=text,
            ),
        )

    def begin_delivery_attempt(self, item_id: str) -> AttemptStartResult:
        with self._transaction("begin_delivery_attempt") as connection:
            row = connection.execute(
                """
                SELECT i.*, d.surface, d.reply_target_key, d.target_sequence
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE i.item_id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return AttemptStartResult(False, "item_not_found")
            state = str(row["state"])
            now = self._now()
            if state == "retry_wait" and (
                row["next_attempt_at_ms"] is None
                or int(row["next_attempt_at_ms"]) > now
            ):
                return AttemptStartResult(False, "retry_not_due")
            if state not in {"queued", "retry_wait"}:
                return AttemptStartResult(False, "item_not_sendable")

            lower_item = connection.execute(
                """
                SELECT 1 FROM delivery_items
                WHERE delivery_id = ? AND ordinal < ? AND state != 'delivered'
                LIMIT 1
                """,
                (row["delivery_id"], row["ordinal"]),
            ).fetchone()
            if lower_item is not None:
                return AttemptStartResult(False, "earlier_item_not_delivered")

            earlier_sequence = connection.execute(
                """
                SELECT 1
                FROM deliveries AS d
                JOIN delivery_items AS i USING (delivery_id)
                WHERE d.surface = ? AND d.reply_target_key = ?
                  AND d.target_sequence < ?
                  AND i.state IN ('queued', 'sending', 'retry_wait')
                LIMIT 1
                """,
                (row["surface"], row["reply_target_key"], row["target_sequence"]),
            ).fetchone()
            if earlier_sequence is not None:
                return AttemptStartResult(False, "earlier_target_sequence")

            active = connection.execute(
                """
                SELECT 1
                FROM delivery_items AS i
                JOIN deliveries AS d USING (delivery_id)
                WHERE d.surface = ? AND d.reply_target_key = ?
                  AND i.state = 'sending'
                LIMIT 1
                """,
                (row["surface"], row["reply_target_key"]),
            ).fetchone()
            if active is not None:
                return AttemptStartResult(False, "target_call_active")

            attempt_no = int(row["attempt_count"]) + 1
            attempt_id = _new_id()
            connection.execute(
                """
                INSERT INTO delivery_attempts (
                    attempt_id, item_id, attempt_no, started_at_ms,
                    finished_at_ms, outcome, error_code, provider_evidence_json
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """,
                (attempt_id, item_id, attempt_no, now),
            )
            connection.execute(
                """
                UPDATE delivery_items
                SET state = 'sending', attempt_count = ?, next_attempt_at_ms = NULL,
                    last_error_code = NULL, updated_at_ms = ?
                WHERE item_id = ?
                """,
                (attempt_no, now, item_id),
            )
            return AttemptStartResult(
                True,
                "started",
                attempt_id,
                item_id,
                attempt_no,
            )

    def finish_delivery_attempt(
        self,
        attempt_id: str,
        outcome: DeliveryAttemptOutcome,
        *,
        error_code: str | None = None,
        provider_evidence: Mapping[str, Any] | None = None,
        retry_at_ms: int | None = None,
        retry_exhausted: bool = False,
    ) -> DeliveryItemRecord:
        if not isinstance(outcome, DeliveryAttemptOutcome):
            outcome = DeliveryAttemptOutcome(outcome)
        if provider_evidence is not None:
            validate_delivery_provider_evidence(provider_evidence)
        if not isinstance(retry_exhausted, bool):
            raise TypeError("retry_exhausted must be a boolean")
        if (
            retry_exhausted
            and outcome is not DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE
        ):
            raise ValueError("Only a retryable outcome can exhaust retries")
        if retry_at_ms is not None and (
            not isinstance(retry_at_ms, int)
            or isinstance(retry_at_ms, bool)
            or retry_at_ms < 0
        ):
            raise ValueError("retry_at_ms must be a non-negative integer or None")
        if (
            retry_at_ms is not None
            and outcome is not DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE
        ):
            raise ValueError("retry_at_ms requires a retryable outcome")
        with self._transaction("finish_delivery_attempt") as connection:
            row = connection.execute(
                """
                SELECT a.*, i.delivery_id, i.ordinal, i.state AS item_state
                FROM delivery_attempts AS a
                JOIN delivery_items AS i USING (item_id)
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if row["finished_at_ms"] is not None:
                item = connection.execute(
                    "SELECT * FROM delivery_items WHERE item_id = ?", (row["item_id"],)
                ).fetchone()
                assert item is not None
                return _delivery_item_record(item)
            if row["item_state"] != "sending":
                raise ControlIntegrityError(
                    "open Delivery Attempt has non-sending Item"
                )

            now = self._now()
            evidence_json = (
                _canonical_json(provider_evidence)
                if provider_evidence is not None
                else None
            )
            if outcome is DeliveryAttemptOutcome.ACCEPTED:
                item_state = "delivered"
                next_attempt = None
                delivered_at = now
            elif (
                outcome is DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE
                and not retry_exhausted
            ):
                item_state = "retry_wait"
                next_attempt = retry_at_ms if retry_at_ms is not None else now + 1_000
                delivered_at = None
            elif outcome is DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE:
                item_state = "failed"
                next_attempt = None
                delivered_at = None
            elif outcome is DeliveryAttemptOutcome.REJECTED_PERMANENT:
                item_state = "failed"
                next_attempt = None
                delivered_at = None
            else:
                item_state = "unknown"
                next_attempt = None
                delivered_at = None

            connection.execute(
                """
                UPDATE delivery_attempts
                SET finished_at_ms = ?, outcome = ?, error_code = ?,
                    provider_evidence_json = ?
                WHERE attempt_id = ? AND finished_at_ms IS NULL
                """,
                (now, outcome.value, error_code, evidence_json, attempt_id),
            )
            connection.execute(
                """
                UPDATE delivery_items
                SET state = ?, next_attempt_at_ms = ?, last_error_code = ?,
                    provider_evidence_json = ?, delivered_at_ms = ?, updated_at_ms = ?
                WHERE item_id = ? AND state = 'sending'
                """,
                (
                    item_state,
                    next_attempt,
                    error_code,
                    evidence_json,
                    delivered_at,
                    now,
                    row["item_id"],
                ),
            )
            if item_state in {"failed", "unknown"}:
                connection.execute(
                    """
                    UPDATE delivery_items
                    SET state = 'suppressed', blocked_by_item_id = ?,
                        next_attempt_at_ms = NULL,
                        last_error_code = 'ordered_prefix_unavailable',
                        updated_at_ms = ?
                    WHERE delivery_id = ? AND ordinal > ?
                      AND state IN ('queued', 'retry_wait')
                    """,
                    (
                        row["item_id"],
                        now,
                        row["delivery_id"],
                        row["ordinal"],
                    ),
                )
            item = connection.execute(
                "SELECT * FROM delivery_items WHERE item_id = ?", (row["item_id"],)
            ).fetchone()
            assert item is not None
            return _delivery_item_record(item)


def _validate_turn_intent(intent: TurnAcceptanceIntent) -> None:
    if intent.surface not in _SURFACES:
        raise ValueError(f"unsupported Surface: {intent.surface}")
    _require_nonempty(intent.source_namespace, "source_namespace")
    _require_nonempty(intent.source_request_id, "source_request_id")
    if intent.fingerprint_version < 1:
        raise ValueError("fingerprint_version must be positive")
    _require_digest(intent.fingerprint_sha256, "fingerprint_sha256")
    if intent.turn_kind not in {"agent", "control_command"}:
        raise ValueError(f"unsupported turn_kind: {intent.turn_kind}")
    if intent.new_conversation and intent.explicit_conversation_id is not None:
        raise ValueError("new_conversation and explicit_conversation_id conflict")


def _require_private_path(path: Path, *, directory: bool) -> None:
    """Reject existing POSIX control paths exposed to group/other users."""
    if os.name != "posix":  # pragma: no cover - Windows ACLs need native checks
        return
    details = path.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ControlIntegrityError(f"Control state path has another owner: {path}")
    permissions = stat.S_IMODE(details.st_mode)
    if permissions & 0o077:
        expected = "0700" if directory else "0600"
        raise ControlIntegrityError(
            f"Control state path must be owner-private ({expected}): {path}"
        )


def _validate_run_intent(intent: RunAcceptanceIntent) -> None:
    _require_nonempty(intent.run_submission_id, "run_submission_id")
    _require_nonempty(intent.run_kind, "run_kind")
    _require_nonempty(intent.manifest_ref, "manifest_ref")
    if intent.fingerprint_version < 1:
        raise ValueError("fingerprint_version must be positive")
    _require_digest(intent.fingerprint_sha256, "fingerprint_sha256")
    if intent.scope_kind == "project":
        if intent.project_id is None:
            raise ValueError("Project scope requires project_id")
    elif intent.scope_kind == "unassigned":
        if intent.project_id is not None:
            raise ValueError("Unassigned scope cannot carry project_id")
    else:
        raise ValueError(f"unsupported scope_kind: {intent.scope_kind}")


def _validate_projection(projection: ProjectionIntentInput) -> None:
    _require_nonempty(projection.projection_kind, "projection_kind")
    _require_nonempty(projection.source_ref, "source_ref")
    if projection.source_store not in _SOURCE_STORES:
        raise ValueError(
            f"unsupported projection source_store: {projection.source_store}"
        )
    if projection.projection_schema_version < 1:
        raise ValueError("projection_schema_version must be positive")
    _require_digest(projection.content_sha256, "projection content_sha256")


def _validate_delivery_item(item: DeliveryItemPlan) -> None:
    if item.item_kind not in {"text", "media"}:
        raise ValueError(f"unsupported Delivery Item kind: {item.item_kind}")
    if item.content_store not in {"transcript", "run_artifact", "tool_result"}:
        raise ValueError(f"unsupported Delivery content store: {item.content_store}")
    _require_nonempty(item.content_ref, "Delivery content_ref")
    _require_digest(item.content_sha256, "Delivery content_sha256")
    if item.render_version < 1:
        raise ValueError("render_version must be positive")
    if (item.caption_ref is None) != (item.caption_sha256 is None):
        raise ValueError("caption reference and digest must appear together")
    if item.caption_sha256 is not None:
        _require_digest(item.caption_sha256, "caption_sha256")


def _validate_terminal_delivery_reference(
    plan: DeliveryPlan,
    transcript_ref: TurnTranscriptRef,
) -> None:
    """Bind every Transcript-backed Item to the committed terminal entry."""

    for item in plan.items:
        if (
            item.content_store == "transcript"
            and item.content_ref != transcript_ref.entry_id
        ):
            raise ValueError(
                "Transcript Delivery Item must reference the Turn terminal entry"
            )


def _project_record(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        project_id=str(row["project_id"]),
        display_name=str(row["display_name"]),
        lifecycle=str(row["lifecycle"]),
        revision=int(row["revision"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        lifecycle_at_ms=int(row["lifecycle_at_ms"]),
    )


def _conversation_record(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=str(row["conversation_id"]),
        surface=str(row["surface"]),
        reply_target_key=str(row["reply_target_key"]),
        reply_target=json.loads(str(row["reply_target_json"])),
        project_id=str(row["project_id"]) if row["project_id"] else None,
        revision=int(row["revision"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _turn_record(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        turn_id=str(row["turn_id"]),
        conversation_id=str(row["conversation_id"]),
        turn_kind=str(row["turn_kind"]),
        status=str(row["status"]),
        retry_of_turn_id=str(row["retry_of_turn_id"])
        if row["retry_of_turn_id"]
        else None,
        terminal_code=str(row["terminal_code"]) if row["terminal_code"] else None,
        created_at_ms=int(row["created_at_ms"]),
        started_at_ms=(
            int(row["started_at_ms"]) if row["started_at_ms"] is not None else None
        ),
        finished_at_ms=(
            int(row["finished_at_ms"]) if row["finished_at_ms"] is not None else None
        ),
        revision=int(row["revision"]),
    )


def _attachment_batch_commitment(row: sqlite3.Row) -> AttachmentBatchCommitment:
    return AttachmentBatchCommitment(
        schema_version=1,
        store_id=str(row["attachment_store_id"]),
        batch_id=str(row["batch_id"]),
        turn_id=str(row["turn_id"]),
        conversation_id=str(row["conversation_id"]),
        record_count=int(row["attachment_count"]),
        records_sha256=str(row["manifest_sha256"]),
    )


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        scope_kind=str(row["scope_kind"]),
        project_id=str(row["project_id"]) if row["project_id"] else None,
        run_kind=str(row["run_kind"]),
        parent_turn_id=str(row["parent_turn_id"]) if row["parent_turn_id"] else None,
        retry_of_run_id=str(row["retry_of_run_id"]) if row["retry_of_run_id"] else None,
        status=str(row["status"]),
        terminal_code=str(row["terminal_code"]) if row["terminal_code"] else None,
        manifest_ref=str(row["manifest_ref"]),
        created_at_ms=int(row["created_at_ms"]),
        started_at_ms=(
            int(row["started_at_ms"]) if row["started_at_ms"] is not None else None
        ),
        finished_at_ms=(
            int(row["finished_at_ms"]) if row["finished_at_ms"] is not None else None
        ),
        revision=int(row["revision"]),
    )


def _autoagent_session_record(row: sqlite3.Row) -> AutoAgentSessionRecord:
    result_json = row["result_json"]
    result: Mapping[str, Any] | None = None
    result_sha256 = (
        str(row["result_sha256"]) if row["result_sha256"] is not None else None
    )
    if result_json is not None:
        encoded = str(result_json)
        actual_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if result_sha256 is None or not secrets.compare_digest(
            result_sha256, actual_digest
        ):
            raise ControlIntegrityError("AutoAgent terminal result digest mismatch")
        try:
            decoded = json.loads(encoded)
        except (TypeError, ValueError) as exc:  # pragma: no cover - schema guard
            raise ControlIntegrityError("AutoAgent terminal result is invalid JSON") from exc
        if not isinstance(decoded, dict) or decoded.get("success") is not True:
            raise ControlIntegrityError("AutoAgent terminal result is not successful")
        result = decoded
    elif result_sha256 is not None:
        raise ControlIntegrityError("AutoAgent result digest has no result")
    return AutoAgentSessionRecord(
        session_id=str(row["session_id"]),
        cwd=str(row["cwd"]),
        output_dir=str(row["output_dir"]),
        skill=str(row["skill"]),
        method=str(row["method"]),
        evolution_goal=str(row["evolution_goal"]),
        creation_receipt_sha256=(
            str(row["creation_receipt_sha256"])
            if row["creation_receipt_sha256"] is not None
            else None
        ),
        cancel_requested_at_ms=(
            int(row["cancel_requested_at_ms"])
            if row["cancel_requested_at_ms"] is not None
            else None
        ),
        execution_reference_type=(
            str(row["execution_reference_type"])
            if row["execution_reference_type"] is not None
            else None
        ),
        execution_reference=(
            str(row["execution_reference"])
            if row["execution_reference"] is not None
            else None
        ),
        owner_stopped_at_ms=(
            int(row["owner_stopped_at_ms"])
            if row["owner_stopped_at_ms"] is not None
            else None
        ),
        owner_stop_evidence=(
            str(row["owner_stop_evidence"])
            if row["owner_stop_evidence"] is not None
            else None
        ),
        status=str(row["status"]),
        result=result,
        result_sha256=result_sha256,
        error_code=(str(row["error_code"]) if row["error_code"] is not None else None),
        error_detail=(
            str(row["error_detail"]) if row["error_detail"] is not None else None
        ),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        finished_at_ms=(
            int(row["finished_at_ms"])
            if row["finished_at_ms"] is not None
            else None
        ),
        revision=int(row["revision"]),
    )


def _run_observation_snapshot(row: sqlite3.Row) -> RunObservationSnapshot:
    run_id = str(row["run_id"])
    assignment = (
        RunAssignmentRecord(
            run_id=run_id,
            assignment_id=str(row["observation_assignment_id"]),
            executor_kind=str(row["observation_executor_kind"]),
            execution_reference_type=(
                str(row["observation_reference_type"])
                if row["observation_reference_type"] is not None
                else None
            ),
            execution_reference=(
                str(row["observation_reference"])
                if row["observation_reference"] is not None
                else None
            ),
            assigned_at_ms=int(row["observation_assigned_at_ms"]),
        )
        if row["observation_assignment_id"] is not None
        else None
    )
    return RunObservationSnapshot(receipt=_run_record(row), assignment=assignment)


def _run_integrity_incident_record(
    row: sqlite3.Row,
) -> RunIntegrityIncidentRecord:
    return RunIntegrityIncidentRecord(
        incident_id=str(row["incident_id"]),
        run_id=str(row["run_id"]),
        assignment_id=str(row["assignment_id"]),
        incident_type=RunIntegrityIncidentType(str(row["incident_type"])),
        evidence_code=RunIntegrityEvidenceCode(str(row["evidence_code"])),
        receipt_revision=int(row["receipt_revision"]),
        evidence_schema_version=int(row["evidence_schema_version"]),
        evidence_sha256=str(row["evidence_sha256"]),
        created_at_ms=int(row["created_at_ms"]),
    )


def _delivery_record(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        delivery_id=str(row["delivery_id"]),
        turn_id=str(row["turn_id"]),
        conversation_id=str(row["conversation_id"]),
        purpose=str(row["purpose"]),
        terminal_kind=str(row["terminal_kind"]),
        surface=str(row["surface"]),
        reply_target_key=str(row["reply_target_key"]),
        reply_target=json.loads(str(row["reply_target_json"])),
        target_sequence=int(row["target_sequence"]),
        resend_of_delivery_id=(
            str(row["resend_of_delivery_id"]) if row["resend_of_delivery_id"] else None
        ),
        created_at_ms=int(row["created_at_ms"]),
    )


def _delivery_evidence(raw: object) -> Mapping[str, Any] | None:
    """Decode stored provider evidence under the write-side audit contract."""

    if raw is None:
        return None
    invalid = False
    try:
        decoded = json.loads(str(raw))
        validate_delivery_provider_evidence(decoded)
    except (json.JSONDecodeError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise ControlIntegrityError(
            "stored Delivery provider evidence violates its contract"
        )
    return MappingProxyType(decoded)


def _delivery_attempt_record(row: sqlite3.Row) -> DeliveryAttemptRecord:
    return DeliveryAttemptRecord(
        attempt_id=str(row["attempt_id"]),
        item_id=str(row["item_id"]),
        attempt_no=int(row["attempt_no"]),
        started_at_ms=int(row["started_at_ms"]),
        finished_at_ms=(
            int(row["finished_at_ms"]) if row["finished_at_ms"] is not None else None
        ),
        outcome=str(row["outcome"]) if row["outcome"] else None,
        error_code=str(row["error_code"]) if row["error_code"] else None,
        provider_evidence=_delivery_evidence(row["provider_evidence_json"]),
    )


def _delivery_item_record(row: sqlite3.Row) -> DeliveryItemRecord:
    return DeliveryItemRecord(
        item_id=str(row["item_id"]),
        delivery_id=str(row["delivery_id"]),
        ordinal=int(row["ordinal"]),
        item_kind=str(row["item_kind"]),
        content_store=str(row["content_store"]),
        content_ref=str(row["content_ref"]),
        content_sha256=str(row["content_sha256"]),
        state=str(row["state"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at_ms=(
            int(row["next_attempt_at_ms"])
            if row["next_attempt_at_ms"] is not None
            else None
        ),
        last_error_code=(
            str(row["last_error_code"]) if row["last_error_code"] else None
        ),
        provider_evidence=_delivery_evidence(row["provider_evidence_json"]),
        delivered_at_ms=(
            int(row["delivered_at_ms"]) if row["delivered_at_ms"] is not None else None
        ),
        blocked_by_item_id=(
            str(row["blocked_by_item_id"]) if row["blocked_by_item_id"] else None
        ),
    )


def _aggregate_delivery_state(items: Sequence[DeliveryItemRecord]) -> str:
    """Roll ordered Item states up to one Owner/operator-facing summary state."""

    if not items:
        return "empty"
    non_delivered = [item for item in items if item.state != "delivered"]
    if not non_delivered:
        return "delivered"
    head = min(non_delivered, key=lambda item: item.ordinal)
    if head.state in {"queued", "sending", "retry_wait"}:
        return "in_progress"
    if head.state in {"failed", "unknown"}:
        return head.state
    return "blocked"


def _delivery_candidate(
    row: sqlite3.Row,
    *,
    attempt_count: int | None = None,
) -> DeliveryCandidate:
    content_range = row["content_range_json"]
    return DeliveryCandidate(
        delivery_id=str(row["delivery_id"]),
        item_id=str(row["item_id"]),
        surface=str(row["surface"]),
        reply_target_key=str(row["reply_target_key"]),
        reply_target=json.loads(str(row["reply_target_json"])),
        target_sequence=int(row["target_sequence"]),
        ordinal=int(row["ordinal"]),
        item_kind=str(row["item_kind"]),
        content_store=str(row["content_store"]),
        content_ref=str(row["content_ref"]),
        content_sha256=str(row["content_sha256"]),
        content_range=(
            json.loads(str(content_range)) if content_range is not None else None
        ),
        render_version=int(row["render_version"]),
        media_type=str(row["media_type"]) if row["media_type"] else None,
        caption_ref=str(row["caption_ref"]) if row["caption_ref"] else None,
        caption_sha256=(str(row["caption_sha256"]) if row["caption_sha256"] else None),
        attempt_count=(
            int(row["attempt_count"]) if attempt_count is None else int(attempt_count)
        ),
    )


def _projection_record(row: sqlite3.Row) -> ProjectionIntentRecord:
    return ProjectionIntentRecord(
        projection_intent_id=str(row["projection_intent_id"]),
        project_id=str(row["project_id"]),
        origin_kind=str(row["origin_kind"]),
        origin_id=str(row["origin_id"]),
        projection_kind=str(row["projection_kind"]),
        projection_schema_version=int(row["projection_schema_version"]),
        source_store=str(row["source_store"]),
        source_ref=str(row["source_ref"]),
        content_sha256=str(row["content_sha256"]),
        state=str(row["state"]),
        last_error_code=(
            str(row["last_error_code"]) if row["last_error_code"] else None
        ),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        applied_at_ms=(
            int(row["applied_at_ms"]) if row["applied_at_ms"] is not None else None
        ),
    )


__all__ = ["ControlStateRepository"]
