"""Project-scoped output path resolver (ADR 0035).

One resolver that every Surface (CLI, agent loop, Channel, Desktop) routes
through to place a Run on disk, so analysis outputs stop piling flat under a
single root and become navigable once dozens of runs accumulate.

Layout::

    <output_root>/<project_dir>/<skill>[__<method>]__<YYYYMMDD_HHMMSS>__<dataset>-<uid8>/

- ``<project_dir>`` is a readable ``<name-slug>__<short-id>`` (or the literal
  ``default``), the on-disk projection of ``project://<project_id>`` (ADR 0018).
  The canonical ``project_id`` lives in ``<project_dir>/project_meta.json``; the
  directory is **located by its stable ``short-id``** so the same project always
  resolves to one folder even when a caller does not know the display name.
- The Run leaf is the globally-unique ``run_id``; the trailing ``<dataset>-<uid8>``
  keeps it readable *and* unique (the desktop ``run_meta`` table keys on
  ``run_id``), so two runs of the same skill on the same dataset in the same
  second never collide.
- A per-project ``index.jsonl`` is a rebuildable listing cache; the Run
  directories and their ``manifest.json`` are the single source of truth.

See ADR 0035 §"Required implementation constraints" — the rules here (atomic
creation, immutable project dir, no post-run rename, dataset-slug ladder,
locked index append, path-safety assert) are part of the decision.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from omicsclaw.common.output_claim import (
    atomic_write_owned_output_text,
    first_filesystem_alias_component,
    is_filesystem_alias,
    stat_is_filesystem_alias,
)

try:  # POSIX advisory locking for the index append; degrade gracefully elsewhere.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAVE_FCNTL = False

try:  # Windows advisory locking for Project and stable index lock files.
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - non-Windows
    _HAVE_MSVCRT = False

__all__ = [
    "DEFAULT_PROJECT_ID",
    "PROJECT_META_FILENAME",
    "RUN_INDEX_FILENAME",
    "INDEX_SCHEMA_VERSION",
    "RunResolution",
    "slugify_token",
    "project_short_id",
    "dataset_slug",
    "build_run_dir_name",
    "resolve_project_dir",
    "resolve_run_dir",
    "finalize_run",
    "rebuild_index",
    "read_index",
    "iter_run_dirs",
    "find_run_dir",
    "read_project_meta",
    "assert_under_root",
    "list_projects",
    "resolve_cli_project",
    "get_current_project",
    "peek_current_project",
    "set_current_project",
    "clear_current_project",
]

DEFAULT_PROJECT_ID = "default"
PROJECT_META_FILENAME = "project_meta.json"
CURRENT_PROJECT_FILENAME = ".current_project"
RUN_INDEX_FILENAME = "index.jsonl"
INDEX_SCHEMA_VERSION = 1
PROJECT_META_SCHEMA_VERSION = 1
_SHORT_ID_LEN = 10
_UID_LEN = 8
_SLUG_MAX = 48
_PROJECT_LOCK_FILENAME = ".project-resolve.lock"
_PROJECT_LOCK_MUTEX = threading.Lock()
_INDEX_LOCK_FILENAME = ".index.lock"
_INDEX_LOCK_MUTEX = threading.Lock()
_CURRENT_PROJECT_LOCK_FILENAME = ".current-project.lock"
_CURRENT_PROJECT_LOCK_MUTEX = threading.Lock()
_CURRENT_PROJECT_MAX_BYTES = 4096
_STATE_FILE_OPEN_RETRIES = 8

# A run directory leaf: ``<skill>[__method]__YYYYMMDD_HHMMSS__<token>``. Matches
# the frontend run-dir pattern in ``OmicsClaw-App/src/lib/chat/run-link.ts``.
_RUN_DIR_RE = re.compile(r"^[^/]*__\d{8}_\d{6}__[^/]+$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify_token(value: str, *, max_len: int = _SLUG_MAX) -> str:
    """Lowercase, collapse non-``[a-z0-9]`` runs to ``-``, cap length.

    Unlike ``common.report.slugify_output_token`` this returns ``""`` (not
    ``"default"``) when nothing survives, so callers can apply a hash fallback
    instead of silently bucketing every non-ASCII name into ``default``.
    """
    text = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].strip("-")
    return text


def _short_hash(value: str, length: int = 6) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def project_short_id(project_id: str) -> str:
    """Deterministic ``hash(project_id)`` suffix used in the project dir name."""
    return hashlib.sha1(project_id.encode("utf-8")).hexdigest()[:_SHORT_ID_LEN]


def dataset_slug(
    *,
    input_path: str | None = None,
    input_paths: list[str] | None = None,
    demo: bool = False,
    dataset_label: str = "",
) -> str:
    """Resolve the readable dataset token (ADR 0035 constraint 7).

    Ladder: explicit label > single input basename > multi-input hash > demo >
    no-input. A name that slugifies to empty (e.g. all-CJK) falls back to a
    short content hash rather than collapsing to ``default``.
    """
    if dataset_label:
        slug = slugify_token(dataset_label)
        return slug or f"ds-{_short_hash(dataset_label)}"
    real_multi = [p for p in (input_paths or []) if p]
    if len(real_multi) >= 2:
        key = "|".join(sorted(Path(p).name for p in real_multi))
        return f"multi-{_short_hash(key)}"
    if input_path:
        stem = Path(input_path).name
        # Drop a single trailing extension for readability (pbmc.h5ad -> pbmc).
        stem = Path(stem).stem if "." in stem else stem
        slug = slugify_token(stem)
        return slug or f"ds-{_short_hash(Path(input_path).name)}"
    if demo:
        return "demo"
    return "noinput"


def build_run_dir_name(
    skill: str,
    timestamp: str,
    dataset: str,
    *,
    method: str | None = None,
    uid: str | None = None,
) -> str:
    """``<skill>[__method]__<ts>__<dataset>-<uid8>`` (ADR 0035 constraint 1)."""
    parts = [slugify_token(skill, max_len=64) or "skill"]
    if method:
        msl = slugify_token(method)
        if msl:
            parts.append(msl)
    parts.append(timestamp)
    uid = uid or uuid.uuid4().hex[:_UID_LEN]
    ds = dataset or "noinput"
    parts.append(f"{ds}-{uid}")
    return "__".join(parts)


@dataclass(frozen=True)
class RunResolution:
    """What ``resolve_run_dir`` hands back to a Surface."""

    run_dir: Path
    run_id: str
    project_dir: Path
    project_id: str
    dataset: str


# ---------------------------------------------------------------------------
# Project directory
# ---------------------------------------------------------------------------


def _has_filesystem_alias_component(path: Path) -> bool:
    """Return whether a lexical path contains a shared filesystem alias."""

    return first_filesystem_alias_component(path) is not None


def _is_unaliased_directory(path: Path) -> bool:
    try:
        return (
            not _has_filesystem_alias_component(path)
            and stat.S_ISDIR(path.stat().st_mode)
        )
    except OSError:
        return False


def _validate_regular_single_link_stat(file_stat: os.stat_result, *, label: str) -> None:
    if (
        stat_is_filesystem_alias(file_stat)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise RuntimeError(f"refusing non-regular or multiply-linked {label}")


def _open_regular_single_link_file(
    path: Path,
    *,
    flags: int,
    create: bool,
    label: str,
) -> int:
    """Open one Backend-owned state file without following static aliases."""
    candidate = Path(path)
    if _has_filesystem_alias_component(candidate.parent):
        raise RuntimeError(
            f"refusing {label} through a symbolic-link parent or Windows reparse point"
        )

    safe_flags = (
        flags
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    for _attempt in range(_STATE_FILE_OPEN_RETRIES):
        try:
            before = candidate.lstat()
        except FileNotFoundError:
            if not create:
                raise
            try:
                fd = os.open(
                    candidate,
                    safe_flags | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
            except FileExistsError:
                continue
            try:
                _validate_regular_single_link_stat(os.fstat(fd), label=label)
            except BaseException:
                os.close(fd)
                raise
            return fd

        _validate_regular_single_link_stat(before, label=label)
        try:
            fd = os.open(candidate, safe_flags)
        except FileNotFoundError:
            if create:
                continue
            raise
        try:
            after = os.fstat(fd)
            _validate_regular_single_link_stat(after, label=label)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise RuntimeError(f"refusing changed {label} during open")
        except BaseException:
            os.close(fd)
            raise
        return fd
    raise RuntimeError(f"could not safely create or open {label}")


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:  # pragma: no cover - defensive for unusual filesystems
            raise OSError("short write while publishing run index")
        remaining = remaining[written:]


def _acquire_exclusive_state_lock(fd: int, *, label: str) -> None:
    """Acquire one blocking cross-process lock or fail closed."""

    if _HAVE_FCNTL:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if _HAVE_MSVCRT:  # pragma: no cover - exercised on Windows
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, b"\0")
            os.fsync(fd)
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise RuntimeError(f"could not acquire {label}") from exc
                time.sleep(0.05)
    raise RuntimeError("cross-process file locking is unavailable")


def _release_exclusive_state_lock(fd: int) -> None:
    if _HAVE_FCNTL:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif _HAVE_MSVCRT:  # pragma: no cover - exercised on Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _read_regular_single_link_text(path: Path, *, label: str) -> str | None:
    try:
        fd = _open_regular_single_link_file(
            path,
            flags=os.O_RDONLY,
            create=False,
            label=label,
        )
    except (OSError, RuntimeError):
        return None
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_SH)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if _HAVE_FCNTL:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - descriptor cleanup is best effort
                pass
        os.close(fd)


def _read_bounded_regular_single_link_snapshot(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> str | None:
    """Read one small navigation snapshot without taking a blocking lock.

    The current-Project pointer is only a hint and its writer publishes by
    atomic replacement.  A no-follow open plus a bounded positional read is
    therefore sufficient; inode/size identity checks reject concurrent or
    adversarial mutation without creating a sibling lock file.
    """

    try:
        fd = _open_regular_single_link_file(
            path,
            flags=os.O_RDONLY,
            create=False,
            label=label,
        )
    except (OSError, RuntimeError):
        return None
    try:
        before = os.fstat(fd)
        if before.st_size < 0 or before.st_size > max_bytes:
            return None
        if hasattr(os, "pread"):
            payload = os.pread(fd, max_bytes + 1, 0)
        else:  # pragma: no cover - Windows fallback
            os.lseek(fd, 0, os.SEEK_SET)
            payload = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
        if (
            len(payload) > max_bytes
            or len(payload) != after.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            return None
        try:
            current = path.lstat()
            _validate_regular_single_link_stat(current, label=label)
        except (OSError, RuntimeError):
            return None
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            return None
        return payload.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def read_project_meta(project_dir: str | Path) -> dict[str, Any]:
    project_path = Path(project_dir)
    path = project_path / PROJECT_META_FILENAME
    try:
        if _has_filesystem_alias_component(path):
            return {}
        file_stat = path.stat()
        path.resolve(strict=True).relative_to(project_path.resolve(strict=True))
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
        ):
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            return {}
        return data
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError):
        return {}


def _project_id_of(project_dir: Path) -> str:
    meta = read_project_meta(project_dir)
    pid = str(meta.get("project_id", "")).strip()
    if pid:
        return pid
    name = project_dir.name
    return DEFAULT_PROJECT_ID if name == DEFAULT_PROJECT_ID else name


def _find_project_dir_by_short_id(output_root: Path, project_id: str) -> Path | None:
    """Locate an existing project dir for ``project_id`` by its stable short-id
    suffix, **confirmed** against ``project_meta.json`` so a hash-suffix
    coincidence or a hand-named directory cannot mis-route (ADR 0035 constraint 6).
    """
    suffix = f"__{project_short_id(project_id)}"
    try:
        for entry in sorted(output_root.iterdir()):
            if (
                not is_filesystem_alias(entry)
                and entry.is_dir()
                and entry.name.endswith(suffix)
            ):
                if str(read_project_meta(entry).get("project_id", "")) == project_id:
                    return entry
    except OSError:
        return None
    return None


def _write_project_meta(project_dir: Path, project_id: str, display_name: str) -> None:
    path = project_dir / PROJECT_META_FILENAME
    existing = read_project_meta(project_dir)
    if existing:
        existing_project_id = str(existing.get("project_id", "")).strip()
        if existing_project_id and existing_project_id != project_id:
            raise ValueError(
                f"project directory {project_dir} belongs to {existing_project_id!r}, "
                f"not {project_id!r}"
            )
        changed = False
        if not existing_project_id:
            existing["project_id"] = project_id
            changed = True
        # Rename reconciliation (ADR 0035 constraint 6): update the display name
        # in place, never the folder. Keep the original created_at + project_id.
        new_name = display_name or existing.get("display_name") or project_id
        if new_name != existing.get("display_name"):
            existing["display_name"] = new_name
            existing["updated_at"] = _utcnow_iso()
            changed = True
        if changed:
            _atomic_write_json(path, existing)
        return
    meta = {
        "schema_version": PROJECT_META_SCHEMA_VERSION,
        "project_id": project_id,
        "display_name": display_name or project_id,
        "created_at": _utcnow_iso(),
    }
    _atomic_write_json(path, meta)


@contextmanager
def _index_mutation_lock(project_dir: Path) -> Iterator[None]:
    """Serialize one Project's manifest-derived index publication.

    Locking ``index.jsonl`` itself is insufficient because ``rebuild_index``
    atomically replaces that inode.  A stable sibling lock coordinates both
    append and rebuild across threads and POSIX processes.
    """

    with _INDEX_LOCK_MUTEX:
        fd = _open_regular_single_link_file(
            Path(project_dir) / _INDEX_LOCK_FILENAME,
            flags=os.O_RDWR,
            create=True,
            label=_INDEX_LOCK_FILENAME,
        )
        _acquire_exclusive_state_lock(fd, label=_INDEX_LOCK_FILENAME)
        try:
            yield
        finally:
            try:
                _release_exclusive_state_lock(fd)
            finally:
                os.close(fd)


@contextmanager
def _current_project_lock(output_root: Path) -> Iterator[None]:
    """Serialize the active-Project pointer across threads and processes."""

    with _CURRENT_PROJECT_LOCK_MUTEX:
        fd = _open_regular_single_link_file(
            Path(output_root) / _CURRENT_PROJECT_LOCK_FILENAME,
            flags=os.O_RDWR,
            create=True,
            label=_CURRENT_PROJECT_LOCK_FILENAME,
        )
        _acquire_exclusive_state_lock(fd, label=_CURRENT_PROJECT_LOCK_FILENAME)
        try:
            yield
        finally:
            try:
                _release_exclusive_state_lock(fd)
            finally:
                os.close(fd)


@contextmanager
def _project_resolution_lock(output_root: Path) -> Iterator[None]:
    """Serialize project short-id scan/create/write across threads/processes."""
    with _PROJECT_LOCK_MUTEX:
        fd = _open_regular_single_link_file(
            output_root / _PROJECT_LOCK_FILENAME,
            flags=os.O_RDWR,
            create=True,
            label=_PROJECT_LOCK_FILENAME,
        )
        _acquire_exclusive_state_lock(fd, label=_PROJECT_LOCK_FILENAME)
        try:
            yield
        finally:
            try:
                _release_exclusive_state_lock(fd)
            finally:
                os.close(fd)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_owned_output_text(
        path,
        output_root=path.parent,
        text=json.dumps(data, ensure_ascii=False, indent=2),
        label=path.name,
    )


def _available_project_dir(output_root: Path, slug: str, short: str) -> Path:
    """Return the first non-existing ``<slug>[-N]__<short>`` candidate."""
    candidate = output_root / f"{slug}__{short}"
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = output_root / f"{slug}-{i}__{short}"
        if not candidate.exists():
            return candidate
        i += 1


def resolve_project_dir(
    output_root: str | Path,
    project_id: str = "",
    project_name: str = "",
    *,
    create: bool = True,
) -> Path:
    """Resolve (and optionally create) the on-disk Project directory.

    Empty / ``default`` ``project_id`` -> the literal ``default`` project. A real
    ``project_id`` resolves by its stable ``short-id`` so the same project is one
    folder regardless of whether this caller knows the display name.
    """
    output_root = Path(output_root)
    if _has_filesystem_alias_component(output_root):
        raise ValueError(
            f"refusing aliased output root before Project creation: {output_root}"
        )
    pid = (project_id or "").strip() or DEFAULT_PROJECT_ID

    if pid == DEFAULT_PROJECT_ID:
        project_dir = output_root / DEFAULT_PROJECT_ID
        if create:
            output_root.mkdir(parents=True, exist_ok=True)
            with _project_resolution_lock(output_root):
                if is_filesystem_alias(project_dir):
                    raise ValueError(f"refusing aliased project directory: {project_dir}")
                project_dir.mkdir(parents=True, exist_ok=True)
                _write_project_meta(project_dir, DEFAULT_PROJECT_ID, DEFAULT_PROJECT_ID)
        return project_dir

    short = project_short_id(pid)
    if create:
        output_root.mkdir(parents=True, exist_ok=True)
        with _project_resolution_lock(output_root):
            existing = _find_project_dir_by_short_id(output_root, pid)
            if existing is not None:
                project_dir = existing
            else:
                slug = slugify_token(project_name) if project_name else ""
                slug = slug or "project"
                project_dir = _available_project_dir(output_root, slug, short)
            if is_filesystem_alias(project_dir):
                raise ValueError(f"refusing aliased project directory: {project_dir}")
            project_dir.mkdir(parents=True, exist_ok=True)
            # Pass the raw (possibly empty) name: ``_write_project_meta`` keeps an
            # existing readable display name when a later caller (e.g. the agent) has
            # no thread name, instead of clobbering it back to the opaque project_id.
            _write_project_meta(project_dir, pid, project_name)
    else:
        existing = _find_project_dir_by_short_id(output_root, pid) if output_root.exists() else None
        if existing is not None:
            project_dir = existing
        else:
            slug = slugify_token(project_name) if project_name else ""
            slug = slug or "project"
            project_dir = output_root / f"{slug}__{short}"
    return project_dir


# ---------------------------------------------------------------------------
# Run directory
# ---------------------------------------------------------------------------


def assert_under_root(path: Path, root: Path) -> Path:
    """Resolve ``path`` and assert it stays under ``root`` (ADR 0035 constraint 10)."""
    rp = path.resolve()
    rr = Path(root).resolve()
    try:
        rp.relative_to(rr)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"resolved path {rp} escapes output root {rr}") from exc
    return rp


def _reserve_dir(parent: Path, name: str) -> Path:
    """Atomically create ``parent/name``; on collision append ``_N`` (constraint 2)."""
    candidate = parent / name
    try:
        candidate.mkdir(parents=False, exist_ok=False)
        return candidate
    except FileExistsError:
        pass
    i = 1
    while True:
        candidate = parent / f"{name}_{i}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            i += 1


def resolve_run_dir(
    *,
    output_root: str | Path,
    skill: str,
    project_id: str = "",
    project_name: str = "",
    input_path: str | None = None,
    input_paths: list[str] | None = None,
    demo: bool = False,
    dataset_label: str = "",
    method: str | None = None,
    timestamp: str | None = None,
    uid: str | None = None,
) -> RunResolution:
    """Place a Run on disk and return its resolution.

    The directory name is final at creation (no post-run rename, constraint 3);
    the actual method, if auto-selected by the skill, is recorded in
    ``manifest.json`` by :func:`finalize_run`, not by renaming.
    """
    output_root = Path(output_root)
    project_dir = resolve_project_dir(output_root, project_id, project_name, create=True)
    # Reject a symlinked project dir masquerading inside the root (constraint 10).
    if is_filesystem_alias(project_dir):
        raise ValueError(f"refusing aliased project directory: {project_dir}")

    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    ds = dataset_slug(
        input_path=input_path,
        input_paths=input_paths,
        demo=demo,
        dataset_label=dataset_label,
    )
    name = build_run_dir_name(skill, ts, ds, method=method, uid=uid)
    if not _RUN_DIR_RE.match(name):  # pragma: no cover - guards the contract
        raise ValueError(f"run dir name {name!r} violates the frontend run-dir pattern")

    run_dir = _reserve_dir(project_dir, name)
    assert_under_root(run_dir, output_root)
    pid = (project_id or "").strip() or DEFAULT_PROJECT_ID
    return RunResolution(
        run_dir=run_dir,
        run_id=run_dir.name,
        project_dir=project_dir,
        project_id=pid,
        dataset=ds,
    )


# ---------------------------------------------------------------------------
# Manifest enrichment + index (source of truth -> cache)
# ---------------------------------------------------------------------------


def _enrich_manifest(run_dir: Path, *, run_record: dict[str, Any]) -> Path:
    from omicsclaw.common.manifest import (  # local import keeps the graph shallow
        PipelineManifest,
        read_manifest,
        save_manifest,
    )

    manifest = read_manifest(run_dir) or PipelineManifest()
    run_meta = dict(manifest.metadata.get("run", {}))
    run_meta.update({k: v for k, v in run_record.items() if v is not None})
    manifest.metadata["run"] = run_meta
    return save_manifest(run_dir, manifest)


def _index_record(
    *,
    project_id: str,
    run_id: str,
    skill: str,
    method: str | None,
    dataset: str,
    status: str,
    manifest_mtime: float,
    path_rel: str,
) -> dict[str, Any]:
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "skill": skill,
        "method": method or "",
        "dataset": dataset,
        "status": status,
        "manifest_mtime": manifest_mtime,
        "path_rel": path_rel,
        "recorded_at": _utcnow_iso(),
    }


def _append_index_line_unlocked(index_path: Path, record: dict[str, Any]) -> None:
    """Append one row while the stable Project index lock is held."""

    line = json.dumps(record, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    fd = _open_regular_single_link_file(
        index_path,
        flags=os.O_WRONLY | os.O_APPEND,
        create=True,
        label=RUN_INDEX_FILENAME,
    )
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_index_line(index_path: Path, record: dict[str, Any]) -> None:
    """Publish one complete row under the stable per-Project index lock."""

    with _index_mutation_lock(index_path.parent):
        _append_index_line_unlocked(index_path, record)


def finalize_run(
    run_dir: str | Path,
    *,
    skill: str,
    status: str,
    method: str | None = None,
    dataset: str = "",
    dataset_label: str = "",
    surface: str = "",
    input_path: str | None = None,
) -> Path:
    """Record a finished Run: enrich ``manifest.json`` then append ``index.jsonl``.

    ``manifest.json`` is the write-truth; the index line is a derived cache
    carrying its ``mtime`` so a reader can detect drift and rebuild (constraint 5/8).
    Safe to call from any Surface's finalize path; derives project + run identity
    from the final on-disk location, so it does not care who created the dir.
    """
    run_dir = Path(run_dir)
    project_dir = run_dir.parent
    project_id = _project_id_of(project_dir)
    run_id = run_dir.name
    ds = dataset or dataset_label or _dataset_from_run_id(run_id)

    run_record = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "skill": skill,
        "method": method or "",
        "dataset": ds,
        "status": status,
        "surface": surface,
        "input_path": input_path,
        "recorded_at": _utcnow_iso(),
    }
    with _index_mutation_lock(project_dir):
        manifest_path = _enrich_manifest(run_dir, run_record=run_record)
        try:
            mtime = manifest_path.stat().st_mtime
        except OSError as exc:
            raise RuntimeError(
                f"manifest was not durably published for Run {run_id!r}"
            ) from exc

        _append_index_line_unlocked(
            project_dir / RUN_INDEX_FILENAME,
            _index_record(
                project_id=project_id,
                run_id=run_id,
                skill=skill,
                method=method,
                dataset=ds,
                status=status,
                manifest_mtime=mtime,
                path_rel=run_id,
            ),
        )
    return manifest_path


def _dataset_from_run_id(run_id: str) -> str:
    """Best-effort recover the dataset token (``…__<dataset>-<uid8>``)."""
    tail = run_id.rsplit("__", 1)[-1]
    if "-" in tail:
        return tail.rsplit("-", 1)[0]
    return tail


# ---------------------------------------------------------------------------
# Listing + lookup (the cache and its rebuild)
# ---------------------------------------------------------------------------


def _looks_like_run_dir(path: Path) -> bool:
    return (
        not is_filesystem_alias(path)
        and path.is_dir()
        and bool(_RUN_DIR_RE.match(path.name))
    )


def iter_run_dirs(output_root: str | Path) -> Iterator[tuple[Path, Path]]:
    """Yield ``(project_dir, run_dir)`` for every Run under the output root.

    Descends one Project level and also tolerates a legacy run dir sitting at
    the root (treated as the ``default`` project) for the no-migration cut-over.
    """
    output_root = Path(output_root)
    if not _is_unaliased_directory(output_root):
        return
    try:
        entries = sorted(output_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if is_filesystem_alias(entry) or not entry.is_dir():
            continue
        if _looks_like_run_dir(entry):
            yield output_root, entry  # legacy root-level run -> default
            continue
        # A project directory: its children are runs.
        try:
            children = sorted(entry.iterdir())
        except OSError:
            continue
        for child in children:
            if _looks_like_run_dir(child):
                yield entry, child


def find_run_dir(output_root: str | Path, run_id: str) -> Path | None:
    """Resolve ``run_id`` -> absolute path via a project-aware lookup (constraint 4).

    Never concatenates ``output_root / run_id`` blindly. Returns ``None`` if not
    found or if the candidate escapes the output root / is a symlink.
    """
    output_root = Path(output_root)
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        return None
    if not _is_unaliased_directory(output_root):
        return None
    # Legacy root-level run.
    candidates: list[Path] = [output_root / run_id]
    try:
        for entry in output_root.iterdir():
            if not is_filesystem_alias(entry) and entry.is_dir():
                candidates.append(entry / run_id)
    except OSError:
        pass
    for cand in candidates:
        if not is_filesystem_alias(cand) and cand.is_dir():
            try:
                assert_under_root(cand, output_root)
            except ValueError:
                continue
            return cand
    return None


def read_index(project_dir: str | Path) -> list[dict[str, Any]]:
    """Parsed ``index.jsonl`` rows, skipping any corrupt/half-written line."""
    project_path = Path(project_dir)
    path = project_path / RUN_INDEX_FILENAME
    lock_path = project_path / _INDEX_LOCK_FILENAME
    if not os.path.lexists(path) and not os.path.lexists(lock_path):
        return []
    try:
        with _index_mutation_lock(project_path):
            text = _read_regular_single_link_text(path, label=RUN_INDEX_FILENAME)
    except (OSError, RuntimeError):
        return []
    if text is None:
        return []
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


# ---------------------------------------------------------------------------
# CLI project ergonomics: list / resolve a name / the "current project" pointer
# ---------------------------------------------------------------------------


def list_projects(output_root: str | Path) -> list[dict[str, Any]]:
    """List Project directories under the output root with run counts.

    A legacy root-level run dir is not a project and is skipped.
    """
    root = Path(output_root)
    out: list[dict[str, Any]] = []
    if not _is_unaliased_directory(root):
        return out
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if (
            is_filesystem_alias(entry)
            or not entry.is_dir()
            or _looks_like_run_dir(entry)
        ):
            continue
        meta = read_project_meta(entry)
        try:
            runs = sum(1 for c in entry.iterdir() if _looks_like_run_dir(c))
        except OSError:
            runs = 0
        if not meta and runs == 0 and entry.name != DEFAULT_PROJECT_ID:
            continue  # not a recognisable project (e.g. an unrelated stray dir)
        pid = str(meta.get("project_id") or (
            DEFAULT_PROJECT_ID if entry.name == DEFAULT_PROJECT_ID else entry.name
        ))
        out.append({
            "dir": entry.name,
            "project_id": pid,
            "display_name": str(meta.get("display_name") or pid),
            "runs": runs,
        })
    return out


def resolve_cli_project(output_root: str | Path, name: str) -> tuple[str, str]:
    """Map a user-typed ``--project`` string to ``(project_id, display_name)``.

    Matches an existing project by id / dir name / display name; otherwise mints
    a new project whose id is the readable slug of the name (stable + predictable
    for CLI use, distinct from Bench's uuid thread ids).
    """
    name = (name or "").strip()
    if not name:
        return ("", "")
    for proj in list_projects(output_root):
        if name in (proj["project_id"], proj["dir"], proj["display_name"]):
            return (proj["project_id"], proj["display_name"])
    pid = slugify_token(name, max_len=64) or f"proj-{_short_hash(name)}"
    return (pid, name)


def _parse_current_project_text(text: str | None) -> tuple[str, str]:
    if text is None:
        return ("", "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ("", "")
    if not isinstance(data, dict):
        return ("", "")
    project_id = data.get("project_id")
    display_name = data.get("display_name")
    if not isinstance(project_id, str) or not project_id.strip():
        return ("", "")
    normalized_id = project_id.strip()
    return (
        normalized_id,
        display_name if isinstance(display_name, str) else normalized_id,
    )


def peek_current_project(output_root: str | Path) -> tuple[str, str]:
    """Read the bounded CLI navigation hint without locks or filesystem writes."""

    path = Path(output_root) / CURRENT_PROJECT_FILENAME
    if not os.path.lexists(path):
        return ("", "")
    return _parse_current_project_text(
        _read_bounded_regular_single_link_snapshot(
            path,
            label=CURRENT_PROJECT_FILENAME,
            max_bytes=_CURRENT_PROJECT_MAX_BYTES,
        )
    )


def get_current_project(output_root: str | Path) -> tuple[str, str]:
    """Active CLI project ``(project_id, display_name)`` or ``("", "")``."""
    root = Path(output_root)
    path = root / CURRENT_PROJECT_FILENAME
    lock_path = root / _CURRENT_PROJECT_LOCK_FILENAME
    if not os.path.lexists(path) and not os.path.lexists(lock_path):
        return ("", "")
    try:
        with _current_project_lock(root):
            text = _read_bounded_regular_single_link_snapshot(
                path,
                label=CURRENT_PROJECT_FILENAME,
                max_bytes=_CURRENT_PROJECT_MAX_BYTES,
            )
    except (OSError, RuntimeError):
        return ("", "")
    return _parse_current_project_text(text)


def set_current_project(output_root: str | Path, project_id: str, display_name: str = "") -> None:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("current Project requires a non-empty string project_id")
    project_id = project_id.strip()
    root = Path(output_root)
    if _has_filesystem_alias_component(root):
        raise RuntimeError("refusing to set current Project through an aliased output root")
    root.mkdir(parents=True, exist_ok=True)
    with _current_project_lock(root):
        _atomic_write_json(
            root / CURRENT_PROJECT_FILENAME,
            {"project_id": project_id, "display_name": display_name or project_id},
        )


def clear_current_project(output_root: str | Path) -> None:
    """Remove a real current-Project pointer; missing is an idempotent no-op.

    Unsafe aliases and non-regular entries raise ``RuntimeError`` because this
    operation mutates filesystem state and must not silently unlink them.
    """
    root = Path(output_root)
    path = root / CURRENT_PROJECT_FILENAME
    lock_path = root / _CURRENT_PROJECT_LOCK_FILENAME
    if not os.path.lexists(path) and not os.path.lexists(lock_path):
        return
    with _current_project_lock(root):
        try:
            fd = _open_regular_single_link_file(
                path,
                flags=os.O_RDONLY,
                create=False,
                label=CURRENT_PROJECT_FILENAME,
            )
        except FileNotFoundError:
            return
        try:
            opened = os.fstat(fd)
            current = path.lstat()
            _validate_regular_single_link_stat(
                current,
                label=CURRENT_PROJECT_FILENAME,
            )
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError(
                    "refusing changed current Project pointer before unlink"
                )
        finally:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            return


def rebuild_index(project_dir: str | Path) -> int:
    """Rewrite ``index.jsonl`` from a walk of the Project's Run dirs.

    The Run dirs + ``manifest.json`` are the source of truth, so the cache can
    always be regenerated (constraint 5). Returns the number of runs indexed.
    """
    from omicsclaw.common.manifest import read_manifest

    project_dir = Path(project_dir)
    with _index_mutation_lock(project_dir):
        project_id = _project_id_of(project_dir)
        lines: list[str] = []
        count = 0
        for child in sorted(project_dir.iterdir()) if project_dir.is_dir() else []:
            if not _looks_like_run_dir(child):
                continue
            manifest = read_manifest(child)
            run_meta = (manifest.metadata.get("run", {}) if manifest else {}) or {}
            manifest_path = child / "manifest.json"
            mtime = manifest_path.stat().st_mtime if manifest_path.exists() else 0.0
            rec = _index_record(
                project_id=project_id,
                run_id=child.name,
                skill=str(run_meta.get("skill", "")),
                method=str(run_meta.get("method", "")) or None,
                dataset=(
                    str(run_meta.get("dataset", ""))
                    or _dataset_from_run_id(child.name)
                ),
                status=str(run_meta.get("status", "")),
                manifest_mtime=mtime,
                path_rel=child.name,
            )
            lines.append(json.dumps(rec, ensure_ascii=False))
            count += 1
        index_path = project_dir / RUN_INDEX_FILENAME
        atomic_write_owned_output_text(
            index_path,
            output_root=project_dir,
            text=("\n".join(lines) + "\n") if lines else "",
            label=RUN_INDEX_FILENAME,
        )
    return count
