"""Dataset registration & retrieval for the remote control plane.

Real implementation (not a placeholder):
- ``POST /datasets/upload``        — multipart upload, fingerprint, persist
- ``POST /datasets/import-remote`` — register a path that already exists on
                                     the server (for users who scp/rsync)
- ``GET  /datasets``               — list registered DatasetRefs
- ``DELETE /datasets/{id}``        — unregister (and for uploads, free disk)

Storage layout (see ``omicsclaw.remote.storage``)::

    <workspace>/.omicsclaw/remote/datasets/<dataset_id>/
        <original_filename>          (upload only)
        meta.json
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
import uuid
from pathlib import Path
import stat
from typing import Iterator
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from omicsclaw.remote.schemas import (
    DatasetImportRemoteRequest,
    DatasetListResponse,
    DatasetRef,
)
from omicsclaw.remote.storage import (
    UnsafeRemoteStorageError,
    composite_checksum,
    open_storage_directory,
    path_modified_at_iso,
    timestamp_iso,
)
from omicsclaw.remote.runtime_binding import get_remote_workspace

router = APIRouter(tags=["remote"])

_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB — boring MVP cap
_MAX_META_BYTES = 1024 * 1024
_IO_CHUNK_BYTES = 1024 * 1024
_CHECKSUM_HEAD_BYTES = 64 * 1024
_MAX_DELETE_ENTRIES = 100_000
_MAX_DELETE_DEPTH = 64
_MAX_DATASET_INVENTORY = 10_000
_MAX_DISPLAY_NAME_BYTES = 1024
_MAX_FILENAME_BYTES = 255
_RESERVED_DATASET_FILENAMES = frozenset({"meta.json", "meta.json.tmp"})


def _owned_directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise UnsafeRemoteStorageError("secure_directory_handles_unavailable")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _owned_file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise UnsafeRemoteStorageError("secure_file_handles_unavailable")
    return os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@contextmanager
def _open_dataset_root(
    workspace: Path,
    *,
    create: bool,
) -> Iterator[tuple[Path, int]]:
    try:
        with open_storage_directory(
            workspace,
            ".omicsclaw",
            "remote",
            "datasets",
            create=create,
        ) as opened:
            yield opened
    except FileNotFoundError:
        raise
    except UnsafeRemoteStorageError as exc:
        raise HTTPException(409, detail="unsafe_dataset_storage") from exc


def _open_dataset_directory(root_fd: int, dataset_id: str) -> int:
    try:
        return os.open(dataset_id, _owned_directory_flags(), dir_fd=root_fd)
    except OSError as exc:
        raise UnsafeRemoteStorageError("unsafe_dataset_directory") from exc


def _verify_storage_root_identity(root_path: Path, root_fd: int) -> None:
    """Prove the response URI still names the directory held by ``root_fd``."""

    try:
        path_fd = os.open(root_path, _owned_directory_flags())
    except OSError as exc:
        raise UnsafeRemoteStorageError("dataset_storage_path_replaced") from exc
    try:
        held = os.fstat(root_fd)
        observed = os.fstat(path_fd)
        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
            raise UnsafeRemoteStorageError("dataset_storage_path_replaced")
    finally:
        os.close(path_fd)


def _read_meta_at(root_fd: int, dataset_id: str) -> DatasetRef | None:
    try:
        dataset_fd = _open_dataset_directory(root_fd, dataset_id)
    except UnsafeRemoteStorageError:
        return None
    try:
        try:
            meta_fd = os.open(
                "meta.json",
                os.O_RDONLY | _owned_file_flags(),
                dir_fd=dataset_fd,
            )
        except OSError:
            return None
        try:
            file_stat = os.fstat(meta_fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _MAX_META_BYTES:
                return None
            with os.fdopen(meta_fd, "r", encoding="utf-8", closefd=True) as handle:
                meta_fd = -1
                payload = json.load(handle)
            ref = DatasetRef.model_validate(payload)
            return ref if ref.dataset_id == dataset_id else None
        except (OSError, ValueError):
            return None
        finally:
            if meta_fd >= 0:
                os.close(meta_fd)
    finally:
        os.close(dataset_fd)


def _write_meta_at(dataset_fd: int, ref: DatasetRef) -> None:
    payload = json.dumps(ref.model_dump(), indent=2, sort_keys=True).encode("utf-8")
    if len(payload) > _MAX_META_BYTES:
        raise UnsafeRemoteStorageError("dataset_metadata_too_large")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _owned_file_flags()
    try:
        meta_fd = os.open("meta.json.tmp", flags, 0o600, dir_fd=dataset_fd)
    except OSError as exc:
        raise UnsafeRemoteStorageError("unsafe_dataset_metadata") from exc
    try:
        with os.fdopen(meta_fd, "wb", closefd=True) as handle:
            meta_fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            "meta.json.tmp",
            "meta.json",
            src_dir_fd=dataset_fd,
            dst_dir_fd=dataset_fd,
        )
    except Exception:
        try:
            os.unlink("meta.json.tmp", dir_fd=dataset_fd)
        except OSError:
            pass
        raise
    finally:
        if meta_fd >= 0:
            os.close(meta_fd)


def _composite_checksum_fd(file_fd: int) -> str:
    file_stat = os.fstat(file_fd)
    os.lseek(file_fd, 0, os.SEEK_SET)
    head = os.read(file_fd, _CHECKSUM_HEAD_BYTES)
    os.lseek(file_fd, 0, os.SEEK_SET)
    return f"sha256-64k:{hashlib.sha256(head).hexdigest()}:{file_stat.st_size}"


def _registered_storage_matches(
    ref: DatasetRef,
    *,
    root_fd: int,
    root_path: Path,
    dataset_id: str,
) -> bool:
    storage_path = _storage_path_from_uri(ref.storage_uri)
    if storage_path is None:
        return False
    try:
        if storage_path.parent == root_path / dataset_id:
            dataset_fd = _open_dataset_directory(root_fd, dataset_id)
            try:
                file_fd = os.open(
                    storage_path.name,
                    os.O_RDONLY | _owned_file_flags(),
                    dir_fd=dataset_fd,
                )
                try:
                    file_stat = os.fstat(file_fd)
                    return bool(
                        stat.S_ISREG(file_stat.st_mode)
                        and file_stat.st_size == ref.size_bytes
                        and _composite_checksum_fd(file_fd) == ref.checksum
                        and timestamp_iso(file_stat.st_mtime) == ref.modified_at
                    )
                finally:
                    os.close(file_fd)
            finally:
                os.close(dataset_fd)
        return bool(
            storage_path.is_file()
            and storage_path.stat().st_size == ref.size_bytes
            and composite_checksum(storage_path) == ref.checksum
            and path_modified_at_iso(storage_path) == ref.modified_at
        )
    except (OSError, UnsafeRemoteStorageError):
        return False


def _project_dataset_ref(
    ref: DatasetRef,
    *,
    root_fd: int,
    root_path: Path,
    dataset_id: str,
) -> DatasetRef:
    storage_matches = _registered_storage_matches(
        ref,
        root_fd=root_fd,
        root_path=root_path,
        dataset_id=dataset_id,
    )
    expected_status = "synced" if storage_matches else "stale"
    return (
        ref
        if expected_status == ref.status
        else ref.model_copy(update={"status": expected_status})
    )


def _full_sha256_fd(file_fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(file_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(file_fd, _IO_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(file_fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _full_sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_IO_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


async def _run_with_duplicated_fd(function, file_fd: int, /, *args, **kwargs):
    """Run blocking fd work without sharing a cancel-closeable descriptor.

    ``asyncio.to_thread`` does not stop its worker when the awaiting request is
    canceled.  The worker therefore owns a duplicate and closes it itself;
    request cleanup may safely close the original immediately.
    """

    duplicate_fd = os.dup(file_fd)

    def invoke():
        try:
            return function(duplicate_fd, *args, **kwargs)
        finally:
            os.close(duplicate_fd)

    try:
        task = asyncio.create_task(asyncio.to_thread(invoke))
    except BaseException:
        os.close(duplicate_fd)
        raise
    return await asyncio.shield(task)


def _full_sha256_for_ref(
    root_fd: int,
    root_path: Path,
    dataset_id: str,
    ref: DatasetRef,
) -> str | None:
    storage_path = _storage_path_from_uri(ref.storage_uri)
    if storage_path is None:
        return None
    expected_parent = root_path / dataset_id
    try:
        if storage_path.parent == expected_parent:
            dataset_fd = _open_dataset_directory(root_fd, dataset_id)
            try:
                file_fd = os.open(
                    storage_path.name,
                    os.O_RDONLY | _owned_file_flags(),
                    dir_fd=dataset_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                        return None
                    return _full_sha256_fd(file_fd)
                finally:
                    os.close(file_fd)
            finally:
                os.close(dataset_fd)
        return _full_sha256_path(storage_path)
    except (OSError, UnsafeRemoteStorageError):
        return None


def _find_existing_by_checksum_at(
    root_fd: int,
    root_path: Path,
    checksum: str,
    execution_target: str,
    full_sha256: str,
    *,
    exclude_id: str | None = None,
) -> DatasetRef | None:
    try:
        with os.scandir(root_fd) as entries:
            names: list[str] = []
            for entry in entries:
                if len(names) >= _MAX_DATASET_INVENTORY:
                    raise UnsafeRemoteStorageError(
                        "dataset_inventory_bound_exceeded"
                    )
                names.append(entry.name)
    except OSError as exc:
        raise UnsafeRemoteStorageError("dataset_inventory_unavailable") from exc
    for dataset_id in names:
        if dataset_id == exclude_id:
            continue
        ref = _read_meta_at(root_fd, dataset_id)
        if ref is None:
            continue
        ref = _project_dataset_ref(
            ref,
            root_fd=root_fd,
            root_path=root_path,
            dataset_id=dataset_id,
        )
        if (
            ref.checksum == checksum
            and ref.execution_target == execution_target
            and ref.status != "stale"
            and _full_sha256_for_ref(root_fd, root_path, dataset_id, ref)
            == full_sha256
        ):
            return ref
    return None


def _list_dataset_refs_at(root_fd: int, root_path: Path) -> list[DatasetRef]:
    try:
        with os.scandir(root_fd) as entries:
            ordered: list[tuple[str, float]] = []
            for entry in entries:
                if len(ordered) >= _MAX_DATASET_INVENTORY:
                    raise UnsafeRemoteStorageError(
                        "dataset_inventory_bound_exceeded"
                    )
                if entry.is_dir(follow_symlinks=False):
                    ordered.append(
                        (entry.name, entry.stat(follow_symlinks=False).st_mtime)
                    )
    except OSError as exc:
        raise UnsafeRemoteStorageError("dataset_inventory_unavailable") from exc
    refs: list[DatasetRef] = []
    for dataset_id, _mtime in sorted(
        ordered,
        key=lambda item: item[1],
        reverse=True,
    ):
        ref = _read_meta_at(root_fd, dataset_id)
        if ref is not None:
            refs.append(
                _project_dataset_ref(
                    ref,
                    root_fd=root_fd,
                    root_path=root_path,
                    dataset_id=dataset_id,
                )
            )
    return refs


def _validate_upload_filename(raw_name: str | None) -> str:
    safe_name = Path(raw_name or "dataset.bin").name
    if (
        safe_name in {"", ".", ".."}
        or "\x00" in safe_name
        or len(safe_name.encode("utf-8")) > _MAX_FILENAME_BYTES
        or safe_name.casefold() in _RESERVED_DATASET_FILENAMES
    ):
        raise HTTPException(400, detail="reserved_dataset_filename")
    return safe_name


def _normalize_display_name(raw_name: str | None, *, fallback: str) -> str:
    value = str(raw_name or "").strip() or fallback
    if "\x00" in value or len(value.encode("utf-8")) > _MAX_DISPLAY_NAME_BYTES:
        raise HTTPException(400, detail="dataset_display_name_too_long")
    return value


def _remove_tree_at(
    parent_fd: int,
    name: str,
    *,
    root_device: int,
    remaining: list[int],
    depth: int = 0,
) -> None:
    if depth > _MAX_DELETE_DEPTH or remaining[0] <= 0:
        raise UnsafeRemoteStorageError("dataset_delete_bound_exceeded")
    child_fd = _open_dataset_directory(parent_fd, name)
    try:
        if os.fstat(child_fd).st_dev != root_device:
            raise UnsafeRemoteStorageError("dataset_delete_mount_not_allowed")
        with os.scandir(child_fd) as entries:
            names = [entry.name for entry in entries]
        for child_name in names:
            remaining[0] -= 1
            if remaining[0] < 0:
                raise UnsafeRemoteStorageError("dataset_delete_bound_exceeded")
            child_stat = os.stat(child_name, dir_fd=child_fd, follow_symlinks=False)
            if stat.S_ISDIR(child_stat.st_mode):
                _remove_tree_at(
                    child_fd,
                    child_name,
                    root_device=root_device,
                    remaining=remaining,
                    depth=depth + 1,
                )
            else:
                os.unlink(child_name, dir_fd=child_fd)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _quarantine_and_remove_dataset(root_fd: int, dataset_id: str) -> None:
    quarantine = f".deleting-{uuid.uuid4().hex}"
    os.rename(
        dataset_id,
        quarantine,
        src_dir_fd=root_fd,
        dst_dir_fd=root_fd,
    )
    _remove_tree_at(
        root_fd,
        quarantine,
        root_device=os.fstat(root_fd).st_dev,
        remaining=[_MAX_DELETE_ENTRIES],
    )


def _normalize_execution_target(raw: str) -> str:
    value = str(raw or "").strip()
    if value == "local":
        return value
    if value.startswith("remote:") and value != "remote:":
        return value
    raise HTTPException(
        status_code=400,
        detail="execution_target must be 'local' or 'remote:<profile_id>'",
    )


def _storage_path_from_uri(storage_uri: str) -> Path | None:
    parsed = urlparse(storage_uri)
    if parsed.scheme in ("", "file"):
        return Path(unquote(parsed.path if parsed.scheme else storage_uri))
    return None


def _workspace_or_503() -> Path:
    workspace = get_remote_workspace()
    if workspace is None:
        raise HTTPException(status_code=503, detail="remote_workspace_unavailable")
    return workspace

def _validate_dataset_id(dataset_id: str) -> str:
    """Reject path-traversal / absolute / multi-segment ids.

    Same shape as ``artifacts._validate_job_id`` — dataset_id must be a
    single safe path component so ``datasets_root / id`` can never
    escape the workspace.
    """
    candidate = str(dataset_id or "").strip()
    path = Path(candidate)
    if (
        not candidate
        or path.is_absolute()
        or len(path.parts) != 1
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise HTTPException(
            status_code=400, detail="dataset_id contains an unsafe path"
        )
    return candidate


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets() -> DatasetListResponse:
    workspace = _workspace_or_503()
    refs: list[DatasetRef] = []
    try:
        with _open_dataset_root(workspace, create=False) as (root_path, root_fd):
            try:
                refs = await _run_with_duplicated_fd(
                    _list_dataset_refs_at,
                    root_fd,
                    root_path,
                )
            except UnsafeRemoteStorageError as exc:
                raise HTTPException(409, detail="unsafe_dataset_storage") from exc
    except FileNotFoundError:
        pass
    return DatasetListResponse(
        datasets=refs,
        total=len(refs),
        workspace=str(workspace),
    )


@router.post("/datasets/upload", response_model=DatasetRef)
async def upload_dataset(
    file: UploadFile = File(...),
    display_name: str = Form(""),
    execution_target: str = Form(...),
) -> DatasetRef:
    workspace = _workspace_or_503()
    normalized_execution_target = _normalize_execution_target(execution_target)
    safe_name = _validate_upload_filename(file.filename)
    normalized_display_name = _normalize_display_name(
        display_name,
        fallback=safe_name,
    )
    dataset_id = uuid.uuid4().hex
    try:
        with _open_dataset_root(workspace, create=True) as (root_path, root_fd):
            created = False
            dataset_fd: int | None = None
            try:
                os.mkdir(dataset_id, mode=0o700, dir_fd=root_fd)
                created = True
                dataset_fd = _open_dataset_directory(root_fd, dataset_id)
                target_flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _owned_file_flags()
                )
                target_fd = os.open(
                    safe_name,
                    target_flags,
                    0o600,
                    dir_fd=dataset_fd,
                )
                written = 0
                full_hasher = hashlib.sha256()
                try:
                    with os.fdopen(target_fd, "wb", closefd=True) as out:
                        target_fd = -1
                        while True:
                            chunk = await file.read(_IO_CHUNK_BYTES)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > _MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413,
                                    detail=(
                                        f"upload exceeds {_MAX_UPLOAD_BYTES} bytes; use "
                                        "POST /datasets/import-remote after scp/rsync"
                                    ),
                                )
                            full_hasher.update(chunk)
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                finally:
                    if target_fd >= 0:
                        os.close(target_fd)

                read_fd = os.open(
                    safe_name,
                    os.O_RDONLY | _owned_file_flags(),
                    dir_fd=dataset_fd,
                )
                try:
                    initial_stat = os.fstat(read_fd)
                    if not stat.S_ISREG(initial_stat.st_mode):
                        raise UnsafeRemoteStorageError("unsafe_dataset_payload")
                    checksum = _composite_checksum_fd(read_fd)
                    full_sha256 = full_hasher.hexdigest()
                finally:
                    os.close(read_fd)

                existing = await _run_with_duplicated_fd(
                    _find_existing_by_checksum_at,
                    root_fd,
                    root_path,
                    checksum,
                    normalized_execution_target,
                    full_sha256,
                    exclude_id=dataset_id,
                )
                if existing is not None:
                    os.close(dataset_fd)
                    dataset_fd = None
                    _quarantine_and_remove_dataset(root_fd, dataset_id)
                    created = False
                    _verify_storage_root_identity(root_path, root_fd)
                    return existing

                target = root_path / dataset_id / safe_name
                ref = DatasetRef(
                    dataset_id=dataset_id,
                    display_name=normalized_display_name,
                    storage_uri=target.as_uri(),
                    execution_target=normalized_execution_target,
                    checksum=checksum,
                    size_bytes=initial_stat.st_size,
                    modified_at=timestamp_iso(initial_stat.st_mtime),
                    status="synced",
                )
                _write_meta_at(dataset_fd, ref)

                verification_fd = os.open(
                    safe_name,
                    os.O_RDONLY | _owned_file_flags(),
                    dir_fd=dataset_fd,
                )
                try:
                    final_stat = os.fstat(verification_fd)
                    if (
                        final_stat.st_dev != initial_stat.st_dev
                        or final_stat.st_ino != initial_stat.st_ino
                        or final_stat.st_size != initial_stat.st_size
                        or _composite_checksum_fd(verification_fd) != checksum
                        or await _run_with_duplicated_fd(
                            _full_sha256_fd,
                            verification_fd,
                        )
                        != full_sha256
                    ):
                        raise UnsafeRemoteStorageError(
                            "dataset_payload_changed_during_registration"
                        )
                finally:
                    os.close(verification_fd)
                _verify_storage_root_identity(root_path, root_fd)
                created = False
                return ref
            except HTTPException:
                raise
            except (OSError, UnsafeRemoteStorageError) as exc:
                raise HTTPException(409, detail="unsafe_dataset_storage") from exc
            finally:
                if dataset_fd is not None:
                    os.close(dataset_fd)
                if created:
                    try:
                        _quarantine_and_remove_dataset(root_fd, dataset_id)
                    except (OSError, UnsafeRemoteStorageError):
                        pass
    finally:
        await file.close()


@router.post("/datasets/import-remote", response_model=DatasetRef)
async def import_remote_dataset(req: DatasetImportRemoteRequest) -> DatasetRef:
    workspace = _workspace_or_503()
    normalized_execution_target = _normalize_execution_target(req.execution_target)
    src = Path(req.remote_path).expanduser()
    if not src.is_absolute():
        raise HTTPException(status_code=400, detail="remote_path must be absolute")
    try:
        resolved_src = src.resolve(strict=True)
        normalized_display_name = _normalize_display_name(
            req.display_name,
            fallback=resolved_src.name,
        )
        source_fd = os.open(
            resolved_src,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _owned_file_flags(),
        )
    except (OSError, UnsafeRemoteStorageError) as exc:
        raise HTTPException(status_code=404, detail=f"file not found on server: {src}") from exc
    try:
        initial_stat = os.fstat(source_fd)
        if not stat.S_ISREG(initial_stat.st_mode):
            raise HTTPException(status_code=404, detail=f"file not found on server: {src}")
        checksum = _composite_checksum_fd(source_fd)
        full_sha256 = await _run_with_duplicated_fd(_full_sha256_fd, source_fd)

        with _open_dataset_root(workspace, create=True) as (root_path, root_fd):
            try:
                existing = await _run_with_duplicated_fd(
                    _find_existing_by_checksum_at,
                    root_fd,
                    root_path,
                    checksum,
                    normalized_execution_target,
                    full_sha256,
                )
            except UnsafeRemoteStorageError as exc:
                raise HTTPException(409, detail="unsafe_dataset_storage") from exc
            if existing is not None:
                _verify_storage_root_identity(root_path, root_fd)
                return existing

            dataset_id = uuid.uuid4().hex
            created = False
            dataset_fd: int | None = None
            try:
                os.mkdir(dataset_id, mode=0o700, dir_fd=root_fd)
                created = True
                dataset_fd = _open_dataset_directory(root_fd, dataset_id)
                ref = DatasetRef(
                    dataset_id=dataset_id,
                    display_name=normalized_display_name,
                    storage_uri=resolved_src.as_uri(),
                    execution_target=normalized_execution_target,
                    checksum=checksum,
                    size_bytes=initial_stat.st_size,
                    modified_at=timestamp_iso(initial_stat.st_mtime),
                    status="synced",
                )
                _write_meta_at(dataset_fd, ref)
                try:
                    verification_fd = os.open(
                        resolved_src,
                        os.O_RDONLY
                        | getattr(os, "O_NONBLOCK", 0)
                        | _owned_file_flags(),
                    )
                except OSError as exc:
                    raise UnsafeRemoteStorageError(
                        "dataset_source_changed_during_registration"
                    ) from exc
                try:
                    final_stat = os.fstat(verification_fd)
                    if (
                        not stat.S_ISREG(final_stat.st_mode)
                        or final_stat.st_dev != initial_stat.st_dev
                        or final_stat.st_ino != initial_stat.st_ino
                        or final_stat.st_size != initial_stat.st_size
                        or final_stat.st_mtime_ns != initial_stat.st_mtime_ns
                        or _composite_checksum_fd(verification_fd) != checksum
                        or await _run_with_duplicated_fd(
                            _full_sha256_fd,
                            verification_fd,
                        )
                        != full_sha256
                    ):
                        raise UnsafeRemoteStorageError(
                            "dataset_source_changed_during_registration"
                        )
                finally:
                    os.close(verification_fd)
                _verify_storage_root_identity(root_path, root_fd)
                created = False
                return ref
            except (OSError, UnsafeRemoteStorageError) as exc:
                raise HTTPException(409, detail="unsafe_dataset_storage") from exc
            finally:
                if dataset_fd is not None:
                    os.close(dataset_fd)
                if created:
                    try:
                        _quarantine_and_remove_dataset(root_fd, dataset_id)
                    except (OSError, UnsafeRemoteStorageError):
                        pass
    finally:
        os.close(source_fd)


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str) -> Response:
    """Unregister a dataset.

    Upload-type datasets: removing the dataset dir frees the stored
    file plus ``meta.json``.

    Import-remote datasets: the dataset dir holds ONLY ``meta.json`` —
    the user's source file at ``storage_uri`` lives outside the
    workspace and is deliberately NOT touched. Rmtree on the workspace-
    local dataset dir is therefore safe for both shapes.
    """
    safe_id = _validate_dataset_id(dataset_id)
    workspace = _workspace_or_503()
    try:
        with _open_dataset_root(workspace, create=False) as (_root_path, root_fd):
            try:
                candidate_stat = os.stat(
                    safe_id,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=404, detail=f"dataset not found: {safe_id}"
                ) from exc
            if not stat.S_ISDIR(candidate_stat.st_mode):
                raise HTTPException(409, detail="unsafe_dataset_storage")
            try:
                await _run_with_duplicated_fd(
                    _quarantine_and_remove_dataset,
                    root_fd,
                    safe_id,
                )
            except (OSError, UnsafeRemoteStorageError) as exc:
                raise HTTPException(409, detail="unsafe_dataset_storage") from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"dataset not found: {safe_id}"
        ) from exc
    return Response(status_code=204)
