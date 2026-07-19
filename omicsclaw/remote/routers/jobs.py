"""Remote Job compatibility Adapter over authoritative canonical Runs.

New scientific submissions are not Jobs.  They are one strict demo-only
``SimpleSkillRunSubmission`` admitted by the Backend-owned ``RunRuntime`` and
projected as ``run-<run_id>`` for the legacy Remote HTTP noun.  A canonical
request never creates ``job.json`` and no observation route can start work.

Disk-backed Jobs are historical read-only compatibility state.  New
``chat_stream`` submissions are rejected because the canonical Desktop
lifespan cannot bind them without creating a second lifecycle authority.
Historical active scientific or chat rows are *projected* as ``interrupted``
and are never replayed from JSON.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from omicsclaw.control.errors import ControlIntegrityError
from omicsclaw.control.models import (
    RunAcceptanceStatus,
    RunObservationPage,
    RunObservationSnapshot,
)
from omicsclaw.control.run_runtime import (
    RunRevisionWaitBackpressure,
    RunRevisionWaitUnavailable,
)
from omicsclaw.remote.routers.env import build_env_doctor_report_payload
from omicsclaw.remote.run_wire import (
    RemoteCanonicalJobSubmissionV1,
    RemoteJobWireError,
    decode_remote_job_submission,
)
from omicsclaw.remote.runtime_binding import (
    get_remote_workspace,
    require_remote_run_runtime,
)
from omicsclaw.remote.schemas import (
    Job,
    JobListResponse,
    JobStatus,
    JobSubmitRequest,
    JobSubmitResponse,
)
from omicsclaw.remote.storage import (
    UnsafeRemoteStorageError,
    open_storage_directory,
    utc_now_iso,
)


router = APIRouter(tags=["remote"])
logger = logging.getLogger(__name__)

_CANONICAL_JOB_ID = re.compile(r"run-([0-9a-f]{32})\Z")
_OPAQUE_ID = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "interrupted"})
_LEGACY_ACTIVE_STATUSES = frozenset({"queued", "running", "cancel_requested"})
_LEGACY_UNRECOVERABLE = "legacy_execution_unrecoverable"
_LEGACY_CHAT_RETIRED = "legacy_chat_job_retired"
_MAX_LEGACY_LIST_INSPECTIONS = 400
_MAX_LEGACY_JOB_BYTES = 1024 * 1024
_MAX_CANONICAL_LIST_PAGES = 4
_MAX_LEGACY_STARTUP_ENTRIES = 2_000


class LegacyJobMigrationError(RuntimeError):
    """Closed startup failure; legacy executable state must remain disabled."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Narrow disk-backed compatibility helpers for migration and historical tests.
# No production Interface creates, binds, or finalizes a chat Job.
# ---------------------------------------------------------------------------


def _legacy_jobs_root(workspace: Path) -> Path:
    """Return the historical root without creating it (GETs stay pure)."""

    return workspace / ".omicsclaw" / "remote" / "jobs"


def _valid_legacy_job_id(job_id: str) -> bool:
    candidate = str(job_id or "")
    path = Path(candidate)
    return bool(
        candidate
        and ":" not in candidate
        and "\\" not in candidate
        and "\x00" not in candidate
        and not path.is_absolute()
        and len(path.parts) == 1
        and path.parts[0] not in {".", ".."}
        and len(candidate) <= 128
    )


def _has_no_symlink_component(path: Path, *, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    try:
        if current.is_symlink():
            return False
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _job_dir(workspace: Path, job_id: str) -> Path:
    if not _valid_legacy_job_id(job_id):
        raise ValueError("unsafe legacy Job ID")
    return _legacy_jobs_root(workspace) / job_id


def _job_path(workspace: Path, job_id: str) -> Path:
    return _job_dir(workspace, job_id) / "job.json"


def _artifact_root(workspace: Path, job_id: str) -> Path:
    return _job_dir(workspace, job_id) / "artifacts"


def _is_chat_stream_job(job: Optional[Job]) -> bool:
    return bool(
        job
        and job.skill == "chat"
        and isinstance(job.params, dict)
        and job.params.get("job_kind") == "chat_stream"
    )


def _workspace_or_503() -> Path:
    """Resolve the lifespan-frozen Workspace without reconciliation writes."""

    workspace = get_remote_workspace()
    if workspace is None:
        raise HTTPException(status_code=503, detail="remote_workspace_unavailable")
    return workspace


def _read_job(workspace: Path, job_id: str) -> Optional[Job]:
    if not _valid_legacy_job_id(job_id):
        return None
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        with open_storage_directory(
            workspace,
            ".omicsclaw",
            "remote",
            "jobs",
            create=False,
        ) as (_root_path, root_fd):
            directory_flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            job_fd = os.open(job_id, directory_flags, dir_fd=root_fd)
            try:
                document_fd = os.open(
                    "job.json",
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=job_fd,
                )
                try:
                    document_stat = os.fstat(document_fd)
                    if (
                        not stat.S_ISREG(document_stat.st_mode)
                        or document_stat.st_nlink != 1
                        or document_stat.st_size > _MAX_LEGACY_JOB_BYTES
                    ):
                        return None
                    with os.fdopen(
                        document_fd,
                        "r",
                        encoding="utf-8",
                        closefd=True,
                    ) as handle:
                        document_fd = -1
                        job = Job.model_validate_json(handle.read())
                finally:
                    if document_fd >= 0:
                        os.close(document_fd)
            finally:
                os.close(job_fd)
    except (FileNotFoundError, OSError, UnsafeRemoteStorageError, ValueError):
        return None
    return job if job.job_id == job_id else None


def _write_job(workspace: Path, job: Job) -> None:
    """Atomically persist one legacy row through an owned directory handle."""

    if not _valid_legacy_job_id(job.job_id):
        raise ValueError("unsafe legacy Job ID")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("secure legacy Job storage unavailable")
    serialized = json.dumps(job.model_dump(), indent=2, sort_keys=True).encode("utf-8")
    if len(serialized) > _MAX_LEGACY_JOB_BYTES:
        raise ValueError("legacy Job document too large")
    temp_name = f".job-{secrets.token_hex(16)}.tmp"
    try:
        with open_storage_directory(
            workspace,
            ".omicsclaw",
            "remote",
            "jobs",
            create=True,
        ) as (_root_path, root_fd):
            try:
                os.mkdir(job.job_id, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            directory_flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            job_fd = os.open(job.job_id, directory_flags, dir_fd=root_fd)
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=job_fd,
                )
                try:
                    with os.fdopen(temp_fd, "wb", closefd=True) as handle:
                        temp_fd = -1
                        handle.write(serialized)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temp_stat = os.stat(
                        temp_name,
                        dir_fd=job_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(temp_stat.st_mode)
                        or temp_stat.st_nlink != 1
                    ):
                        raise ValueError("unsafe legacy Job temporary file")
                    os.replace(
                        temp_name,
                        "job.json",
                        src_dir_fd=job_fd,
                        dst_dir_fd=job_fd,
                    )
                finally:
                    if temp_fd >= 0:
                        os.close(temp_fd)
                    try:
                        os.unlink(temp_name, dir_fd=job_fd)
                    except OSError:
                        pass
            finally:
                os.close(job_fd)
    except (OSError, UnsafeRemoteStorageError) as exc:
        raise ValueError("unsafe legacy Job storage path") from exc


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def append_job_stdout_line(workspace: Path, job_id: str, line: str) -> None:
    """Append chat diagnostics; canonical Run SSE never reads this file."""

    trimmed = line.rstrip()
    if not trimmed:
        return
    stdout_path = _job_dir(workspace, job_id) / "stdout.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{trimmed}\n")


def _finalize_stdout(workspace: Path, job_id: str, stdout_text: str) -> None:
    stdout_path = _job_dir(workspace, job_id) / "stdout.log"
    if stdout_path.is_file() and stdout_path.stat().st_size > 0:
        return
    if stdout_text:
        normalized = stdout_text if stdout_text.endswith("\n") else f"{stdout_text}\n"
        _write_text(stdout_path, normalized)


def _persist_failure_diagnostics(
    workspace: Path,
    job_id: str,
    *,
    stdout_text: str,
) -> str:
    _finalize_stdout(workspace, job_id, stdout_text)
    job_dir = _job_dir(workspace, job_id)
    artifact_root = _artifact_root(workspace, job_id)
    diagnostics_dir = artifact_root / "diagnostics"
    stdout_path = job_dir / "stdout.log"
    diagnostics_content = (
        stdout_path.read_text(encoding="utf-8") if stdout_path.is_file() else ""
    )
    _write_text(diagnostics_dir / "stdout.log", diagnostics_content)
    env_payload = build_env_doctor_report_payload(workspace_dir=str(workspace))
    _write_text(
        diagnostics_dir / "env_doctor.json",
        json.dumps(env_payload.model_dump(), indent=2, sort_keys=True),
    )
    return str(artifact_root.resolve())


def _persist_success_stdout(workspace: Path, job_id: str, *, stdout_text: str) -> str:
    _finalize_stdout(workspace, job_id, stdout_text)
    artifact_root = _artifact_root(workspace, job_id)
    artifact_root.mkdir(parents=True, exist_ok=True)
    return str(artifact_root.resolve())


def bind_chat_stream_job(workspace: Path, job_id: str, *, session_id: str = "") -> Job:
    job = _read_job(workspace, job_id)
    if job is None:
        raise LookupError(f"job not found: {job_id}")
    if not _is_chat_stream_job(job):
        raise ValueError(f"job is not a chat_stream display job: {job_id}")
    if session_id and job.session_id and job.session_id != session_id:
        raise ValueError(
            f"job session mismatch: expected {job.session_id!r}, got {session_id!r}"
        )
    if job.status == "queued":
        running = job.model_copy(
            update={
                "status": "running",
                "started_at": job.started_at or utc_now_iso(),
                "compatibility_kind": "chat_stream",
            }
        )
        _write_job(workspace, running)
        return running
    if job.status in {"running", "canceled"}:
        return job
    raise ValueError(f"job is already terminal: {job.status}")


def finalize_chat_stream_job(
    workspace: Path,
    job_id: str,
    *,
    status: JobStatus,
    error: str | None = None,
) -> Job:
    if status not in {"succeeded", "failed", "canceled"}:
        raise ValueError(
            f"chat stream job must finalize to a terminal status, got {status}"
        )
    job = _read_job(workspace, job_id)
    if job is None:
        raise LookupError(f"job not found: {job_id}")
    if not _is_chat_stream_job(job):
        raise ValueError(f"job is not a chat_stream display job: {job_id}")
    if job.status == "canceled" and status != "canceled":
        return job
    if job.status in _TERMINAL_STATUSES:
        return job

    updates: dict[str, object | None] = {
        "status": status,
        "finished_at": utc_now_iso(),
        "started_at": job.started_at or utc_now_iso(),
        "compatibility_kind": "chat_stream",
    }
    if status == "succeeded":
        updates.update(
            {
                "exit_code": 0,
                "error": None,
                "artifact_root": _persist_success_stdout(
                    workspace, job_id, stdout_text=""
                ),
            }
        )
    elif status == "failed":
        message = error or "chat_stream_failed"
        updates.update(
            {
                "exit_code": 1,
                "error": message,
                "artifact_root": _persist_failure_diagnostics(
                    workspace, job_id, stdout_text=message
                ),
            }
        )
    else:
        updates.update({"error": error or job.error})
    final = job.model_copy(update=updates)
    _write_job(workspace, final)
    return final


# ---------------------------------------------------------------------------
# Canonical Run projection
# ---------------------------------------------------------------------------


def _canonical_run_id(job_id: str) -> str | None:
    """Return the Run ID, reserving the whole ``run-`` compatibility namespace."""

    if not job_id.startswith("run-"):
        return None
    match = _CANONICAL_JOB_ID.fullmatch(job_id)
    if match is None:
        raise HTTPException(404, detail="job_not_found")
    return match.group(1)


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def _runtime_required():
    try:
        return require_remote_run_runtime()
    except RuntimeError as exc:
        raise HTTPException(503, detail="remote_run_runtime_unavailable") from exc


def _canonical_projection(
    runtime,
    observation: RunObservationSnapshot,
    *,
    workspace: Path,
    skill_id: str | None = None,
) -> Job:
    receipt = observation.receipt
    if receipt.scope_kind != "unassigned" or receipt.run_kind != "skill":
        raise HTTPException(404, detail="job_not_found")
    try:
        canonical_skill = skill_id or runtime.get_receipt_skill_id(receipt.run_id)
    except KeyError as exc:
        raise HTTPException(404, detail="job_not_found") from exc
    except Exception as exc:
        raise HTTPException(
            409, detail="run_receipt_projection_integrity_error"
        ) from exc
    assignment = observation.assignment
    status = str(receipt.status)
    if status not in {
        "queued",
        "running",
        "cancel_requested",
        "succeeded",
        "failed",
        "canceled",
        "interrupted",
    }:
        raise HTTPException(409, detail="run_receipt_projection_integrity_error")
    return Job(
        job_id=f"run-{receipt.run_id}",
        session_id="",
        skill=canonical_skill,
        status=status,
        # Canonical Job compatibility responses do not expose Backend-local
        # filesystem locations. The frozen Workspace remains an Adapter input,
        # not wire authority.
        workspace="",
        inputs={"demo": True},
        params={},
        created_at=_iso_from_ms(receipt.created_at_ms) or "",
        started_at=_iso_from_ms(receipt.started_at_ms),
        finished_at=_iso_from_ms(receipt.finished_at_ms),
        exit_code=(
            0
            if status == "succeeded"
            else 1
            if status in {"failed", "interrupted"}
            else None
        ),
        error=None,
        artifact_root=None,
        run_id=receipt.run_id,
        receipt_revision=receipt.revision,
        terminal_code=receipt.terminal_code,
        assignment_id=assignment.assignment_id if assignment is not None else None,
        compatibility_kind="canonical_run",
    )


def _read_canonical_job(runtime, run_id: str, *, workspace: Path) -> Job:
    try:
        observation = runtime.get_receipt(run_id)
    except KeyError as exc:
        raise HTTPException(404, detail="job_not_found") from exc
    except ControlIntegrityError as exc:
        raise HTTPException(409, detail="run_receipt_integrity_error") from exc
    except RuntimeError as exc:
        raise HTTPException(503, detail="remote_run_runtime_unavailable") from exc
    return _canonical_projection(runtime, observation, workspace=workspace)


def _legacy_projection(job: Job) -> Job:
    if _is_chat_stream_job(job):
        if job.status in _LEGACY_ACTIVE_STATUSES:
            return job.model_copy(
                update={
                    "status": "interrupted",
                    "terminal_code": _LEGACY_CHAT_RETIRED,
                    "error": _LEGACY_CHAT_RETIRED,
                    "exit_code": 1,
                    "artifact_root": None,
                    "compatibility_kind": "chat_stream",
                }
            )
        return job.model_copy(update={"compatibility_kind": "chat_stream"})
    if job.status in _LEGACY_ACTIVE_STATUSES:
        return job.model_copy(
            update={
                "status": "interrupted",
                "terminal_code": _LEGACY_UNRECOVERABLE,
                "error": _LEGACY_UNRECOVERABLE,
                "exit_code": 1,
                "artifact_root": None,
                "compatibility_kind": "legacy_job",
            }
        )
    return job.model_copy(update={"compatibility_kind": "legacy_job"})


def terminalize_legacy_active_jobs_at_startup(
    workspace: Path,
    *,
    max_entries: int = _MAX_LEGACY_STARTUP_ENTRIES,
) -> tuple[str, ...]:
    """Durably close historical scientific Jobs without replaying payloads.

    This command is intentionally separate from every GET Interface.  The
    caller invokes it once from the Backend lifespan before Remote traffic is
    admitted.  A bounded preflight gathers the complete directory snapshot
    before the first write; exceeding the configured bound fails closed with
    no partial migration.
    """

    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
        or max_entries > _MAX_LEGACY_STARTUP_ENTRIES
    ):
        raise ValueError(
            f"max_entries must be between 1 and {_MAX_LEGACY_STARTUP_ENTRIES}"
        )
    frozen_workspace = Path(workspace).expanduser().resolve()
    root = _legacy_jobs_root(frozen_workspace)
    if not root.exists():
        return ()
    if (
        root.is_symlink()
        or not root.is_dir()
        or not _has_no_symlink_component(root, root=frozen_workspace)
    ):
        raise LegacyJobMigrationError("legacy_job_migration_unsafe_storage")

    entries: list[Path] = []
    try:
        for entry in root.iterdir():
            if len(entries) >= max_entries:
                raise LegacyJobMigrationError(
                    "legacy_job_migration_scan_limit_exceeded"
                )
            entries.append(entry)
    except LegacyJobMigrationError:
        raise
    except OSError as exc:
        raise LegacyJobMigrationError("legacy_job_migration_scan_failed") from exc

    candidates: list[Job] = []
    for entry in sorted(entries, key=lambda candidate: candidate.name):
        if entry.is_symlink():
            raise LegacyJobMigrationError("legacy_job_migration_unsafe_storage")
        if not entry.is_dir():
            continue
        job = _read_job(frozen_workspace, entry.name)
        if job is None or _is_chat_stream_job(job):
            continue
        if job.status not in _LEGACY_ACTIVE_STATUSES:
            continue
        candidates.append(job)

    migrated: list[str] = []
    for job in candidates:
        interrupted = job.model_copy(
            update={
                "status": "interrupted",
                "terminal_code": _LEGACY_UNRECOVERABLE,
                "error": _LEGACY_UNRECOVERABLE,
                "exit_code": 1,
                "finished_at": utc_now_iso(),
                "artifact_root": None,
                "compatibility_kind": "legacy_job",
            }
        )
        try:
            _write_job(frozen_workspace, interrupted)
        except (OSError, ValueError) as exc:
            raise LegacyJobMigrationError("legacy_job_migration_write_failed") from exc
        migrated.append(job.job_id)
    return tuple(migrated)


def _require_idempotency_key(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1:
        raise HTTPException(422, detail="exactly_one_idempotency_key_required")
    value = values[0]
    if _OPAQUE_ID.fullmatch(value) is None:
        raise HTTPException(422, detail="invalid_idempotency_key")
    return value


def _raise_submission_rejection(status: RunAcceptanceStatus, code: str) -> None:
    detail = code or "run_rejected"
    if status is RunAcceptanceStatus.CONFLICT:
        raise HTTPException(409, detail=detail)
    status_code = {
        "skill_not_found": 404,
        "skill_not_canonical": 422,
        "skill_deprecated": 422,
        "skill_demo_not_supported": 422,
        "run_kind_not_supported": 422,
        "resource_contract_missing": 422,
        "resource_contract_mismatch": 422,
        "resource_unsupported": 422,
        "skill_authority_unavailable": 503,
        "run_backpressure": 429,
        "admission_contention": 429,
        "control_not_ready": 503,
        "executor_isolation_unavailable": 503,
    }.get(detail, 422)
    raise HTTPException(status_code, detail=detail)


# ---------------------------------------------------------------------------
# HTTP Interface
# ---------------------------------------------------------------------------


@router.post(
    "/jobs",
    response_model=JobSubmitResponse,
    status_code=202,
    responses={200: {"model": JobSubmitResponse}},
)
async def submit_job(request: Request, response: Response) -> JobSubmitResponse:
    """Admit canonical demo Runs; reject the retired chat Job write wire."""

    try:
        decoded = await decode_remote_job_submission(request)
    except RemoteJobWireError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from exc

    if isinstance(decoded, JobSubmitRequest):
        raise HTTPException(409, detail="legacy_chat_job_submission_retired")

    assert isinstance(decoded, RemoteCanonicalJobSubmissionV1)
    submission_id = _require_idempotency_key(request)
    _workspace_or_503()
    runtime = _runtime_required()
    try:
        result = await runtime.submit(decoded.to_domain(submission_id))
    except ControlIntegrityError as exc:
        raise HTTPException(409, detail="run_admission_integrity_error") from exc
    except RuntimeError as exc:
        raise HTTPException(503, detail="remote_run_runtime_unavailable") from exc
    if result.acceptance_status not in {
        RunAcceptanceStatus.ACCEPTED,
        RunAcceptanceStatus.DUPLICATE,
    }:
        _raise_submission_rejection(result.acceptance_status, result.code)
    receipt = result.receipt
    if receipt is None:
        raise HTTPException(503, detail="accepted_run_receipt_unavailable")
    duplicate = result.acceptance_status is RunAcceptanceStatus.DUPLICATE
    response.status_code = 200 if duplicate else 202
    response.headers["Location"] = f"/jobs/run-{receipt.run_id}"
    return JobSubmitResponse(
        job_id=f"run-{receipt.run_id}",
        run_id=receipt.run_id,
        status=receipt.status,
        duplicate=duplicate,
        receipt_revision=receipt.revision,
    )


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: Optional[JobStatus] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
) -> JobListResponse:
    """Return a bounded mixed projection; never reconcile or start execution."""

    workspace = _workspace_or_503()
    runtime = _runtime_required()
    rows: list[Job] = []
    page_cursor = cursor
    next_cursor: str | None = None
    for _ in range(_MAX_CANONICAL_LIST_PAGES):
        try:
            page: RunObservationPage = runtime.list_receipts(
                status=status,
                cursor=page_cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(400, detail="invalid_job_cursor") from exc
        except RuntimeError as exc:
            raise HTTPException(503, detail="remote_run_runtime_unavailable") from exc
        for observation in page.observations:
            receipt = observation.receipt
            if receipt.scope_kind != "unassigned" or receipt.run_kind != "skill":
                continue
            row = _canonical_projection(runtime, observation, workspace=workspace)
            if status is None or row.status == status:
                rows.append(row)
            if len(rows) >= limit:
                break
        next_cursor = page.next_cursor
        if len(rows) >= limit or next_cursor is None:
            break
        page_cursor = next_cursor

    root = _legacy_jobs_root(workspace)
    # Historical rows have no canonical keyset identity.  Append their bounded
    # compatibility window only after this canonical page reaches the end, so
    # a Run cursor never causes the same legacy rows to repeat on every page.
    if len(rows) < limit and next_cursor is None and root.is_dir():
        inspected = 0
        try:
            entries = root.iterdir()
            for entry in entries:
                inspected += 1
                if inspected > min(_MAX_LEGACY_LIST_INSPECTIONS, limit * 4):
                    break
                if entry.is_symlink() or not entry.is_dir():
                    continue
                legacy = _read_job(workspace, entry.name)
                if legacy is None:
                    continue
                projected = _legacy_projection(legacy)
                if status is not None and projected.status != status:
                    continue
                rows.append(projected)
                if len(rows) >= limit:
                    break
        except OSError:
            logger.warning("legacy Remote Job directory became unavailable")

    rows.sort(key=lambda item: item.created_at, reverse=True)
    rows = rows[:limit]
    return JobListResponse(
        jobs=rows,
        total=len(rows),
        next_cursor=next_cursor,
    )


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    workspace = _workspace_or_503()
    run_id = _canonical_run_id(job_id)
    if run_id is not None:
        return _read_canonical_job(_runtime_required(), run_id, workspace=workspace)
    job = _read_job(workspace, job_id)
    if job is None:
        raise HTTPException(404, detail="job_not_found")
    return _legacy_projection(job)


@router.post("/jobs/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str) -> Job:
    workspace = _workspace_or_503()
    run_id = _canonical_run_id(job_id)
    if run_id is None:
        job = _read_job(workspace, job_id)
        if job is None:
            raise HTTPException(404, detail="job_not_found")
        detail = (
            "chat_stream_cancel_not_supported"
            if _is_chat_stream_job(job)
            else "legacy_cancel_not_supported"
        )
        raise HTTPException(409, detail=detail)

    runtime = _runtime_required()
    try:
        result = await runtime.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(404, detail="job_not_found") from exc
    except ControlIntegrityError as exc:
        raise HTTPException(409, detail="run_cancel_owner_unconfirmed") from exc
    except RuntimeError as exc:
        raise HTTPException(503, detail="remote_run_runtime_unavailable") from exc
    if not result.changed and result.code not in {
        "already_terminal",
        "cancel_already_requested",
    }:
        raise HTTPException(409, detail=result.code or "run_cancel_rejected")
    return _read_canonical_job(runtime, run_id, workspace=workspace)


@router.post("/jobs/{job_id}/retry", response_model=JobSubmitResponse)
async def retry_job(job_id: str) -> JobSubmitResponse:
    workspace = _workspace_or_503()
    if _canonical_run_id(job_id) is not None:
        raise HTTPException(409, detail="canonical_retry_not_supported")
    original = _read_job(workspace, job_id)
    if original is None:
        raise HTTPException(404, detail="job_not_found")
    raise HTTPException(409, detail="legacy_retry_not_supported")


# ---------------------------------------------------------------------------
# Pure snapshot-first SSE observation
# ---------------------------------------------------------------------------


def _sse_event(event: str, data: dict, *, event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _status_event_name(status: JobStatus) -> str:
    return {
        "queued": "job_queued",
        "running": "job_started",
        "cancel_requested": "job_cancel_requested",
        "succeeded": "job_succeeded",
        "failed": "job_failed",
        "canceled": "job_canceled",
        "interrupted": "job_interrupted",
    }[status]


def _parse_revision_cursor(raw: str | None, *, current_revision: int) -> int:
    if raw is None or raw == "":
        return current_revision
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise HTTPException(400, detail="invalid_job_event_cursor") from exc
    if value < 0 or value > current_revision:
        raise HTTPException(400, detail="invalid_job_event_cursor")
    return value


async def _canonical_job_event_stream(
    runtime,
    workspace: Path,
    initial: RunObservationSnapshot,
) -> AsyncIterator[str]:
    current = initial
    while True:
        try:
            projected = _canonical_projection(runtime, current, workspace=workspace)
        except HTTPException:
            yield _sse_event("error", {"error": "run_receipt_integrity_error"})
            return
        revision = current.receipt.revision
        yield _sse_event(
            _status_event_name(projected.status),
            projected.model_dump(),
            event_id=revision,
        )
        if projected.status in _TERMINAL_STATUSES:
            yield _sse_event(
                "done",
                {"job_id": projected.job_id, "run_id": projected.run_id},
            )
            return
        try:
            next_snapshot = await runtime.wait_for_receipt_revision(
                current.receipt.run_id,
                after_revision=revision,
            )
        except KeyError:
            yield _sse_event("error", {"error": "job_not_found"})
            return
        except ValueError:
            yield _sse_event("error", {"error": "invalid_job_event_cursor"})
            return
        except RunRevisionWaitBackpressure:
            yield _sse_event("error", {"error": "job_observer_capacity_exceeded"})
            return
        except RunRevisionWaitUnavailable:
            yield _sse_event("error", {"error": "job_observer_unavailable"})
            return
        except ControlIntegrityError:
            yield _sse_event("error", {"error": "job_observer_integrity_error"})
            return
        except RuntimeError:
            yield _sse_event("error", {"error": "job_observer_unavailable"})
            return
        if next_snapshot.receipt.revision <= revision:
            yield _sse_event("error", {"error": "job_observer_integrity_error"})
            return
        current = next_snapshot


async def _legacy_job_event_stream(
    workspace: Path,
    job_id: str,
    initial: Job,
) -> AsyncIterator[str]:
    current = _legacy_projection(initial)
    last_state: tuple[str, str | None] | None = None
    while True:
        state = (current.status, current.finished_at)
        if state != last_state:
            yield _sse_event(_status_event_name(current.status), current.model_dump())
            last_state = state
        if current.status in _TERMINAL_STATUSES:
            yield _sse_event("done", {"job_id": job_id})
            return
        await asyncio.sleep(0.05)
        observed = _read_job(workspace, job_id)
        if observed is None:
            yield _sse_event("error", {"error": "job_not_found"})
            return
        current = _legacy_projection(observed)


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    workspace = _workspace_or_503()
    run_id = _canonical_run_id(job_id)
    if run_id is not None:
        runtime = _runtime_required()
        try:
            initial = runtime.get_receipt(run_id)
        except KeyError as exc:
            raise HTTPException(404, detail="job_not_found") from exc
        except ControlIntegrityError as exc:
            raise HTTPException(409, detail="run_receipt_integrity_error") from exc
        except RuntimeError as exc:
            raise HTTPException(503, detail="remote_run_runtime_unavailable") from exc
        _parse_revision_cursor(
            last_event_id,
            current_revision=initial.receipt.revision,
        )
        # Validate the initial Receipt/Manifest projection before HTTP headers
        # are committed. Subsequent drift is rendered as a closed SSE error.
        _canonical_projection(runtime, initial, workspace=workspace)
        stream = _canonical_job_event_stream(runtime, workspace, initial)
    else:
        initial_job = _read_job(workspace, job_id)
        if initial_job is None:
            raise HTTPException(404, detail="job_not_found")
        stream = _legacy_job_event_stream(workspace, job_id, initial_job)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = [
    "LegacyJobMigrationError",
    "append_job_stdout_line",
    "bind_chat_stream_job",
    "finalize_chat_stream_job",
    "router",
    "terminalize_legacy_active_jobs_at_startup",
]
