"""Filesystem layout helpers for remote control-plane state.

Workspace layout::

    <workspace>/.omicsclaw/remote/
        datasets/<dataset_id>/
            <original_filename>
            meta.json
        jobs/<job_id>/
            job.json
            stdout.log
            artifacts/<...>

Callers must supply the Workspace frozen by the Desktop Backend composition
root.  This module deliberately does not re-resolve mutable process environment
state at request time.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Iterator

REMOTE_SUBDIR = ".omicsclaw/remote"
_CHECKSUM_HEAD_BYTES = 64 * 1024


class UnsafeRemoteStorageError(RuntimeError):
    """A compatibility-state root is missing directory ownership proof."""


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise UnsafeRemoteStorageError("secure_directory_handles_unavailable")
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise UnsafeRemoteStorageError("secure_directory_handles_unavailable")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


@contextmanager
def open_storage_directory(
    workspace: Path,
    *relative_parts: str,
    create: bool,
) -> Iterator[tuple[Path, int]]:
    """Hold a no-follow directory handle across a storage operation.

    A returned lexical ``Path`` is presentation metadata only.  Callers that
    mutate compatibility state must address children through the yielded fd;
    the fd continues to name the proven directory even if an attacker renames
    or replaces its path after this function returns control.
    """

    boundary = Path(workspace).expanduser()
    if not boundary.is_absolute():
        raise UnsafeRemoteStorageError("remote_workspace_not_absolute")
    flags = _directory_open_flags()
    try:
        current_fd = os.open(boundary, flags)
    except OSError as exc:
        raise UnsafeRemoteStorageError(
            "remote_workspace_not_owned_directory"
        ) from exc

    lexical_path = boundary
    try:
        for raw_part in relative_parts:
            for part in Path(raw_part).parts:
                if part in {"", ".", ".."}:
                    raise UnsafeRemoteStorageError(
                        "unsafe_remote_storage_component"
                    )
                lexical_path = lexical_path / part
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise UnsafeRemoteStorageError(
                            "remote_storage_directory_create_failed"
                        ) from exc
                    try:
                        next_fd = os.open(part, flags, dir_fd=current_fd)
                    except OSError as exc:
                        raise UnsafeRemoteStorageError(
                            "remote_storage_directory_ownership_lost"
                        ) from exc
                except OSError as exc:
                    raise UnsafeRemoteStorageError(
                        "remote_storage_symlink_or_non_directory"
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
        yield lexical_path, current_fd
    finally:
        os.close(current_fd)


def _storage_directory(
    workspace: Path,
    *relative_parts: str,
    create: bool,
) -> Path:
    """Return one lexical Workspace child without following storage symlinks.

    ``Path.mkdir(parents=True)`` follows an existing ``.omicsclaw``/``remote``
    symlink.  Dataset deletion would then be able to remove state outside the
    Active Workspace.  Walk and re-check every owned component before and
    after creation instead.
    """

    boundary = Path(workspace).expanduser()
    if not boundary.is_absolute():
        raise UnsafeRemoteStorageError("remote_workspace_not_absolute")
    if boundary.is_symlink() or not boundary.is_dir():
        raise UnsafeRemoteStorageError("remote_workspace_not_owned_directory")

    current = boundary
    for raw_part in relative_parts:
        for part in Path(raw_part).parts:
            if part in {"", ".", ".."}:
                raise UnsafeRemoteStorageError("unsafe_remote_storage_component")
            current = current / part
            if current.is_symlink():
                raise UnsafeRemoteStorageError("remote_storage_symlink_not_allowed")
            if current.exists():
                if not current.is_dir():
                    raise UnsafeRemoteStorageError(
                        "remote_storage_component_not_directory"
                    )
                continue
            if not create:
                return boundary.joinpath(*relative_parts)
            try:
                current.mkdir()
            except FileExistsError:
                # A concurrent creator is acceptable only when it created the
                # same ordinary directory, never a symlink or file.
                pass
            except OSError as exc:
                raise UnsafeRemoteStorageError(
                    "remote_storage_directory_create_failed"
                ) from exc
            if current.is_symlink() or not current.is_dir():
                raise UnsafeRemoteStorageError(
                    "remote_storage_directory_ownership_lost"
                )
            if boundary.is_symlink() or not boundary.is_dir():
                raise UnsafeRemoteStorageError(
                    "remote_workspace_directory_ownership_lost"
                )

    # Re-prove the complete chain after creation so a pre-existing symlink
    # cannot be swapped in between an early check and the caller's write.
    current = boundary
    for raw_part in relative_parts:
        for part in Path(raw_part).parts:
            current = current / part
            if current.is_symlink() or not current.is_dir():
                raise UnsafeRemoteStorageError(
                    "remote_storage_directory_ownership_lost"
                )
    return current


def remote_root(workspace: Path, *, create: bool = True) -> Path:
    return _storage_directory(
        workspace,
        ".omicsclaw",
        "remote",
        create=create,
    )


def datasets_root(workspace: Path, *, create: bool = True) -> Path:
    return _storage_directory(
        workspace,
        ".omicsclaw",
        "remote",
        "datasets",
        create=create,
    )


def jobs_root(workspace: Path, *, create: bool = True) -> Path:
    return _storage_directory(
        workspace,
        ".omicsclaw",
        "remote",
        "jobs",
        create=create,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def path_modified_at_iso(path: Path) -> str:
    return timestamp_iso(path.stat().st_mtime)


def composite_checksum(path: Path) -> str:
    """sha256 of the first 64 KiB plus ``":<size_bytes>"``.

    Matches App ``src/lib/dataset-ref.ts`` so checksums round-trip.
    Cheap on multi-GB ``.h5ad`` files; collisions are vanishingly rare for
    deduplication purposes (UX-grade fingerprint, not cryptographic).
    """
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        head = fh.read(_CHECKSUM_HEAD_BYTES)
        hasher.update(head)
        size = path.stat().st_size
    return f"sha256-64k:{hasher.hexdigest()}:{size}"
