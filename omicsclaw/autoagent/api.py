"""FastAPI Router for autoagent harness evolution.

Provides SSE-based streaming endpoints for the harness evolution loop.
All endpoints are prefixed with ``/autoagent``.

Mount this router in the main app server with a single line:

    from omicsclaw.autoagent.api import router as autoagent_router
    app.include_router(autoagent_router)
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from omicsclaw.autoagent.constants import (
    API_RATE_LIMIT_PER_MINUTE,
    SESSION_TTL_SECONDS as _SESSION_TTL_SECONDS,
)
from omicsclaw.autoagent.output_ownership import preclaim_session_output_root
from omicsclaw.autoagent.process_owner import (
    GovernedAutoAgentWorker,
    GovernedWorkerOutcome,
    GovernedWorkerStopUnconfirmed,
    GovernedWorkerUnavailable,
    OWNER_STOP_EVIDENCE_CODE,
    governed_worker_available,
    new_governed_worker_reference,
    prepare_governed_worker_ipc_root,
    reconcile_governed_worker,
)
from omicsclaw.autoagent.search_space import build_method_surface
from omicsclaw.control import (
    AutoAgentActiveCapacityError,
    AutoAgentCapacityError,
    AutoAgentSessionRecord,
    AutoAgentStartupReconciliationResult,
    ControlStateRepository,
)
from omicsclaw.skill.execution.environment import (
    scrub_internal_control_credentials,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autoagent", tags=["autoagent"])

_sessions: dict[str, "OptimizeSessionRuntime"] = {}
_autoagent_repository: ControlStateRepository | None = None
_autoagent_unconfirmed_owner_session_ids: tuple[str, ...] = ()
_autoagent_terminal_commit_pending_session_ids: tuple[str, ...] = ()
_repository_lock = threading.RLock()
_GOVERNED_WORKER_INTEGRATED = True
_SAFE_SESSION_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_SAFE_SESSION_ID_RE = re.compile(rf"^{_SAFE_SESSION_ID_PATTERN}$")
_EXTERNAL_SESSION_ID_PATTERN = r"[0-9a-f]{32}"
_EXTERNAL_SESSION_ID_RE = re.compile(rf"^{_EXTERNAL_SESSION_ID_PATTERN}$")

# Completed sessions are kept for this many seconds so /status and
# /results can still query them, then reaped to prevent memory leak.

# Simple sliding-window rate limiter for /start endpoint.
_start_timestamps: deque[float] = deque()
_rate_lock = threading.Lock()
_DURABLE_ERROR_DETAILS = {
    "harness_failed": "Harness evolution failed",
    "invalid_terminal_result": "Optimization result failed validation",
    "worker_crashed": "Optimization worker crashed",
    "worker_start_failed": "Optimization worker could not start",
    "cancelled": "Optimization cancelled",
    "result_capacity_exhausted": "AutoAgent durable result capacity is exhausted",
    "repository_failure": "Optimization terminal state could not be committed",
    "backend_restart_interrupted": "Optimization interrupted by Backend restart",
    "backend_shutdown_interrupted": "Optimization interrupted by Backend shutdown",
}
_SESSION_EVENT_QUEUE_CAPACITY = 256
_SESSION_EVENT_PROGRESS_LIMIT = _SESSION_EVENT_QUEUE_CAPACITY - 2
_AUTOAGENT_SSE_DATA_MAX_BYTES = 256 * 1024
_AUTOAGENT_SSE_TOTAL_MAX_BYTES = 17 * 1024 * 1024
_AUTOAGENT_SSE_EVENT_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_AUTOAGENT_TERMINAL_COMMIT_RETRY_INITIAL_SECONDS = 0.25
_AUTOAGENT_TERMINAL_COMMIT_RETRY_MAX_SECONDS = 5.0
_RUNTIME_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled", "interrupted"})
_TERMINAL_RECEIPT_ERROR_CODES = frozenset(_DURABLE_ERROR_DETAILS)
_TERMINAL_RECEIPT_ERROR_CODES_BY_STATUS = {
    "error": frozenset(
        {
            "harness_failed",
            "invalid_terminal_result",
            "worker_crashed",
            "worker_start_failed",
            "result_capacity_exhausted",
            "repository_failure",
        }
    ),
    "cancelled": frozenset({"cancelled"}),
    "interrupted": frozenset(
        {"backend_restart_interrupted", "backend_shutdown_interrupted"}
    ),
}


class AutoAgentSSEBoundsError(ValueError):
    """One already-validated worker event cannot fit the Desktop wire."""


def _render_autoagent_sse_frame(
    event_type: str,
    data: Mapping[str, Any],
) -> bytes:
    """Render finite JSON without ASCII expansion under the IPC event bound."""

    if (
        not isinstance(event_type, str)
        or _AUTOAGENT_SSE_EVENT_TYPE_RE.fullmatch(event_type) is None
        or not isinstance(data, Mapping)
    ):
        raise AutoAgentSSEBoundsError("AutoAgent SSE event is invalid")
    try:
        encoded = json.dumps(
            dict(data),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise AutoAgentSSEBoundsError("AutoAgent SSE event is not finite JSON") from exc
    if not encoded or len(encoded) > _AUTOAGENT_SSE_DATA_MAX_BYTES:
        raise AutoAgentSSEBoundsError("AutoAgent SSE event exceeds its byte bound")
    return b"event: " + event_type.encode("ascii") + b"\ndata: " + encoded + b"\n\n"


class AutoAgentWorkersUnconfirmedError(RuntimeError):
    """Bounded shutdown could not prove that every Python worker exited."""

    def __init__(
        self,
        session_ids: tuple[str, ...],
        reconciliation: AutoAgentStartupReconciliationResult,
    ) -> None:
        self.session_ids = tuple(session_ids)
        self.reconciliation = reconciliation
        super().__init__(
            f"{len(self.session_ids)} AutoAgent worker(s) remain unconfirmed"
        )


def bind_autoagent_repository(
    repository: ControlStateRepository,
) -> AutoAgentStartupReconciliationResult:
    """Synchronously bind without attempting process-owner recovery.

    This compatibility seam is useful to isolated tests.  Production lifespan
    startup uses :func:`bind_governed_autoagent_repository` so inherited owners
    are stopped and observed before any new admission is exposed.
    """

    if not isinstance(repository, ControlStateRepository):
        raise TypeError("repository must be a ControlStateRepository")
    global _autoagent_repository, _autoagent_unconfirmed_owner_session_ids
    global _autoagent_terminal_commit_pending_session_ids
    with _repository_lock:
        if (
            _autoagent_repository is not None
            and _autoagent_repository is not repository
        ):
            raise RuntimeError("AutoAgent repository is already bound")
        if _autoagent_repository is repository:
            return AutoAgentStartupReconciliationResult(
                (),
                _autoagent_unconfirmed_owner_session_ids,
            )
        reconciled = repository.reconcile_autoagent_sessions()
        _autoagent_repository = repository
        _autoagent_terminal_commit_pending_session_ids = ()
        _autoagent_unconfirmed_owner_session_ids = reconciled.unconfirmed_session_ids
        return reconciled


async def bind_governed_autoagent_repository(
    repository: ControlStateRepository,
) -> AutoAgentStartupReconciliationResult:
    """Reconcile every inherited owner exactly once, without payload replay."""

    if not isinstance(repository, ControlStateRepository):
        raise TypeError("repository must be a ControlStateRepository")
    global _autoagent_repository, _autoagent_unconfirmed_owner_session_ids
    global _autoagent_terminal_commit_pending_session_ids
    with _repository_lock:
        if (
            _autoagent_repository is not None
            and _autoagent_repository is not repository
        ):
            raise RuntimeError("AutoAgent repository is already bound")
        if _autoagent_repository is repository:
            return AutoAgentStartupReconciliationResult(
                (),
                _autoagent_unconfirmed_owner_session_ids,
            )

    running = repository.list_running_autoagent_sessions()
    unconfirmed: set[str] = set()
    ipc_root: Path | None = None
    if running and governed_worker_available():
        try:
            ipc_root = prepare_governed_worker_ipc_root(repository.state_root)
        except (OSError, RuntimeError, ValueError):
            ipc_root = None
    for record in running:
        if record.owner_stop_evidence == OWNER_STOP_EVIDENCE_CODE:
            continue
        if (
            ipc_root is None
            or record.execution_reference_type is None
            or record.execution_reference is None
        ):
            unconfirmed.add(record.session_id)
            continue
        try:
            evidence = await reconcile_governed_worker(
                record.execution_reference_type,
                record.execution_reference,
                ipc_root=ipc_root,
                session_id=record.session_id,
            )
            repository.confirm_autoagent_owner_stopped(
                record.session_id,
                evidence_code=evidence,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.warning(
                "Inherited AutoAgent owner %s remains unconfirmed",
                record.session_id,
            )
            unconfirmed.add(record.session_id)

    reconciled = repository.reconcile_autoagent_sessions()
    unconfirmed.update(reconciled.unconfirmed_session_ids)
    result = AutoAgentStartupReconciliationResult(
        reconciled.interrupted_session_ids,
        tuple(sorted(unconfirmed)),
    )
    with _repository_lock:
        if (
            _autoagent_repository is not None
            and _autoagent_repository is not repository
        ):
            raise RuntimeError("AutoAgent repository was concurrently bound")
        _autoagent_repository = repository
        _autoagent_terminal_commit_pending_session_ids = ()
        _autoagent_unconfirmed_owner_session_ids = result.unconfirmed_session_ids
    return result


def unbind_autoagent_repository(repository: ControlStateRepository) -> None:
    """Remove one exact test/lifespan binding without owning Repository close."""

    global _autoagent_repository, _autoagent_unconfirmed_owner_session_ids
    global _autoagent_terminal_commit_pending_session_ids
    with _repository_lock:
        if _autoagent_repository is repository:
            _autoagent_repository = None
            _autoagent_terminal_commit_pending_session_ids = ()
            _autoagent_unconfirmed_owner_session_ids = ()


async def shutdown_autoagent_repository_binding(
    repository: ControlStateRepository,
    *,
    worker_join_timeout_seconds: float = 2.0,
) -> AutoAgentStartupReconciliationResult:
    """Fence ingress and prove every governed owner absent before closure."""

    global _autoagent_repository, _autoagent_unconfirmed_owner_session_ids
    with _repository_lock:
        if _autoagent_repository is not repository:
            return AutoAgentStartupReconciliationResult(())
        _autoagent_repository = None
        _autoagent_unconfirmed_owner_session_ids = ()
        owned = tuple(
            (session_id, runtime)
            for session_id, runtime in _sessions.items()
            if getattr(runtime, "repository", None) is repository
        )

    # Kept for wire compatibility.  The governed driver owns bounded TERM,
    # KILL, and cgroup-empty deadlines; a shorter Python join timeout would
    # reintroduce the exact detached-worker bug this boundary removes.
    del worker_join_timeout_seconds
    first_error: BaseException | None = None
    unconfirmed: set[str] = set()
    reconciled = AutoAgentStartupReconciliationResult(())
    try:
        for _session_id, runtime in owned:
            # A durable terminal retry is still an owned worker task. Wake it
            # so this shutdown can hand the already-stopped owner to the
            # authoritative interrupted reconciliation below.
            runtime.request_shutdown()
        tasks = tuple(
            runtime.worker
            for _session_id, runtime in owned
            if runtime.worker is not None
        )
        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome,
                    asyncio.CancelledError,
                ):
                    if first_error is None:
                        first_error = outcome

        running = repository.list_running_autoagent_sessions()
        ipc_root: Path | None = None
        if running and governed_worker_available():
            try:
                ipc_root = prepare_governed_worker_ipc_root(repository.state_root)
            except (OSError, RuntimeError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
        for record in running:
            if record.owner_stop_evidence == OWNER_STOP_EVIDENCE_CODE:
                continue
            if (
                ipc_root is None
                or record.execution_reference_type is None
                or record.execution_reference is None
            ):
                unconfirmed.add(record.session_id)
                continue
            try:
                evidence = await reconcile_governed_worker(
                    record.execution_reference_type,
                    record.execution_reference,
                    ipc_root=ipc_root,
                    session_id=record.session_id,
                )
                repository.confirm_autoagent_owner_stopped(
                    record.session_id,
                    evidence_code=evidence,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                unconfirmed.add(record.session_id)
                if first_error is None:
                    first_error = exc
        try:
            reconciled = repository.reconcile_autoagent_sessions(
                error_code="backend_shutdown_interrupted"
            )
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        else:
            unconfirmed.update(reconciled.unconfirmed_session_ids)
            interrupted = set(reconciled.interrupted_session_ids)
            for session_id, runtime in owned:
                if session_id in interrupted:
                    runtime.freeze_interrupted(
                        repository.get_autoagent_session(session_id)
                    )
                    _release_autoagent_terminal_commit(session_id)
        for _session_id, runtime in owned:
            runtime.detach_repository(repository)
    finally:
        for session_id, _runtime in owned:
            _sessions.pop(session_id, None)

    if unconfirmed:
        with _repository_lock:
            _autoagent_repository = repository
            _autoagent_unconfirmed_owner_session_ids = tuple(sorted(unconfirmed))
        error = AutoAgentWorkersUnconfirmedError(
            tuple(sorted(unconfirmed)),
            reconciled,
        )
        if first_error is not None:
            raise error from first_error
        raise error
    if first_error is not None:
        raise first_error
    return reconciled


def _require_autoagent_repository() -> ControlStateRepository:
    with _repository_lock:
        repository = _autoagent_repository
    if repository is None:
        raise HTTPException(503, detail="AutoAgent lifecycle authority is unavailable")
    return repository


def _creation_receipt_sha256(receipt: str) -> str:
    return hashlib.sha256(receipt.encode("ascii")).hexdigest()


def _bounded_error_detail(value: object, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    if not cleaned:
        return fallback
    return cleaned[:512]


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _check_rate_limit() -> None:
    """Raise HTTP 429 if /start request rate exceeds the limit."""
    now = time.monotonic()
    cutoff = now - 60.0
    with _rate_lock:
        while _start_timestamps and _start_timestamps[0] < cutoff:
            _start_timestamps.popleft()
        if len(_start_timestamps) >= API_RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                429,
                detail=f"Rate limit exceeded: max {API_RATE_LIMIT_PER_MINUTE} optimization starts per minute.",
            )
        _start_timestamps.append(now)


def _block_autoagent_terminal_commit(session_id: str) -> None:
    """Fence novel admission while one stopped owner lacks durable terminal state."""

    global _autoagent_terminal_commit_pending_session_ids
    with _repository_lock:
        _autoagent_terminal_commit_pending_session_ids = tuple(
            sorted(
                {
                    *_autoagent_terminal_commit_pending_session_ids,
                    session_id,
                }
            )
        )


def _release_autoagent_terminal_commit(session_id: str) -> None:
    """Release only the exact terminal-commit quarantine after DB success."""

    global _autoagent_terminal_commit_pending_session_ids
    with _repository_lock:
        _autoagent_terminal_commit_pending_session_ids = tuple(
            candidate
            for candidate in _autoagent_terminal_commit_pending_session_ids
            if candidate != session_id
        )


@dataclass(frozen=True, slots=True)
class _AutoAgentTerminalIntent:
    """One bounded in-memory projection awaiting the sole durable commit."""

    status: Literal["done", "error", "cancelled", "interrupted"]
    result: dict[str, Any] | None = None
    error_code: str | None = None


def _bounded_autoagent_success_intent(
    result: dict[str, Any],
) -> _AutoAgentTerminalIntent:
    """Freeze at most one 4 MiB JSON result for same-process DB retry."""

    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if not 2 <= len(encoded) <= 4_194_304:
            raise ValueError("AutoAgent pending terminal result is oversized")
        frozen = json.loads(encoded.decode("utf-8"))
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return _AutoAgentTerminalIntent(
            status="error",
            error_code="invalid_terminal_result",
        )
    if not isinstance(frozen, dict):
        return _AutoAgentTerminalIntent(
            status="error",
            error_code="invalid_terminal_result",
        )
    return _AutoAgentTerminalIntent(status="done", result=frozen)


@dataclass
class OptimizeSessionRuntime:
    session_id: str
    loop: asyncio.AbstractEventLoop
    start_authority: "OptimizeStartAuthority | None" = None
    repository: ControlStateRepository | None = None
    # Queue is created via __post_init__ to guarantee it belongs to the
    # running event loop.  Do NOT use default_factory=asyncio.Queue here.
    queue: asyncio.Queue[dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=_SESSION_EVENT_QUEUE_CAPACITY)
        self._terminal_commit_wakeup = asyncio.Event()

    cancel_event: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    owner: GovernedAutoAgentWorker | None = None
    worker: asyncio.Task[None] | None = None
    finished_at: float = 0.0  # time.monotonic() when terminal state reached
    durable_finished_at_ms: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _finished_enqueued: bool = False
    _terminal_event: str | None = None
    terminal_commit_failed: bool = False
    authority_detached: bool = False
    shutdown_requested: bool = False
    _terminal_commit_wakeup: asyncio.Event = field(init=False)
    _terminal_commit_notice_emitted: bool = False
    _terminal_commit_loop_active: bool = False

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type in {"done", "error"}:
            # Harness internals emit a provisional terminal event before their
            # wrapper has constructed and returned the final summary.  That
            # event is not authoritative: only mark_done/mark_error may expose
            # a terminal state after the Runtime validates the final result.
            logger.debug(
                "Optimize session %s ignored provisional producer %s event",
                self.session_id,
                event_type,
            )
            return
        self._submit_to_loop(
            self._enqueue_event,
            {"type": event_type, "data": data},
        )

    def request_cancel(self, *, persist: bool = True) -> str:
        self.wake_terminal_commit_retry()
        owner: GovernedAutoAgentWorker | None
        repository: ControlStateRepository | None
        with self._lock:
            if (
                self.status in _RUNTIME_TERMINAL_STATUSES
                or self.terminal_commit_failed
                or self._terminal_commit_loop_active
                or self.authority_detached
            ):
                return self.status
            self.cancel_event.set()
            self.status = "cancelling"
            owner = self.owner
            repository = self.repository
            status = self.status
        if persist and repository is not None:
            try:
                record = repository.get_autoagent_session(self.session_id)
                if record.creation_receipt_sha256 is not None:
                    repository.request_autoagent_cancellation(
                        session_id=self.session_id,
                        creation_receipt_sha256=record.creation_receipt_sha256,
                    )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                logger.warning(
                    "AutoAgent cancellation intent persistence failed for %s",
                    self.session_id,
                )
        if owner is not None:
            owner.request_cancel()
        return status

    def request_shutdown(self) -> str:
        with self._lock:
            self.shutdown_requested = True
        self.wake_terminal_commit_retry()
        return self.request_cancel()

    def wake_terminal_commit_retry(self) -> None:
        """Wake one bounded durable-terminal retry without creating a loop."""

        def wake() -> None:
            self._terminal_commit_wakeup.set()

        try:
            self.loop.call_soon_threadsafe(wake)
        except RuntimeError:
            logger.warning(
                "Optimize session %s could not wake terminal persistence because "
                "the event loop is closed",
                self.session_id,
            )

    async def wait_for_terminal_commit_retry(self, delay_seconds: float) -> None:
        """Wait for one wake signal or one bounded retry deadline."""

        if self._terminal_commit_wakeup.is_set():
            self._terminal_commit_wakeup.clear()
            return
        try:
            await asyncio.wait_for(
                self._terminal_commit_wakeup.wait(),
                timeout=delay_seconds,
            )
        except asyncio.TimeoutError:
            return
        else:
            # Preserve a signal that arrives exactly at the timeout boundary;
            # only a successfully observed wake is consumed here.
            self._terminal_commit_wakeup.clear()

    def note_terminal_commit_pending(self) -> None:
        """Expose transport loss once while retaining nonterminal authority."""

        should_emit = False
        with self._lock:
            if self.status in _RUNTIME_TERMINAL_STATUSES:
                return
            self.result = None
            self.error = "Optimization terminal commit unavailable"
            self.error_code = "repository_failure"
            self.terminal_commit_failed = True
            if not self._terminal_commit_notice_emitted:
                self._terminal_commit_notice_emitted = True
                should_emit = True
        _block_autoagent_terminal_commit(self.session_id)
        if should_emit:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "transport_error",
                    "data": {"code": "terminal_commit_unavailable"},
                },
            )

    def claim_terminal_commit_loop(self) -> None:
        """Prove that this session owns at most one terminal retry loop."""

        with self._lock:
            if self._terminal_commit_loop_active:
                raise RuntimeError("AutoAgent terminal commit loop already exists")
            self._terminal_commit_loop_active = True

    def release_terminal_commit_loop(self) -> None:
        with self._lock:
            self._terminal_commit_loop_active = False

    def publish_committed_terminal(self, record: AutoAgentSessionRecord) -> None:
        """Project one already-durable terminal and only then close the stream."""

        if (
            record.session_id != self.session_id
            or record.status not in _RUNTIME_TERMINAL_STATUSES
        ):
            raise ValueError("AutoAgent durable terminal record does not match runtime")
        retained_result: dict[str, Any] | None = None
        event_type = "error"
        if record.status == "done":
            retained_result = _thaw_json(record.result)
            if not isinstance(retained_result, dict):
                raise ValueError("AutoAgent durable success result is invalid")
            event_type = "done"
        elif (
            record.result is not None
            or record.error_code not in _TERMINAL_RECEIPT_ERROR_CODES
        ):
            raise ValueError("AutoAgent durable error record is invalid")
        with self._lock:
            if self.status in _RUNTIME_TERMINAL_STATUSES:
                return
            self.result = retained_result
            self.error = record.error_detail
            self.error_code = record.error_code
            self.status = record.status
            self.finished_at = time.monotonic()
            self.durable_finished_at_ms = record.finished_at_ms
            self.terminal_commit_failed = False
            self._terminal_event = event_type
        _release_autoagent_terminal_commit(self.session_id)
        receipt = self._terminal_receipt(
            status=record.status,
            error_code=record.error_code,
        )
        self._submit_to_loop(
            self._enqueue_event,
            {
                "type": event_type,
                "data": receipt,
            },
        )
        self._finish()

    def _terminal_receipt(
        self,
        *,
        status: str,
        error_code: str | None = None,
    ) -> dict[str, str]:
        """Build the bounded SSE projection of an authoritative terminal state.

        Scientific result payloads can be several MiB and remain available only
        through ``/results``.  The event stream carries identity and state so a
        client can close the stream and perform an authoritative status/result
        read without buffering the result twice.
        """

        if _EXTERNAL_SESSION_ID_RE.fullmatch(self.session_id) is None:
            raise ValueError("terminal receipt session_id must be 32 lowercase hex")
        if status == "done":
            if error_code is not None:
                raise ValueError("done terminal receipt must omit error_code")
        else:
            allowed_codes = _TERMINAL_RECEIPT_ERROR_CODES_BY_STATUS.get(status)
            if allowed_codes is None or error_code not in allowed_codes:
                raise ValueError("terminal receipt status/error_code is not closed")
        if error_code is not None and error_code not in _TERMINAL_RECEIPT_ERROR_CODES:
            raise ValueError("terminal receipt error_code is not bounded")
        receipt = {"session_id": self.session_id, "status": status}
        if error_code is not None:
            receipt["error_code"] = error_code
        return receipt

    def mark_done(self, result: dict[str, Any]) -> None:
        if not isinstance(result, dict) or result.get("success") is not True:
            logger.warning("mark_done rejected a result without exact success=true")
            self.mark_error(
                "Malformed optimization result: success must be true",
                error_code="invalid_terminal_result",
            )
            return
        retained_result = result
        transport_failure = False
        with self._lock:
            if (
                self.status in _RUNTIME_TERMINAL_STATUSES
                or self.terminal_commit_failed
                or self._terminal_commit_loop_active
                or self.authority_detached
            ):
                return
            if self.repository is not None:
                try:
                    record = self.repository.complete_autoagent_session_success(
                        self.session_id,
                        result,
                    )
                except AutoAgentCapacityError:
                    try:
                        error_record = self.repository.complete_autoagent_session_error(
                            self.session_id,
                            status="error",
                            error_code="result_capacity_exhausted",
                            error_detail=_DURABLE_ERROR_DETAILS[
                                "result_capacity_exhausted"
                            ],
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        transport_failure = True
                    else:
                        self.status = error_record.status
                        self.error = error_record.error_detail
                        self.error_code = error_record.error_code
                        self.finished_at = time.monotonic()
                        self.durable_finished_at_ms = error_record.finished_at_ms
                        self._terminal_event = "error"
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "AutoAgent terminal result validation failed for %s (%s)",
                        self.session_id,
                        type(exc).__name__,
                    )
                    try:
                        error_record = self.repository.complete_autoagent_session_error(
                            self.session_id,
                            status="error",
                            error_code="invalid_terminal_result",
                            error_detail=_DURABLE_ERROR_DETAILS[
                                "invalid_terminal_result"
                            ],
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        transport_failure = True
                    else:
                        self.status = error_record.status
                        self.error = "Optimization result failed validation"
                        self.error_code = error_record.error_code
                        self.finished_at = time.monotonic()
                        self.durable_finished_at_ms = error_record.finished_at_ms
                        self._terminal_event = "error"
                except (OSError, RuntimeError) as exc:
                    logger.warning(
                        "AutoAgent terminal result persistence failed for %s (%s)",
                        self.session_id,
                        type(exc).__name__,
                    )
                    transport_failure = True
                else:
                    retained_result = _thaw_json(record.result)  # type: ignore[assignment]
                    self.durable_finished_at_ms = record.finished_at_ms
                    self.result = retained_result
                    self.error = None
                    self.error_code = None
                    self.status = record.status
                    self.finished_at = time.monotonic()
                    self._terminal_event = "done"
            else:
                self.result = retained_result
                self.error = None
                self.error_code = None
                self.status = "done"
                self.finished_at = time.monotonic()
                self._terminal_event = "done"
            if transport_failure:
                self.result = None
                self.error = "Optimization terminal commit unavailable"
                self.error_code = "repository_failure"
                self.terminal_commit_failed = True
                self._terminal_event = "transport_error"
        if transport_failure:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "transport_error",
                    "data": {"code": "terminal_commit_unavailable"},
                },
            )
            self._finish()
            return
        if self.status == "error":
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "error",
                    "data": self._terminal_receipt(
                        status="error",
                        error_code=self.error_code or "invalid_terminal_result",
                    ),
                },
            )
            self._finish()
            return
        self._submit_to_loop(
            self._enqueue_event,
            {
                "type": "done",
                "data": self._terminal_receipt(status="done"),
            },
        )
        self._finish()

    def mark_cancelled(self, message: str = "Optimization cancelled") -> None:
        detail = _bounded_error_detail(message, fallback="Optimization cancelled")
        transport_failure = False
        with self._lock:
            if (
                self.status in _RUNTIME_TERMINAL_STATUSES
                or self.terminal_commit_failed
                or self._terminal_commit_loop_active
                or self.authority_detached
            ):
                return
            if self.repository is not None:
                try:
                    record = self.repository.complete_autoagent_session_error(
                        self.session_id,
                        status="cancelled",
                        error_code="cancelled",
                        error_detail=_DURABLE_ERROR_DETAILS["cancelled"],
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning(
                        "AutoAgent cancellation persistence failed for %s (%s)",
                        self.session_id,
                        type(exc).__name__,
                    )
                    detail = "Optimization terminal commit unavailable"
                    self.error_code = "repository_failure"
                    self.terminal_commit_failed = True
                    self._terminal_event = "transport_error"
                    transport_failure = True
                else:
                    self.error_code = record.error_code
                    self.status = record.status
                    self.durable_finished_at_ms = record.finished_at_ms
                    detail = record.error_detail or _DURABLE_ERROR_DETAILS["cancelled"]
            else:
                self.error_code = "cancelled"
                self.status = "cancelled"
            self.error = detail
            if not transport_failure:
                self.finished_at = time.monotonic()
        if transport_failure:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "transport_error",
                    "data": {"code": "terminal_commit_unavailable"},
                },
            )
        else:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "error",
                    "data": self._terminal_receipt(
                        status="cancelled",
                        error_code="cancelled",
                    ),
                },
            )
        self._finish()

    def mark_interrupted(self, *, error_code: str) -> None:
        if error_code not in {
            "backend_restart_interrupted",
            "backend_shutdown_interrupted",
        }:
            raise ValueError("unsupported AutoAgent interruption code")
        transport_failure = False
        with self._lock:
            if (
                self.status in _RUNTIME_TERMINAL_STATUSES
                or self.terminal_commit_failed
                or self._terminal_commit_loop_active
                or self.authority_detached
            ):
                return
            if self.repository is not None:
                try:
                    record = self.repository.complete_autoagent_session_error(
                        self.session_id,
                        status="interrupted",
                        error_code=error_code,
                        error_detail=_DURABLE_ERROR_DETAILS[error_code],
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    transport_failure = True
                else:
                    self.status = record.status
                    self.error = record.error_detail
                    self.error_code = record.error_code
                    self.durable_finished_at_ms = record.finished_at_ms
            else:
                self.status = "interrupted"
                self.error = _DURABLE_ERROR_DETAILS[error_code]
                self.error_code = error_code
            if transport_failure:
                self.result = None
                self.error = "Optimization terminal commit unavailable"
                self.error_code = "repository_failure"
                self.terminal_commit_failed = True
                self._terminal_event = "transport_error"
            else:
                self.finished_at = time.monotonic()
                self._terminal_event = "error"
        if transport_failure:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "transport_error",
                    "data": {"code": "terminal_commit_unavailable"},
                },
            )
        else:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "error",
                    "data": self._terminal_receipt(
                        status="interrupted",
                        error_code=error_code,
                    ),
                },
            )
        self._finish()

    def mark_owner_unconfirmed(self) -> None:
        """Close only the local stream; durable state intentionally stays running."""

        with self._lock:
            if self.status in _RUNTIME_TERMINAL_STATUSES or self.terminal_commit_failed:
                return
            self.result = None
            self.error = "Optimization execution ownership is unconfirmed"
            self.error_code = "repository_failure"
            self.terminal_commit_failed = True
            self._terminal_event = "transport_error"
            self.finished_at = time.monotonic()
        self._submit_to_loop(
            self._enqueue_event,
            {
                "type": "transport_error",
                "data": {"code": "execution_owner_unconfirmed"},
            },
        )
        self._finish()

    def mark_error(
        self,
        message: str,
        emit_event: bool = True,
        *,
        error_code: str = "harness_failed",
    ) -> None:
        detail = _bounded_error_detail(message, fallback="Optimization failed")
        should_emit = False
        transport_failure = False
        with self._lock:
            if (
                self.status in _RUNTIME_TERMINAL_STATUSES
                or self.terminal_commit_failed
                or self._terminal_commit_loop_active
                or self.authority_detached
            ):
                return
            if self.repository is not None:
                try:
                    record = self.repository.complete_autoagent_session_error(
                        self.session_id,
                        status="error",
                        error_code=error_code,
                        error_detail=_DURABLE_ERROR_DETAILS[error_code],
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning(
                        "AutoAgent error persistence failed for %s (%s)",
                        self.session_id,
                        type(exc).__name__,
                    )
                    detail = "Optimization terminal commit unavailable"
                    error_code = "repository_failure"
                    self.terminal_commit_failed = True
                    self._terminal_event = "transport_error"
                    transport_failure = True
                else:
                    self.status = record.status
                    self.error_code = record.error_code
                    self.durable_finished_at_ms = record.finished_at_ms
                    detail = record.error_detail or _DURABLE_ERROR_DETAILS[error_code]
            else:
                self.status = "error"
                self.error_code = error_code
            self.error = detail
            if not transport_failure:
                self.finished_at = time.monotonic()
                if emit_event and self._terminal_event is None:
                    self._terminal_event = "error"
                    should_emit = True
        if transport_failure:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "transport_error",
                    "data": {"code": "terminal_commit_unavailable"},
                },
            )
        if should_emit:
            self._submit_to_loop(
                self._enqueue_event,
                {
                    "type": "error",
                    "data": self._terminal_receipt(
                        status="error",
                        error_code=error_code,
                    ),
                },
            )
        self._finish()

    def snapshot(self) -> tuple[str, dict[str, Any] | None, str | None]:
        with self._lock:
            return self.status, self.result, self.error

    def detach_repository(self, repository: ControlStateRepository) -> bool:
        """Drop one exact Repository reference after the worker shutdown fence."""

        with self._lock:
            if self.repository is not repository:
                return False
            self.repository = None
            self.authority_detached = True
            return True

    def freeze_cancelled(self, record: AutoAgentSessionRecord) -> None:
        """Project one already-committed shutdown cancellation locally."""

        with self._lock:
            if record.status != "cancelled":
                raise ValueError("AutoAgent shutdown record is not cancelled")
            self.status = record.status
            self.result = None
            self.error = record.error_detail
            self.error_code = record.error_code
            self.durable_finished_at_ms = record.finished_at_ms
            self.finished_at = time.monotonic()

    def freeze_interrupted(self, record: AutoAgentSessionRecord) -> None:
        """Freeze an orphan Runtime after durable shutdown reconciliation."""

        with self._lock:
            if record.status != "interrupted":
                raise ValueError("AutoAgent shutdown record is not interrupted")
            self.status = record.status
            self.result = None
            self.error = record.error_detail
            self.error_code = record.error_code
            self.durable_finished_at_ms = record.finished_at_ms
            self.finished_at = time.monotonic()

    def _finish(self) -> None:
        self._submit_to_loop(self._enqueue_finished)

    def _submit_to_loop(self, callback: Any, *args: Any) -> None:
        """Schedule queue mutation onto the owning event loop thread."""
        try:
            self.loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            logger.warning(
                "Optimize session %s dropped an event because the event loop is closed",
                self.session_id,
            )

    def _enqueue_finished(self) -> None:
        with self._lock:
            if self._finished_enqueued:
                return
            self._finished_enqueued = True
        self._enqueue_event({"type": "_finished", "data": {}})

    def _enqueue_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        terminal = event_type in {"done", "error", "transport_error", "_finished"}
        if not terminal and self.queue.qsize() >= _SESSION_EVENT_PROGRESS_LIMIT:
            logger.debug(
                "Optimize event queue dropped progress for session %s",
                self.session_id,
            )
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            if not terminal:
                return
            # Two slots are reserved for terminal + _finished.  This branch is
            # only a defensive recovery for direct test/manual queue mutation.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.error(
                    "Optimize terminal event queue unavailable for session %s",
                    self.session_id,
                )


@dataclass(frozen=True, slots=True)
class OptimizeStartAuthority:
    """Immutable save authority captured before the optimization worker starts."""

    cwd: str
    output_dir: str
    skill: str
    method: str
    evolution_goal: str

    @classmethod
    def capture(
        cls,
        req: "OptimizeRequest",
        *,
        output_dir: str,
    ) -> "OptimizeStartAuthority":
        raw_cwd = req.cwd
        bound_cwd = ""
        if raw_cwd.strip():
            bound_cwd = os.path.abspath(os.fspath(Path(raw_cwd).expanduser()))
        return cls(
            cwd=bound_cwd,
            output_dir=output_dir,
            skill=str(req.skill),
            method=str(req.method),
            evolution_goal=str(req.evolution_goal),
        )


@dataclass(frozen=True, slots=True)
class _PersistedOptimizeSession:
    session_id: str
    start_authority: OptimizeStartAuthority
    status: str
    result: dict[str, Any] | None
    error: str | None
    durable_finished_at_ms: int | None

    def snapshot(self) -> tuple[str, dict[str, Any] | None, str | None]:
        return self.status, self.result, self.error


def _persisted_session(record: AutoAgentSessionRecord) -> _PersistedOptimizeSession:
    result = _thaw_json(record.result) if record.result is not None else None
    if result is not None and not isinstance(result, dict):  # pragma: no cover
        raise RuntimeError("Durable AutoAgent result is not an object")
    return _PersistedOptimizeSession(
        session_id=record.session_id,
        start_authority=OptimizeStartAuthority(
            cwd=record.cwd,
            output_dir=record.output_dir,
            skill=record.skill,
            method=record.method,
            evolution_goal=record.evolution_goal,
        ),
        status=record.status,
        result=result,
        error=record.error_detail,
        durable_finished_at_ms=record.finished_at_ms,
    )


def _lookup_session(
    session_id: str,
) -> OptimizeSessionRuntime | _PersistedOptimizeSession | Any | None:
    runtime = _sessions.get(session_id)
    if runtime is not None:
        return runtime
    repository = _require_autoagent_repository()
    try:
        return _persisted_session(repository.get_autoagent_session(session_id))
    except KeyError:
        return None


def _resolve_session_id(candidate: str | None) -> str:
    if candidate in (None, ""):
        return uuid.uuid4().hex
    if (
        not isinstance(candidate, str)
        or _EXTERNAL_SESSION_ID_RE.fullmatch(candidate) is None
    ):
        raise HTTPException(422, detail="Invalid AutoAgent session_id")
    return candidate


def _require_external_session_id(candidate: str) -> str:
    if (
        not isinstance(candidate, str)
        or _EXTERNAL_SESSION_ID_RE.fullmatch(candidate) is None
    ):
        raise HTTPException(422, detail="Invalid AutoAgent session_id")
    return candidate


def _reap_finished_sessions() -> int:
    """Remove terminal process-local cache entries older than the TTL.

    Durable lifecycle and result authority remains in ``control.db``. Called
    opportunistically so no background task is needed.
    """
    now = time.monotonic()
    to_remove: list[str] = []
    for sid, rt in _sessions.items():
        if rt.finished_at > 0 and (now - rt.finished_at) > _SESSION_TTL_SECONDS:
            to_remove.append(sid)
    for sid in to_remove:
        _sessions.pop(sid, None)
    if to_remove:
        logger.debug("Reaped %d finished optimize session(s)", len(to_remove))
    return len(to_remove)


def _quarantine_autoagent_session(session_id: str) -> None:
    global _autoagent_unconfirmed_owner_session_ids
    with _repository_lock:
        _autoagent_unconfirmed_owner_session_ids = tuple(
            sorted({*_autoagent_unconfirmed_owner_session_ids, session_id})
        )


async def _commit_autoagent_terminal_with_retry(
    runtime: OptimizeSessionRuntime,
    intent: _AutoAgentTerminalIntent,
) -> None:
    """Keep one stopped owner live until its exact terminal is durable."""

    repository = runtime.repository
    if repository is None:
        if intent.status == "done":
            assert intent.result is not None
            runtime.mark_done(intent.result)
        elif intent.status == "cancelled":
            runtime.mark_cancelled()
        elif intent.status == "interrupted":
            assert intent.error_code is not None
            runtime.mark_interrupted(error_code=intent.error_code)
        else:
            runtime.mark_error(
                _DURABLE_ERROR_DETAILS[intent.error_code or "harness_failed"],
                error_code=intent.error_code or "harness_failed",
            )
        return
    runtime.claim_terminal_commit_loop()
    delay_seconds = _AUTOAGENT_TERMINAL_COMMIT_RETRY_INITIAL_SECONDS
    try:
        while True:
            try:
                if intent.status == "done":
                    assert intent.result is not None
                    record = repository.complete_autoagent_session_success(
                        runtime.session_id,
                        intent.result,
                    )
                else:
                    error_code = intent.error_code or "harness_failed"
                    record = repository.complete_autoagent_session_error(
                        runtime.session_id,
                        status=intent.status,
                        error_code=error_code,
                        error_detail=_DURABLE_ERROR_DETAILS[error_code],
                    )
            except AutoAgentCapacityError:
                if intent.status == "done":
                    intent = _AutoAgentTerminalIntent(
                        status="error",
                        error_code="result_capacity_exhausted",
                    )
                    continue
                runtime.note_terminal_commit_pending()
            except (TypeError, ValueError):
                if intent.status == "done":
                    intent = _AutoAgentTerminalIntent(
                        status="error",
                        error_code="invalid_terminal_result",
                    )
                    continue
                runtime.note_terminal_commit_pending()
            except sqlite3.ProgrammingError:
                raise
            except (sqlite3.Error, OSError, RuntimeError):
                runtime.note_terminal_commit_pending()
            else:
                runtime.publish_committed_terminal(record)
                return

            if runtime.shutdown_requested:
                return
            await runtime.wait_for_terminal_commit_retry(delay_seconds)
            if runtime.shutdown_requested:
                return
            delay_seconds = min(
                _AUTOAGENT_TERMINAL_COMMIT_RETRY_MAX_SECONDS,
                delay_seconds * 2,
            )
    finally:
        runtime.release_terminal_commit_loop()


async def _run_governed_optimization_session(
    runtime: OptimizeSessionRuntime,
) -> None:
    """Project child evidence only after exact owner absence is durable."""

    owner = runtime.owner
    repository = runtime.repository
    if owner is None or repository is None:
        runtime.mark_owner_unconfirmed()
        return

    try:
        if runtime.cancel_event.is_set():
            evidence = await reconcile_governed_worker(
                owner.execution_reference_type,
                owner.execution_reference,
                ipc_root=owner.ipc_root,
                session_id=runtime.session_id,
            )
            owner.process_tree_confirmed_empty = True
            outcome = GovernedWorkerOutcome(
                "cancelled",
                error_code="cancelled",
            )
        else:
            outcome = await owner.run(on_event=runtime.emit)
            if not owner.process_tree_confirmed_empty:
                raise GovernedWorkerStopUnconfirmed(
                    "AutoAgent owner returned without process-tree proof"
                )
            evidence = OWNER_STOP_EVIDENCE_CODE
        repository.confirm_autoagent_owner_stopped(
            runtime.session_id,
            evidence_code=evidence,
        )
    except asyncio.CancelledError:
        owner.request_cancel()
        try:
            evidence = await reconcile_governed_worker(
                owner.execution_reference_type,
                owner.execution_reference,
                ipc_root=owner.ipc_root,
                session_id=runtime.session_id,
            )
            repository.confirm_autoagent_owner_stopped(
                runtime.session_id,
                evidence_code=evidence,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            _quarantine_autoagent_session(runtime.session_id)
            runtime.mark_owner_unconfirmed()
        else:
            await _commit_autoagent_terminal_with_retry(
                runtime,
                _AutoAgentTerminalIntent(
                    status="interrupted",
                    error_code="backend_shutdown_interrupted",
                ),
            )
        return
    except GovernedWorkerUnavailable:
        try:
            evidence = await reconcile_governed_worker(
                owner.execution_reference_type,
                owner.execution_reference,
                ipc_root=owner.ipc_root,
                session_id=runtime.session_id,
            )
            repository.confirm_autoagent_owner_stopped(
                runtime.session_id,
                evidence_code=evidence,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            _quarantine_autoagent_session(runtime.session_id)
            runtime.mark_owner_unconfirmed()
        else:
            await _commit_autoagent_terminal_with_retry(
                runtime,
                _AutoAgentTerminalIntent(
                    status="error",
                    error_code="worker_start_failed",
                ),
            )
        return
    except (OSError, RuntimeError, TypeError, ValueError):
        _quarantine_autoagent_session(runtime.session_id)
        runtime.mark_owner_unconfirmed()
        return

    if outcome.status == "done":
        assert outcome.result is not None
        intent = _bounded_autoagent_success_intent(outcome.result)
        await _commit_autoagent_terminal_with_retry(runtime, intent)
        return
    if outcome.status == "cancelled":
        if runtime.shutdown_requested:
            intent = _AutoAgentTerminalIntent(
                status="interrupted",
                error_code="backend_shutdown_interrupted",
            )
        else:
            intent = _AutoAgentTerminalIntent(
                status="cancelled",
                error_code="cancelled",
            )
        await _commit_autoagent_terminal_with_retry(runtime, intent)
        return
    error_code = outcome.error_code or "worker_crashed"
    if error_code not in {
        "harness_failed",
        "invalid_terminal_result",
        "worker_crashed",
        "worker_start_failed",
    }:
        error_code = "worker_crashed"
    await _commit_autoagent_terminal_with_retry(
        runtime,
        _AutoAgentTerminalIntent(
            status="error",
            error_code=error_code,
        ),
    )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: str = Field(default="", max_length=128)
    api_key: SecretStr = SecretStr("")
    base_url: str = Field(default="", max_length=4_096)
    model: str = Field(default="", max_length=256)

    def to_llm_config(self) -> dict[str, str]:
        """Export as plain dict with the api_key revealed (for internal use)."""
        return {
            "provider": self.provider,
            "api_key": self.api_key.get_secret_value(),
            "base_url": self.base_url,
            "model": self.model,
        }


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(
        default="",
        max_length=32,
        pattern=rf"^(?:{_EXTERNAL_SESSION_ID_PATTERN})?$",
        strict=True,
    )
    creation_receipt: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
        exclude=True,
    )
    skill: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=256)
    input_path: str = Field(default="", max_length=4_096)
    cwd: str = Field(default="", max_length=4_096)
    output_dir: str = Field(default="", max_length=4_096)
    max_iterations: int = Field(default=10, ge=1, le=100)
    max_trials: int | None = Field(default=None, exclude=True)  # deprecated alias
    fixed_params: dict[str, Any] = Field(default_factory=dict, max_length=4_096)
    evolution_goal: str = Field(default="", max_length=16_384)
    surface_level: int = Field(default=2, ge=1, le=4)
    explicit_files: list[str] = Field(default_factory=list, max_length=256)
    # Source mutation is allowed only after the successful result is durable;
    # callers must use the journal-backed manual /promote command.
    auto_promote: Literal[False] = False
    provider: str = Field(default="", max_length=128)  # legacy fallback
    provider_id: str = Field(default="", max_length=128)
    provider_config: ProviderConfig | None = None
    llm_model: str = Field(default="", max_length=256)
    demo: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.max_trials is not None:
            logger.warning(
                "Deprecated field 'max_trials=%d' received; "
                "use 'max_iterations' instead. Mapping to max_iterations.",
                self.max_trials,
            )
            # Only apply if max_iterations was left at the default
            if self.max_iterations == 10:
                self.max_iterations = min(max(self.max_trials, 1), 100)
        _validate_optimize_request_bounds(self)


def _validate_optimize_request_bounds(request: OptimizeRequest) -> None:
    """Reject oversized/non-finite worker payloads before any authority write."""

    node_count = 0

    def validate(value: Any, *, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > 20_000 or depth > 8:
            raise ValueError("Optimize request exceeds structural bounds")
        if value is None or isinstance(value, bool | int):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Optimize request must contain finite JSON")
            return
        if isinstance(value, str):
            if len(value) > 65_536 or "\x00" in value:
                raise ValueError("Optimize request string is not bounded")
            return
        if isinstance(value, Mapping):
            if len(value) > 4_096:
                raise ValueError("Optimize request mapping is not bounded")
            for key, item in value.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 256
                    or "\x00" in key
                ):
                    raise ValueError("Optimize request keys are not bounded")
                validate(item, depth=depth + 1)
            return
        if isinstance(value, (list, tuple)):
            if len(value) > 4_096:
                raise ValueError("Optimize request list is not bounded")
            for item in value:
                validate(item, depth=depth + 1)
            return
        raise ValueError("Optimize request must contain finite JSON")

    validate(request.fixed_params, depth=0)
    for path in request.explicit_files:
        if not path or len(path) > 4_096 or "\x00" in path:
            raise ValueError("Optimize request file references are not bounded")
    for value in (
        request.skill,
        request.method,
        request.input_path,
        request.cwd,
        request.output_dir,
        request.evolution_goal,
        request.provider,
        request.provider_id,
        request.llm_model,
    ):
        if "\x00" in value:
            raise ValueError("Optimize request text must not contain NUL")
    secret_length = 0
    if request.provider_config is not None:
        for value in (
            request.provider_config.provider,
            request.provider_config.base_url,
            request.provider_config.model,
        ):
            if "\x00" in value:
                raise ValueError("Optimize provider configuration contains NUL")
        secret = request.provider_config.api_key.get_secret_value()
        if len(secret) > 16_384 or "\x00" in secret:
            raise ValueError("Optimize provider credential is not bounded")
        secret_length = len(secret.encode("utf-8"))
    try:
        encoded = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Optimize request must contain finite JSON") from exc
    if len(encoded) + secret_length > 1_000_000:
        raise ValueError("Optimize request exceeds its serialized byte bound")


def _resolve_governed_worker_provider(req: OptimizeRequest) -> dict[str, str]:
    """Resolve the App-shaped provider request in the parent process only."""

    from omicsclaw.providers.runtime import (
        provider_requires_api_key,
        resolve_provider_runtime,
    )

    explicit = (
        req.provider_config.to_llm_config() if req.provider_config is not None else {}
    )
    runtime = resolve_provider_runtime(
        provider=str(explicit.get("provider") or req.provider_id or req.provider or ""),
        base_url=str(explicit.get("base_url") or ""),
        model=str(explicit.get("model") or req.llm_model or ""),
        api_key=str(explicit.get("api_key") or ""),
    )
    values = {
        "provider": runtime.provider,
        "base_url": runtime.base_url,
        "model": runtime.model,
        "api_key": runtime.api_key,
    }
    bounds = {
        "provider": 128,
        "base_url": 4_096,
        "model": 256,
        "api_key": 16_384,
    }
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > bounds[key] or "\x00" in value:
            raise ValueError("resolved AutoAgent provider is not bounded")
    if not values["model"]:
        raise ValueError("resolved AutoAgent provider has no model")
    if provider_requires_api_key(values["provider"]) and not values["api_key"]:
        raise ValueError("resolved AutoAgent provider has no credential")
    return values


def _governed_worker_request(
    req: OptimizeRequest,
    *,
    authority: "OptimizeStartAuthority",
    session_id: str,
    provider: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "skill_name": authority.skill,
        "method": authority.method,
        "input_path": req.input_path,
        "cwd": authority.cwd,
        "output_dir": authority.output_dir,
        "output_claim_id": session_id,
        "max_iterations": req.max_iterations,
        "fixed_params": dict(req.fixed_params),
        "evolution_goal": authority.evolution_goal,
        "surface_level": req.surface_level,
        "explicit_files": list(req.explicit_files),
        "auto_promote": False,
        "llm_provider": provider["provider"],
        "llm_model": provider["model"],
        "llm_provider_config": dict(provider),
        "demo": req.demo,
    }


class OptimizeStatusResponse(BaseModel):
    session_id: str
    status: (
        str  # "running" | "cancelling" | "cancelled" | "done" | "error" | "not_found"
    )
    result: dict[str, Any] | None = None
    error: str | None = None


class SaveConfigRequest(BaseModel):
    """Opaque reference to one Backend-owned completed optimization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=rf"^{_EXTERNAL_SESSION_ID_PATTERN}$",
    )


class ReconcileAutoAgentRequest(BaseModel):
    """Caller-held crash receipt; the raw value is never persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    creation_receipt: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------


def _governed_worker_capability() -> int:
    if not _GOVERNED_WORKER_INTEGRATED:
        return 0
    with _repository_lock:
        if (
            _autoagent_repository is None
            or _autoagent_unconfirmed_owner_session_ids
            or _autoagent_terminal_commit_pending_session_ids
        ):
            return 0
    try:
        from omicsclaw.autoagent.process_owner import governed_worker_available

        return int(bool(governed_worker_available()))
    except (ImportError, RuntimeError):
        return 0


def _resolve_autoagent_cwd_authority(raw_cwd: str) -> str:
    import stat

    from omicsclaw.common.output_claim import (
        first_filesystem_alias_component,
        stat_is_filesystem_alias,
    )

    candidate = Path(raw_cwd).expanduser() if raw_cwd else Path.cwd()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if first_filesystem_alias_component(candidate) is not None:
        raise ValueError("AutoAgent cwd contains a filesystem alias")
    resolved = candidate.resolve(strict=True)
    metadata = os.lstat(resolved)
    if stat_is_filesystem_alias(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("AutoAgent cwd must be a plain directory")
    return os.fspath(resolved)


def _clean_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _build_optimizable_methods(
    skill_name: str,
    param_hints: object,
) -> list[dict[str, Any]]:
    if not isinstance(param_hints, Mapping):
        return []

    methods: list[dict[str, Any]] = []
    for method_name, hints in param_hints.items():
        if not isinstance(hints, Mapping):
            continue
        surface = build_method_surface(skill_name, str(method_name).strip(), hints)
        if not surface.tunable:
            continue
        methods.append(
            {
                "name": surface.method,
                "params": [param.name for param in surface.tunable],
                "defaults": {param.name: param.default for param in surface.tunable},
                "tips": [param.tip for param in surface.tunable if param.tip],
                "fixed_params": [param.to_dict() for param in surface.fixed],
            }
        )
    return methods


def _collect_skill_aliases(
    registry: Any,
    canonical_skill: str,
) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for alias, info in getattr(registry, "skills", {}).items():
        resolved = str(info.get("alias", alias))
        if resolved != canonical_skill or alias == canonical_skill or alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _close_unstarted_autoagent_session(
    repository: ControlStateRepository,
    *,
    session_id: str,
    execution_reference_type: str,
    execution_reference: str,
    ipc_root: Path,
    status: Literal["error", "cancelled"],
    error_code: str,
) -> AutoAgentSessionRecord:
    """Close a persisted pre-spawn owner only after exact absence proof."""

    try:
        evidence = await reconcile_governed_worker(
            execution_reference_type,
            execution_reference,
            ipc_root=ipc_root,
            session_id=session_id,
        )
        repository.confirm_autoagent_owner_stopped(
            session_id,
            evidence_code=evidence,
        )
        return repository.complete_autoagent_session_error(
            session_id,
            status=status,
            error_code=error_code,
            error_detail=_DURABLE_ERROR_DETAILS[error_code],
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _quarantine_autoagent_session(session_id)
        raise GovernedWorkerStopUnconfirmed(
            "unstarted AutoAgent owner absence could not be committed"
        ) from exc


@router.get("/capabilities")
async def autoagent_capabilities() -> dict[str, int | str]:
    """Return one closed, side-effect-free durability capability contract."""

    repository = _require_autoagent_repository()
    return {
        "schema_version": 1,
        "control_authority_id": repository.control_authority_id,
        "durable_session": 1,
        "creation_receipt": 1,
        "preaccept_cancel": 1,
        "terminal_event": 1,
        "governed_worker": _governed_worker_capability(),
    }


@router.post("/start")
async def optimize_start(req: OptimizeRequest):
    """Start a harness evolution run.

    Returns an SSE stream with events:
    - trial_start, trial_complete, trial_judgment, reasoning, progress, done, error
    """
    repository = _require_autoagent_repository()
    with _repository_lock:
        unconfirmed_owner_ids = _autoagent_unconfirmed_owner_session_ids
        pending_terminal_ids = _autoagent_terminal_commit_pending_session_ids
    if unconfirmed_owner_ids or pending_terminal_ids or not governed_worker_available():
        raise HTTPException(
            503,
            detail="AutoAgent governed worker is unavailable",
        )
    _check_rate_limit()
    _reap_finished_sessions()
    session_id = _resolve_session_id(req.session_id)
    try:
        from omicsclaw.autoagent import _resolve_optimization_output_root

        canonical_cwd = _resolve_autoagent_cwd_authority(req.cwd)
        bound_request = req.model_copy(update={"cwd": canonical_cwd})
        resolved_output_dir = os.fspath(
            _resolve_optimization_output_root(
                bound_request.skill,
                bound_request.method,
                cwd=bound_request.cwd,
                output_dir=bound_request.output_dir,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(422, detail="Invalid AutoAgent output authority") from exc
    try:
        provider = _resolve_governed_worker_provider(bound_request)
        ipc_root = prepare_governed_worker_ipc_root(repository.state_root)
        execution_reference_type, execution_reference = new_governed_worker_reference()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            503,
            detail="AutoAgent governed worker preflight was rejected",
        ) from exc
    authority = OptimizeStartAuthority.capture(
        bound_request,
        output_dir=resolved_output_dir,
    )
    try:
        accepted = repository.accept_autoagent_session(
            session_id=session_id,
            cwd=authority.cwd,
            output_dir=authority.output_dir,
            skill=authority.skill,
            method=authority.method,
            evolution_goal=authority.evolution_goal,
            creation_receipt_sha256=(
                _creation_receipt_sha256(req.creation_receipt)
                if req.creation_receipt is not None
                else None
            ),
            execution_reference_type=execution_reference_type,
            execution_reference=execution_reference,
        )
    except KeyError as exc:
        raise HTTPException(
            409,
            detail=f"Session '{session_id}' already exists",
        ) from exc
    except AutoAgentActiveCapacityError as exc:
        raise HTTPException(
            429,
            detail="AutoAgent active session capacity is exhausted",
        ) from exc
    except AutoAgentCapacityError as exc:
        raise HTTPException(
            507,
            detail="AutoAgent durable audit capacity is exhausted",
        ) from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(
            409, detail="AutoAgent start authority was rejected"
        ) from exc

    receipt_headers = (
        {"X-OmicsClaw-AutoAgent-Receipt-Confirmed": "true"}
        if req.creation_receipt is not None
        else {}
    )
    if accepted.status == "cancelled":
        return JSONResponse(
            {"session_id": session_id, "status": "cancelled"},
            status_code=200,
            headers=receipt_headers,
        )

    # Close cancellation already persisted by a concurrent receipt/abort
    # caller before claiming an output directory or constructing a worker.
    latest = repository.get_autoagent_session(session_id)
    if latest.cancel_requested_at_ms is not None:
        try:
            cancelled = await _close_unstarted_autoagent_session(
                repository,
                session_id=session_id,
                execution_reference_type=execution_reference_type,
                execution_reference=execution_reference,
                ipc_root=ipc_root,
                status="cancelled",
                error_code="cancelled",
            )
        except GovernedWorkerStopUnconfirmed as exc:
            raise HTTPException(
                503,
                detail="AutoAgent execution ownership is unconfirmed",
            ) from exc
        return JSONResponse(
            {"session_id": session_id, "status": cancelled.status},
            status_code=200,
            headers=receipt_headers,
        )

    try:
        output_root = preclaim_session_output_root(
            authority.output_dir,
            claim_id=session_id,
        )
        owner = GovernedAutoAgentWorker(
            session_id=session_id,
            execution_reference_type=execution_reference_type,
            execution_reference=execution_reference,
            cwd=Path(authority.cwd),
            writable_output_root=output_root,
            ipc_root=ipc_root,
            request=_governed_worker_request(
                bound_request,
                authority=authority,
                session_id=session_id,
                provider=provider,
            ),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        try:
            await _close_unstarted_autoagent_session(
                repository,
                session_id=session_id,
                execution_reference_type=execution_reference_type,
                execution_reference=execution_reference,
                ipc_root=ipc_root,
                status="error",
                error_code="worker_start_failed",
            )
        except GovernedWorkerStopUnconfirmed as stop_exc:
            raise HTTPException(
                503,
                detail="AutoAgent execution ownership is unconfirmed",
            ) from stop_exc
        raise HTTPException(409, detail="AutoAgent output claim was rejected") from exc

    runtime = OptimizeSessionRuntime(
        session_id=session_id,
        loop=asyncio.get_running_loop(),
        start_authority=authority,
        repository=repository,
        owner=owner,
    )
    # Publish the local cancellation target before the final durable intent
    # read.  A racing abort now either appears in that read or signals this
    # owner before its task gets a chance to spawn the governed process.
    _sessions[session_id] = runtime
    latest = repository.get_autoagent_session(session_id)
    if latest.cancel_requested_at_ms is not None:
        runtime.request_cancel(persist=False)
        try:
            cancelled = await _close_unstarted_autoagent_session(
                repository,
                session_id=session_id,
                execution_reference_type=execution_reference_type,
                execution_reference=execution_reference,
                ipc_root=ipc_root,
                status="cancelled",
                error_code="cancelled",
            )
        except GovernedWorkerStopUnconfirmed as exc:
            runtime.mark_owner_unconfirmed()
            raise HTTPException(
                503,
                detail="AutoAgent execution ownership is unconfirmed",
            ) from exc
        runtime.freeze_cancelled(cancelled)
        _sessions.pop(session_id, None)
        return JSONResponse(
            {"session_id": session_id, "status": cancelled.status},
            status_code=200,
            headers=receipt_headers,
        )

    runtime.worker = asyncio.create_task(
        _run_governed_optimization_session(runtime),
        name=f"autoagent-{session_id}",
    )

    async def event_generator():
        emitted_bytes = 0

        def bounded_frame(event_type: str, data: Mapping[str, Any]) -> bytes:
            nonlocal emitted_bytes
            frame = _render_autoagent_sse_frame(event_type, data)
            if emitted_bytes + len(frame) > _AUTOAGENT_SSE_TOTAL_MAX_BYTES:
                raise AutoAgentSSEBoundsError(
                    "AutoAgent SSE stream exceeds its aggregate byte bound"
                )
            emitted_bytes += len(frame)
            return frame

        # Emit session_id first.
        yield bounded_frame("status", {"session_id": session_id})

        try:
            while True:
                try:
                    event = await asyncio.wait_for(runtime.queue.get(), timeout=600)
                except asyncio.TimeoutError:
                    yield bounded_frame("keep_alive", {})
                    continue

                if event["type"] == "_finished":
                    break

                try:
                    yield bounded_frame(event["type"], event["data"])
                except AutoAgentSSEBoundsError:
                    # Progress observation is non-authoritative. Close this
                    # stream with a bounded transport receipt; status/results
                    # remain available without cancelling the governed owner.
                    error_frame = _render_autoagent_sse_frame(
                        "transport_error",
                        {"code": "event_stream_bounds_exceeded"},
                    )
                    if (
                        emitted_bytes + len(error_frame)
                        <= _AUTOAGENT_SSE_TOTAL_MAX_BYTES
                    ):
                        emitted_bytes += len(error_frame)
                        yield error_frame
                    break

                if event["type"] in ("done", "error"):
                    break
        except asyncio.CancelledError:
            # The receipt-confirmed stream is only an observer. Transport
            # cancellation must not become an execution cancellation; only
            # the explicit abort Interfaces own that lifecycle transition.
            raise

    response_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    if req.creation_receipt is not None:
        response_headers["X-OmicsClaw-AutoAgent-Receipt-Confirmed"] = "true"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=response_headers,
    )


@router.post("/abort-receipt/{session_id}")
async def abort_autoagent_receipt(
    session_id: str,
    req: ReconcileAutoAgentRequest,
) -> JSONResponse:
    """Persist an exact crash-recovery cancellation before or after accept."""

    _resolve_session_id(session_id)
    repository = _require_autoagent_repository()
    try:
        outcome = repository.request_autoagent_cancellation(
            session_id=session_id,
            creation_receipt_sha256=_creation_receipt_sha256(req.creation_receipt),
        )
    except AutoAgentCapacityError as exc:
        raise HTTPException(
            507,
            detail="AutoAgent durable audit capacity is exhausted",
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(409, detail="AutoAgent creation receipt mismatch") from exc

    if outcome.status == "cancel_requested":
        runtime = _sessions.get(session_id)
        if runtime is not None:
            runtime.request_cancel(persist=False)
        status_code = 202
    else:
        status_code = 200
    return JSONResponse(
        {"session_id": session_id, "status": outcome.status},
        status_code=status_code,
    )


@router.post("/reconcile/{session_id}")
async def reconcile_autoagent_session(
    session_id: str,
    req: ReconcileAutoAgentRequest,
):
    """Prove that this Backend accepted one caller-held creation receipt."""

    _require_external_session_id(session_id)
    repository = _require_autoagent_repository()
    try:
        repository.verify_autoagent_creation_receipt(
            session_id,
            _creation_receipt_sha256(req.creation_receipt),
        )
    except KeyError as exc:
        raise HTTPException(404, detail=f"Session '{session_id}' not found") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, detail="AutoAgent creation receipt mismatch") from exc
    return {"session_id": session_id, "status": "accepted"}


@router.get("/status/{session_id}")
async def optimize_status(session_id: str):
    """Check the status of an optimization session."""
    _require_external_session_id(session_id)
    _reap_finished_sessions()
    runtime = _lookup_session(session_id)
    if runtime is None:
        return OptimizeStatusResponse(session_id=session_id, status="not_found")

    if isinstance(runtime, OptimizeSessionRuntime):
        # Observation is side-effect-free for durable lifecycle state, but it
        # may wake one already-owned terminal commit retry after availability
        # is restored. The retry loop still performs the sole DB transition.
        runtime.wake_terminal_commit_retry()
    status, result, error = runtime.snapshot()
    return OptimizeStatusResponse(
        session_id=session_id,
        status=status,
        result=result,
        error=error,
    )


@router.post("/abort/{session_id}")
async def optimize_abort(session_id: str):
    """Abort a running optimization session."""
    _require_external_session_id(session_id)
    _reap_finished_sessions()
    runtime = _sessions.get(session_id)
    if runtime is None:
        persisted = _lookup_session(session_id)
        if persisted is None:
            raise HTTPException(404, detail=f"Session '{session_id}' not found")
        status, _result, _error = persisted.snapshot()
        raise HTTPException(
            409,
            detail=f"Session is not active (current: {status})",
        )

    status = runtime.request_cancel()
    return {"status": status, "session_id": session_id}


@router.get("/results/{session_id}")
async def optimize_results(session_id: str):
    """Get the results of a completed optimization session."""
    _require_external_session_id(session_id)
    _reap_finished_sessions()
    runtime = _lookup_session(session_id)
    if runtime is None:
        raise HTTPException(404, detail=f"No results for session '{session_id}'")

    status, result, error = runtime.snapshot()
    if result is None or status != "done":
        if status in {"cancelled", "cancelling", "interrupted"}:
            raise HTTPException(
                409, detail=error or f"Session '{session_id}' was cancelled"
            )
        if status == "error":
            raise HTTPException(409, detail=error or f"Session '{session_id}' failed")
        raise HTTPException(404, detail=f"No results for session '{session_id}'")
    return result


@router.post("/promote/{session_id}")
async def promote_session(session_id: str):
    """Manually promote accepted patches from sandbox to source tree.

    Only works for completed sessions whose promotion was skipped.
    """
    _require_external_session_id(session_id)
    from pathlib import Path
    import os
    import stat
    from omicsclaw.autoagent import _check_protected_branch
    from omicsclaw.autoagent.harness_workspace import (
        HarnessWorkspace,
    )
    from omicsclaw.common.output_claim import (
        first_filesystem_alias_component,
        stat_is_filesystem_alias,
    )

    project_root = Path(__file__).resolve().parents[2]
    branch_warning = _check_protected_branch(project_root) or ""

    _reap_finished_sessions()
    runtime = _lookup_session(session_id)
    if runtime is None:
        raise HTTPException(404, detail=f"Session '{session_id}' not found")

    status, result, error = runtime.snapshot()
    if status != "done" or result is None:
        raise HTTPException(
            409,
            detail=f"Session must be in 'done' state to promote (current: {status})",
        )

    promotion = result.get("promotion", {})
    if isinstance(promotion, dict) and promotion.get("status") not in ("skipped",):
        raise HTTPException(
            409,
            detail=f"Promotion status is '{promotion.get('status', 'unknown')}'; "
            f"only 'skipped' sessions can be manually promoted.",
        )

    output_dir = runtime.start_authority.output_dir
    if result.get("output_dir") != output_dir:
        raise HTTPException(
            409,
            detail="Session result output does not match its durable authority.",
        )
    try:
        workspace = HarnessWorkspace(project_root, Path(output_dir))
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            409,
            detail=f"Session's AutoAgent output authority is invalid: {exc}",
        ) from exc

    sandbox_repo = workspace.repo_root
    try:
        repo_alias = first_filesystem_alias_component(sandbox_repo)
        repo_stat = os.lstat(sandbox_repo)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            404,
            detail=f"Sandbox repo is unavailable: {exc}",
        ) from exc
    if (
        repo_alias is not None
        or stat_is_filesystem_alias(repo_stat)
        or not stat.S_ISDIR(repo_stat.st_mode)
    ):
        raise HTTPException(409, detail="Sandbox repo is not a plain directory.")
    try:
        workspace.open_existing()
        accepted_patch = workspace.durable_accepted_head_record()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            409,
            detail=f"Durable accepted patch authority is invalid: {exc}",
        ) from exc

    try:
        promo_result = workspace.promote_accepted_state(
            accepted_patch=accepted_patch,
        )
    except Exception as exc:
        raise HTTPException(500, detail=f"Promotion failed: {exc}")

    # Update the session result with new promotion state
    result["promotion"] = promo_result.to_dict()

    response = promo_result.to_dict()
    if branch_warning:
        response["branch_warning"] = branch_warning
    return response


_SAFE_EVOLVED_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _require_safe_evolved_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_EVOLVED_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Session {label} is not a safe identifier")
    return value


def _evolved_config_filename(skill: str, method: str) -> str:
    """Return a bounded, injective-by-digest filename for one method pair.

    A delimiter join such as ``skill_method.json`` aliases distinct valid
    pairs (``a_b`` + ``c`` versus ``a`` + ``b_c``).  Hash the canonical tuple
    instead; the file body remains the human-readable source of both names.
    """

    identity = json.dumps(
        [skill, method],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"evolved-{hashlib.sha256(identity).hexdigest()}.json"


def _require_finite_result_number(
    value: object,
    *,
    label: str,
    allow_none: bool = False,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Session result {label} must be a finite number")
    projected = float(value)
    if not math.isfinite(projected):
        raise ValueError(f"Session result {label} must be a finite number")
    return projected


def _require_bounded_string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 4096:
        raise ValueError(f"Session result {label} must be a bounded string list")
    projected: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
        ):
            raise ValueError(f"Session result {label} must be a bounded string list")
        projected.append(item)
    return projected


def _project_evolved_config(
    runtime: OptimizeSessionRuntime | _PersistedOptimizeSession | Any,
    *,
    requested_session_id: str,
) -> tuple[OptimizeStartAuthority, dict[str, Any]]:
    if runtime.session_id != requested_session_id:
        raise ValueError("Session identity does not match its runtime authority")
    authority = runtime.start_authority
    if authority is None:
        raise ValueError("Session has no retained start authority")

    status, result, _error = runtime.snapshot()
    if status != "done":
        raise ValueError(f"Session must be in 'done' state to save (current: {status})")
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ValueError("Session has no valid successful result")

    skill = _require_safe_evolved_identifier(authority.skill, label="skill")
    method = _require_safe_evolved_identifier(authority.method, label="method")
    expected_identity = {
        "skill": skill,
        "method": method,
        "evolution_goal": authority.evolution_goal,
        "output_dir": authority.output_dir,
    }
    for key, expected in expected_identity.items():
        if result.get(key) != expected:
            raise ValueError(f"Session result {key} does not match start authority")

    patches_accepted = result.get("patches_accepted")
    if (
        isinstance(patches_accepted, bool)
        or not isinstance(patches_accepted, int)
        or patches_accepted < 0
    ):
        raise ValueError(
            "Session result patches_accepted must be a non-negative integer"
        )
    accepted_files = _require_bounded_string_list(
        result.get("accepted_files"),
        label="accepted_files",
    )
    accepted_patch_commits = _require_bounded_string_list(
        result.get("accepted_patch_commits"),
        label="accepted_patch_commits",
    )
    if len(accepted_patch_commits) != patches_accepted:
        raise ValueError(
            "Session result patches_accepted does not match accepted_patch_commits"
        )

    return authority, {
        "skill": skill,
        "method": method,
        "best_score": _require_finite_result_number(
            result.get("best_score"),
            label="best_score",
            allow_none=True,
        ),
        "improvement_pct": _require_finite_result_number(
            result.get("improvement_pct"),
            label="improvement_pct",
        ),
        "patches_accepted": patches_accepted,
        "accepted_files": accepted_files,
        "accepted_patch_commits": accepted_patch_commits,
        "evolution_goal": authority.evolution_goal,
    }


@router.post("/save-config")
async def save_evolved_config(req: SaveConfigRequest):
    """Persist one Backend-derived summary for a completed optimization."""
    from datetime import datetime, timezone

    from omicsclaw.common.output_claim import (
        atomic_write_owned_output_text_beneath,
    )

    _reap_finished_sessions()
    runtime = _lookup_session(req.session_id)
    if runtime is None:
        raise HTTPException(404, detail=f"Session '{req.session_id}' not found")

    try:
        authority, config_data = _project_evolved_config(
            runtime,
            requested_session_id=req.session_id,
        )
        if not authority.cwd:
            raise ValueError("Session has no bound working directory")
        cwd = Path(authority.cwd)
        if not cwd.is_absolute():
            raise ValueError("Session working directory is not absolute")

        durable_finished_at_ms = getattr(runtime, "durable_finished_at_ms", None)
        evolved_at = (
            datetime.fromtimestamp(
                durable_finished_at_ms / 1_000,
                tz=timezone.utc,
            )
            if isinstance(durable_finished_at_ms, int)
            else datetime.now(timezone.utc)
        )
        config_data["evolved_at"] = evolved_at.isoformat()
        filename = _evolved_config_filename(
            config_data["skill"],
            config_data["method"],
        )
        config_path = atomic_write_owned_output_text_beneath(
            cwd,
            relative_parent=(".omicsclaw", "evolved"),
            filename=filename,
            text=json.dumps(
                config_data,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            label="evolved AutoAgent config",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc

    relative_path = f".omicsclaw/evolved/{config_path.name}"
    return {
        "success": True,
        "path": str(config_path),
        "relative_path": relative_path,
    }


@router.get("/skills")
async def optimizable_skills():
    """List all skills that support auto-evolution, with methods and param_hints."""
    from omicsclaw.autoagent.metrics_registry import get_metrics_for_skill

    # Load skill registry for param_hints
    try:
        from omicsclaw.skill.registry import registry

        registry.load_all()
        primary_skills = registry.iter_primary_skills()
    except Exception as exc:
        logger.warning("Failed to load skill registry for optimize catalog: %s", exc)
        return {"skills": [], "total": 0}

    skills: list[dict[str, Any]] = []

    for skill_name, info in sorted(primary_skills, key=lambda item: item[0]):
        methods = _build_optimizable_methods(skill_name, info.get("param_hints", {}))
        if not methods:
            continue

        metrics = get_metrics_for_skill(skill_name)
        if not metrics:
            continue

        # Metric summaries
        metric_items = [
            {
                "name": k,
                "direction": v.direction,
                "weight": v.weight,
                "description": v.description,
            }
            for k, v in metrics.items()
        ]

        skills.append(
            {
                "skill": skill_name,
                "canonical_skill": skill_name,
                "aliases": _collect_skill_aliases(registry, skill_name),
                "description": info.get("description", ""),
                "domain": info.get("domain", ""),
                "methods": methods,
                "metrics": metric_items,
            }
        )

    return {"skills": skills, "total": len(skills)}


@router.get("/branch-status")
async def branch_status():
    """Return the source project's git branch and protection status.

    The frontend must use this instead of checking its own workingDirectory,
    because harness evolution operates on the OmicsClaw source tree — which
    may be a different repo from the user's data project.
    """
    from pathlib import Path
    from omicsclaw.autoagent import _check_protected_branch

    project_root = Path(__file__).resolve().parents[2]
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return {
            "is_repo": False,
            "branch": "",
            "protected": False,
            "reason": "",
            "project_root": str(project_root),
        }

    try:
        import subprocess

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=5,
            env=scrub_internal_control_credentials(os.environ),
        ).stdout.strip()
    except Exception:
        return {
            "is_repo": True,
            "branch": "",
            "protected": False,
            "reason": "",
            "project_root": str(project_root),
        }

    error = _check_protected_branch(project_root)
    return {
        "is_repo": True,
        "branch": branch,
        "protected": error is not None,
        "reason": error or "",
        "project_root": str(project_root),
    }
