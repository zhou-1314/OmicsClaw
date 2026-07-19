"""Pure artifact observations for canonical Runs and terminal legacy Jobs.

``run-<run_id>`` is a reserved compatibility namespace.  Canonical artifacts
are projected only through the Backend ``RunRuntime`` after Receipt,
Assignment, Manifest completion and the complete immutable inventory have
been verified.  The Runtime returns an already-open reader, so this router
never reopens a verified path and cannot cross a verify-to-stream TOCTOU seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import mimetypes
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
)
from omicsclaw.control import (
    RunArtifactNotFound,
    RunArtifactProjectionIntegrityError,
    RunArtifactReadBackpressure,
    RunArtifactsUnavailable,
)
from omicsclaw.remote.runtime_binding import (
    get_remote_run_runtime,
    get_remote_workspace,
)
from omicsclaw.remote.schemas import Artifact, ArtifactListResponse, Job
from omicsclaw.remote.storage import REMOTE_SUBDIR, path_modified_at_iso


router = APIRouter(tags=["remote"])

_CANONICAL_JOB_RE = re.compile(r"run-([0-9a-f]{32})\Z")
_TERMINAL_LEGACY_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_MAX_LEGACY_ARTIFACT_SCAN = 1_000
_STREAM_CHUNK_BYTES = 256 * 1024


def _workspace_or_503() -> Path:
    workspace = get_remote_workspace()
    if workspace is None:
        raise HTTPException(status_code=503, detail="remote_workspace_unavailable")
    return workspace


def _runtime_or_503():
    runtime = get_remote_run_runtime()
    if runtime is None or not runtime.lifecycle_ready:
        raise HTTPException(503, detail="remote_run_runtime_unavailable")
    return runtime


def _artifact_id(job_id: str, relative: Path | PurePosixPath) -> str:
    return f"{job_id}:{relative.as_posix()}"


def _validate_job_id(job_id: str) -> str:
    raw_candidate = str(job_id or "")
    candidate = raw_candidate.strip()
    path = Path(candidate)
    if (
        not candidate
        or candidate != raw_candidate
        or len(candidate) > 128
        or ":" in candidate
        or "\\" in candidate
        or "\x00" in candidate
        or path.is_absolute()
        or len(path.parts) != 1
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise HTTPException(status_code=400, detail="unsafe_job_id")
    return candidate


def _canonical_run_id(job_id: str) -> str | None:
    candidate = _validate_job_id(job_id)
    match = _CANONICAL_JOB_RE.fullmatch(candidate)
    if match is not None:
        return match.group(1)
    if candidate.startswith("run-"):
        # The canonical prefix is reserved and never falls back to a legacy
        # directory, including when its opaque identifier is malformed.
        raise HTTPException(400, detail="invalid_canonical_job_id")
    return None


def _validate_relative_path(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or ":" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise HTTPException(400, detail="unsafe_artifact_path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise HTTPException(400, detail="unsafe_artifact_path")
    return relative


def _split_artifact_id(artifact_id: str) -> tuple[str, PurePosixPath]:
    if ":" not in artifact_id:
        raise HTTPException(400, detail="invalid_artifact_id")
    job_id, _, raw_relative = artifact_id.partition(":")
    return _validate_job_id(job_id), _validate_relative_path(raw_relative)


def _artifacts_dir(workspace: Path, job_id: str) -> Path:
    root = workspace / REMOTE_SUBDIR / "jobs"
    target = root / _validate_job_id(job_id) / "artifacts"
    try:
        target.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, detail="unsafe_job_id") from exc
    return target


def _resolve_artifact_target(base: Path, rel: Path | PurePosixPath) -> Path:
    try:
        target = (base / Path(*rel.parts)).resolve()
        target.relative_to(base.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, detail="unsafe_artifact_path") from exc
    return target


def _has_no_symlink_component(path: Path, *, root: Path) -> bool:
    candidate = Path(path)
    boundary = Path(root)
    try:
        relative = candidate.relative_to(boundary)
    except ValueError:
        return False
    current = boundary
    try:
        if current.is_symlink():
            return False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _read_terminal_legacy_job(workspace: Path, job_id: str) -> Job | None:
    path = workspace / REMOTE_SUBDIR / "jobs" / _validate_job_id(job_id) / "job.json"
    if not _has_no_symlink_component(path, root=workspace) or not path.is_file():
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        job = Job.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if job.job_id != job_id or job.status not in _TERMINAL_LEGACY_STATUSES:
        return None
    return job


def _iso_from_ms(value: int | None) -> str:
    timestamp = max(0, int(value or 0)) / 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _canonical_observation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (RunArtifactNotFound, KeyError, FileNotFoundError)):
        return HTTPException(404, detail="canonical_artifact_not_found")
    if isinstance(exc, RunArtifactReadBackpressure):
        return HTTPException(429, detail="artifact_observation_backpressure")
    if isinstance(exc, RunArtifactsUnavailable):
        if exc.code == "runtime_closed":
            return HTTPException(503, detail="remote_run_runtime_unavailable")
        return HTTPException(409, detail="canonical_artifacts_unavailable")
    if isinstance(exc, RunArtifactProjectionIntegrityError):
        return HTTPException(409, detail="artifact_integrity_error")
    if isinstance(exc, RuntimeError):
        return HTTPException(503, detail="remote_run_runtime_unavailable")
    return HTTPException(409, detail="artifact_integrity_error")


async def _list_canonical_artifacts(
    *,
    job_id: str,
    run_id: str,
    cursor: str | None,
    limit: int,
) -> ArtifactListResponse:
    runtime = _runtime_or_503()
    try:
        page = await runtime.list_verified_artifacts(
            run_id,
            cursor=cursor,
            limit=limit,
        )
    except asyncio.CancelledError:
        raise
    except ValueError:
        raise HTTPException(400, detail="invalid_artifact_cursor") from None
    except Exception as exc:
        raise _canonical_observation_error(exc) from None
    created_at = _iso_from_ms(page.receipt.finished_at_ms)
    rows = [
        Artifact(
            artifact_id=_artifact_id(job_id, PurePosixPath(item.relative_path)),
            job_id=job_id,
            run_id=run_id,
            relative_path=item.relative_path,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            mime_type=item.media_type,
            created_at=created_at,
        )
        for item in page.artifacts
    ]
    return ArtifactListResponse(
        artifacts=rows,
        total=int(getattr(page, "total", len(rows))),
        next_cursor=page.next_cursor,
    )


def _list_terminal_legacy_artifacts(
    *,
    workspace: Path,
    job_id: str,
    limit: int,
) -> ArtifactListResponse:
    if _read_terminal_legacy_job(workspace, job_id) is None:
        return ArtifactListResponse(artifacts=[], total=0)
    base = _artifacts_dir(workspace, job_id)
    if not base.is_dir() or not _has_no_symlink_component(base, root=workspace):
        return ArtifactListResponse(artifacts=[], total=0)
    claim_identities = collect_output_claim_identities(base)
    rows: list[Artifact] = []
    examined = 0
    candidates: list[Path] = []
    try:
        for path in base.rglob("*"):
            examined += 1
            if examined > _MAX_LEGACY_ARTIFACT_SCAN:
                raise HTTPException(409, detail="legacy_artifact_inventory_too_large")
            candidates.append(path)
    except OSError:
        return ArtifactListResponse(artifacts=[], total=0)
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        if (
            not path.is_file()
            or not _has_no_symlink_component(path, root=base)
            or not is_scientific_output_file(
                path,
                output_root=base,
                claim_identities=claim_identities,
            )
        ):
            continue
        rel = path.relative_to(base)
        try:
            rows.append(
                Artifact(
                    artifact_id=_artifact_id(job_id, rel),
                    job_id=job_id,
                    relative_path=rel.as_posix(),
                    size_bytes=path.stat().st_size,
                    mime_type=mimetypes.guess_type(str(path))[0]
                    or "application/octet-stream",
                    created_at=path_modified_at_iso(path),
                )
            )
        except OSError:
            continue
    total = len(rows)
    return ArtifactListResponse(artifacts=rows[:limit], total=total)


@router.get("/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    job_id: str = Query(...),
    cursor: str | None = Query(None, max_length=256),
    limit: int = Query(50, ge=1, le=100),
) -> ArtifactListResponse:
    run_id = _canonical_run_id(job_id)
    if run_id is not None:
        return await _list_canonical_artifacts(
            job_id=job_id,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )
    if cursor is not None:
        raise HTTPException(422, detail="legacy_artifact_cursor_not_supported")
    return _list_terminal_legacy_artifacts(
        workspace=_workspace_or_503(),
        job_id=job_id,
        limit=limit,
    )


def _parse_single_range(value: str | None, *, size: int) -> tuple[int, int] | None:
    if value is None or value == "":
        return None
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(
            416,
            detail="invalid_byte_range",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    spec = value.removeprefix("bytes=")
    if "-" not in spec:
        raise HTTPException(
            416,
            detail="invalid_byte_range",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    raw_start, raw_end = spec.split("-", 1)
    try:
        if raw_start == "":
            suffix = int(raw_end, 10)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(raw_start, 10)
            end = size - 1 if raw_end == "" else int(raw_end, 10)
            if start < 0 or end < start or start >= size:
                raise ValueError
            end = min(end, size - 1)
        if size <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(
            416,
            detail="byte_range_not_satisfiable",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        ) from None
    return start, end


async def _verified_reader_stream(
    reader,
    *,
    start: int,
    length: int,
) -> AsyncIterator[bytes]:
    offset = start
    remaining = length
    try:
        while remaining:
            chunk = await reader.read_chunk(
                offset=offset,
                max_bytes=min(_STREAM_CHUNK_BYTES, remaining),
            )
            if not chunk:
                raise RuntimeError("verified artifact ended before its inventory size")
            yield chunk
            offset += len(chunk)
            remaining -= len(chunk)
    finally:
        await reader.aclose()


async def _download_canonical_artifact(
    *,
    job_id: str,
    run_id: str,
    relative: PurePosixPath,
    range_header: str | None,
) -> StreamingResponse:
    runtime = _runtime_or_503()
    try:
        reader = await runtime.open_verified_artifact(run_id, relative.as_posix())
    except asyncio.CancelledError:
        raise
    except ValueError:
        raise HTTPException(400, detail="unsafe_artifact_path") from None
    except Exception as exc:
        raise _canonical_observation_error(exc) from None
    try:
        item = reader.artifact
        selected = _parse_single_range(range_header, size=item.size_bytes)
        if selected is None:
            start, end = 0, item.size_bytes - 1
            status_code = 200
        else:
            start, end = selected
            status_code = 206
        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                + quote(PurePosixPath(item.relative_path).name, safe="")
            ),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{item.size_bytes}"
        return StreamingResponse(
            _verified_reader_stream(reader, start=start, length=length),
            status_code=status_code,
            media_type=item.media_type,
            headers=headers,
        )
    except BaseException:
        await reader.aclose()
        raise


def _download_terminal_legacy_artifact(
    *,
    workspace: Path,
    job_id: str,
    relative: PurePosixPath,
) -> FileResponse:
    if _read_terminal_legacy_job(workspace, job_id) is None:
        raise HTTPException(404, detail="legacy_artifact_not_found")
    base = _artifacts_dir(workspace, job_id)
    target = base.joinpath(*relative.parts)
    if (
        not base.is_dir()
        or not _has_no_symlink_component(base, root=workspace)
        or not _has_no_symlink_component(target, root=base)
    ):
        raise HTTPException(404, detail="legacy_artifact_not_found")
    _resolve_artifact_target(base, relative)
    claim_identities = collect_output_claim_identities(base)
    if not is_scientific_output_file(
        target,
        output_root=base,
        claim_identities=claim_identities,
    ):
        raise HTTPException(404, detail="legacy_artifact_not_found")
    return FileResponse(
        path=target,
        filename=relative.name,
        media_type=mimetypes.guess_type(str(target))[0]
        or "application/octet-stream",
        headers={"X-Artifact-Authority": "legacy-terminal-job"},
    )


@router.get("/artifacts/{artifact_id:path}/download")
async def download_artifact(
    artifact_id: str,
    range_header: str | None = Header(default=None, alias="Range"),
):
    job_id, relative = _split_artifact_id(artifact_id)
    run_id = _canonical_run_id(job_id)
    if run_id is not None:
        return await _download_canonical_artifact(
            job_id=job_id,
            run_id=run_id,
            relative=relative,
            range_header=range_header,
        )
    # Starlette's FileResponse retains legacy single-Range behavior. Canonical
    # downloads never use it because reopening a verified path is not safe.
    return _download_terminal_legacy_artifact(
        workspace=_workspace_or_503(),
        job_id=job_id,
        relative=relative,
    )
