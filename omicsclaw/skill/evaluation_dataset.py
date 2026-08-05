"""Content-bound dataset resolution for Evaluation Protocols."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .schema import EvaluationDatasetRef

__all__ = [
    "DatasetIntegrityError",
    "digest_dataset_selection",
    "digest_dataset_tree",
    "resolve_repository_dataset",
]


class DatasetIntegrityError(RuntimeError):
    """The declared evaluation dataset cannot be proven at its expected identity."""


@dataclass(frozen=True, slots=True)
class _DatasetEntry:
    relative_path: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


def _entry_identity(path: Path, root: Path) -> _DatasetEntry:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise DatasetIntegrityError("dataset changed while being inspected") from exc
    if stat.S_ISLNK(observed.st_mode):
        raise DatasetIntegrityError("dataset contains a symbolic link")
    if stat.S_ISDIR(observed.st_mode):
        kind = "directory"
    elif stat.S_ISREG(observed.st_mode):
        kind = "file"
    else:
        raise DatasetIntegrityError("dataset contains a special file")
    relative = "." if path == root else path.relative_to(root).as_posix()
    return _DatasetEntry(
        relative_path=relative,
        kind=kind,
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=stat.S_IMODE(observed.st_mode),
        size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
    )


def _snapshot_tree(root: Path) -> tuple[_DatasetEntry, ...]:
    """Capture a stable, sorted metadata inventory without following links."""
    root_entry = _entry_identity(root, root)
    if root_entry.kind == "file":
        return (root_entry,)

    def fail_on_walk_error(error: OSError) -> None:
        raise DatasetIntegrityError("cannot enumerate dataset tree") from error

    entries = [root_entry]
    for current, directories, files in os.walk(
        root,
        followlinks=False,
        onerror=fail_on_walk_error,
    ):
        # Python bytecode is execution-environment cache, not benchmark content.
        # Excluding it keeps a verifier import from changing the scientific
        # dataset identity across Python versions.
        directories[:] = [name for name in directories if name != "__pycache__"]
        files = [name for name in files if not name.endswith(".pyc")]
        directories.sort()
        files.sort()
        current_path = Path(current)
        for name in [*directories, *files]:
            entries.append(_entry_identity(current_path / name, root))
    entries.sort(key=lambda item: item.relative_path)
    return tuple(entries)


def _file_digest(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DatasetIntegrityError(f"cannot open dataset file: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise DatasetIntegrityError(f"dataset entry is not a regular file: {path.name}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise DatasetIntegrityError(f"dataset file changed while hashing: {path.name}")
        return before.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def digest_dataset_tree(root: str | Path) -> str:
    """Hash relative paths and bytes for one immutable file or directory tree."""
    root_path = Path(root)
    try:
        before = _snapshot_tree(root_path)
    except DatasetIntegrityError as exc:
        if not root_path.exists() and not root_path.is_symlink():
            raise DatasetIntegrityError("dataset path is missing") from exc
        raise

    digest = hashlib.sha256()
    if before[0].kind == "file":
        size, content_digest = _file_digest(root_path)
        digest.update(f"F\0.\0{size}\0{content_digest}\n".encode("utf-8"))
        if _snapshot_tree(root_path) != before:
            raise DatasetIntegrityError("dataset changed while hashing")
        return "sha256:" + digest.hexdigest()

    for entry in before:
        if entry.kind == "directory":
            digest.update(f"D\0{entry.relative_path}\n".encode("utf-8"))
        else:
            path = root_path / entry.relative_path
            size, content_digest = _file_digest(path)
            digest.update(
                f"F\0{entry.relative_path}\0{size}\0{content_digest}\n".encode("utf-8")
            )
    if _snapshot_tree(root_path) != before:
        raise DatasetIntegrityError("dataset changed while hashing")
    return "sha256:" + digest.hexdigest()


def _resolve_selected_member(root: Path, member: str) -> Path:
    candidate = root.joinpath(*Path(member).parts)
    current = root
    for part in Path(member).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise DatasetIntegrityError("selected dataset member is missing") from exc
        if stat.S_ISLNK(mode):
            raise DatasetIntegrityError("selected dataset member contains a symbolic link")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DatasetIntegrityError("selected dataset member escapes its root") from exc
    return resolved


def _snapshot_selection(
    selected: tuple[tuple[str, Path], ...],
) -> tuple[tuple[str, _DatasetEntry], ...]:
    return tuple(
        (member, entry)
        for member, path in selected
        for entry in _snapshot_tree(path)
    )


def digest_dataset_selection(root: str | Path, members: list[str] | tuple[str, ...]) -> str:
    """Hash a named, non-overlapping selection of files or subtrees under a root."""
    root_path = Path(root).resolve()
    if not members:
        return digest_dataset_tree(root_path)
    if len(members) != len(set(members)):
        raise DatasetIntegrityError("selected dataset members must be unique")
    selected = tuple(
        (member, _resolve_selected_member(root_path, member))
        for member in sorted(members)
    )
    before = _snapshot_selection(selected)
    digest = hashlib.sha256()
    for member, path in selected:
        selected_digest = digest_dataset_tree(path)
        digest.update(f"M\0{member}\0{selected_digest}\n".encode("utf-8"))
    if _snapshot_selection(selected) != before:
        raise DatasetIntegrityError("dataset selection changed while hashing")
    return "sha256:" + digest.hexdigest()


def resolve_repository_dataset(
    repository_root: str | Path,
    reference: EvaluationDatasetRef,
) -> tuple[Path, str]:
    """Resolve and verify a repository-relative evaluation dataset reference."""
    if reference.store != "repository":
        raise DatasetIntegrityError(f"unsupported dataset store: {reference.store}")
    root = Path(repository_root).resolve()
    candidate = root.joinpath(*Path(reference.path).parts)
    current = root
    for part in Path(reference.path).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise DatasetIntegrityError("dataset path is missing") from exc
        if stat.S_ISLNK(mode):
            raise DatasetIntegrityError("dataset path contains a symbolic link")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DatasetIntegrityError("dataset path escapes the repository") from exc

    observed = digest_dataset_selection(resolved, reference.members)
    if observed != reference.content_sha256:
        raise DatasetIntegrityError(
            "dataset digest mismatch: declared content does not match repository bytes"
        )
    return resolved, observed
