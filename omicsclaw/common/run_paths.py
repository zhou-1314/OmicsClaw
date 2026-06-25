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
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX advisory locking for the index append; degrade gracefully elsewhere.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAVE_FCNTL = False

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


def read_project_meta(project_dir: str | Path) -> dict[str, Any]:
    path = Path(project_dir) / PROJECT_META_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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
            if entry.is_dir() and entry.name.endswith(suffix):
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
def _project_resolution_lock(output_root: Path) -> Iterator[None]:
    """Serialize project short-id scan/create/write across threads/processes."""
    with _PROJECT_LOCK_MUTEX:
        fd: int | None = None
        if _HAVE_FCNTL:
            fd = os.open(
                str(output_root / _PROJECT_LOCK_FILENAME),
                os.O_WRONLY | os.O_CREAT,
                0o644,
            )
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
    pid = (project_id or "").strip() or DEFAULT_PROJECT_ID

    if pid == DEFAULT_PROJECT_ID:
        project_dir = output_root / DEFAULT_PROJECT_ID
        if create:
            output_root.mkdir(parents=True, exist_ok=True)
            with _project_resolution_lock(output_root):
                if project_dir.is_symlink():
                    raise ValueError(f"refusing symlinked project directory: {project_dir}")
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
            if project_dir.is_symlink():
                raise ValueError(f"refusing symlinked project directory: {project_dir}")
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
    if project_dir.is_symlink():
        raise ValueError(f"refusing symlinked project directory: {project_dir}")

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


def _enrich_manifest(run_dir: Path, *, run_record: dict[str, Any]) -> Path | None:
    from omicsclaw.common.manifest import (  # local import keeps the graph shallow
        MANIFEST_FILENAME,
        PipelineManifest,
        read_manifest,
        save_manifest,
    )

    manifest = read_manifest(run_dir) or PipelineManifest()
    run_meta = dict(manifest.metadata.get("run", {}))
    run_meta.update({k: v for k, v in run_record.items() if v is not None})
    manifest.metadata["run"] = run_meta
    try:
        return save_manifest(run_dir, manifest)
    except OSError:  # pragma: no cover - defensive
        return run_dir / MANIFEST_FILENAME


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


def _append_index_line(index_path: Path, record: dict[str, Any]) -> None:
    """Single locked ``O_APPEND`` write so concurrent runs never interleave (constraint 5)."""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    fd = os.open(str(index_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
    finally:
        if _HAVE_FCNTL:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover
                pass
        os.close(fd)


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
    manifest_path = _enrich_manifest(run_dir, run_record=run_record)
    mtime = 0.0
    if manifest_path is not None and manifest_path.exists():
        try:
            mtime = manifest_path.stat().st_mtime
        except OSError:  # pragma: no cover
            mtime = 0.0

    _append_index_line(
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
    return manifest_path if manifest_path is not None else run_dir


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
    return path.is_dir() and bool(_RUN_DIR_RE.match(path.name))


def iter_run_dirs(output_root: str | Path) -> Iterator[tuple[Path, Path]]:
    """Yield ``(project_dir, run_dir)`` for every Run under the output root.

    Descends one Project level and also tolerates a legacy run dir sitting at
    the root (treated as the ``default`` project) for the no-migration cut-over.
    """
    output_root = Path(output_root)
    if not output_root.is_dir():
        return
    try:
        entries = sorted(output_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
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
    # Legacy root-level run.
    candidates: list[Path] = [output_root / run_id]
    try:
        for entry in output_root.iterdir():
            if entry.is_dir():
                candidates.append(entry / run_id)
    except OSError:
        pass
    for cand in candidates:
        if cand.is_dir() and not cand.is_symlink():
            try:
                assert_under_root(cand, output_root)
            except ValueError:
                continue
            return cand
    return None


def read_index(project_dir: str | Path) -> list[dict[str, Any]]:
    """Parsed ``index.jsonl`` rows, skipping any corrupt/half-written line."""
    path = Path(project_dir) / RUN_INDEX_FILENAME
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
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
    if not root.is_dir():
        return out
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir() or _looks_like_run_dir(entry):
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


def get_current_project(output_root: str | Path) -> tuple[str, str]:
    """Active CLI project ``(project_id, display_name)`` or ``("", "")``."""
    path = Path(output_root) / CURRENT_PROJECT_FILENAME
    if not path.exists():
        return ("", "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (str(data.get("project_id", "")), str(data.get("display_name", "")))
    except (json.JSONDecodeError, OSError):
        return ("", "")


def set_current_project(output_root: str | Path, project_id: str, display_name: str = "") -> None:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        root / CURRENT_PROJECT_FILENAME,
        {"project_id": project_id, "display_name": display_name or project_id},
    )


def clear_current_project(output_root: str | Path) -> None:
    path = Path(output_root) / CURRENT_PROJECT_FILENAME
    if path.exists():
        path.unlink()


def rebuild_index(project_dir: str | Path) -> int:
    """Rewrite ``index.jsonl`` from a walk of the Project's Run dirs.

    The Run dirs + ``manifest.json`` are the source of truth, so the cache can
    always be regenerated (constraint 5). Returns the number of runs indexed.
    """
    from omicsclaw.common.manifest import read_manifest

    project_dir = Path(project_dir)
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
            dataset=str(run_meta.get("dataset", "")) or _dataset_from_run_id(child.name),
            status=str(run_meta.get("status", "")),
            manifest_mtime=mtime,
            path_rel=child.name,
        )
        lines.append(json.dumps(rec, ensure_ascii=False))
        count += 1
    index_path = project_dir / RUN_INDEX_FILENAME
    tmp = index_path.with_name(f".{RUN_INDEX_FILENAME}.tmp-{os.getpid()}")
    tmp.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    os.replace(tmp, index_path)
    return count
