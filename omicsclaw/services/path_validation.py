"""Filesystem path validation + safe destination resolution + file discovery.

Carved out of ``bot/core.py`` per ADR 0001. Every helper enforces that the
resolved path lives inside one of the trusted data directories (or under
``OMICSCLAW_DIR``) — paths that escape those roots are rejected and audit-
logged. ``OMICSCLAW_DIR`` / ``DATA_DIR`` / ``EXAMPLES_DIR`` / ``OUTPUT_DIR``
are owned by ``omicsclaw.runtime.agent.state``; we late-import them on first use to avoid a
load-order circular.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from omicsclaw.services.audit import audit

logger = logging.getLogger("omicsclaw.omicsclaw.services.path_validation")


def _bot_core_dirs():
    """Late import of the omicsclaw.runtime.agent.state directory globals."""
    from omicsclaw.runtime.agent.state import DATA_DIR, EXAMPLES_DIR, OMICSCLAW_DIR, OUTPUT_DIR
    return OMICSCLAW_DIR, DATA_DIR, EXAMPLES_DIR, OUTPUT_DIR


def sanitize_filename(filename: str) -> str:
    filename = Path(filename).name
    filename = re.sub(r"[\x00-\x1f]", "", filename)
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    return filename or "unnamed_file"


def resolve_dest(folder: str | None, default: Path | None = None) -> Path:
    OMICSCLAW_DIR, DATA_DIR, _, _ = _bot_core_dirs()
    fallback = default if default is not None else DATA_DIR
    dest = Path(folder) if folder else fallback
    if not dest.is_absolute():
        dest = OMICSCLAW_DIR / dest
    try:
        dest.resolve().relative_to(OMICSCLAW_DIR.resolve())
    except ValueError:
        logger.warning(f"Path escape blocked: {dest}")
        audit("security", severity="HIGH", detail="path_escape_blocked", attempted_path=str(dest))
        dest = fallback
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def validate_path(filepath: Path, allowed_root: Path) -> bool:
    try:
        filepath.resolve().relative_to(allowed_root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Trusted data directories + file discovery
# ---------------------------------------------------------------------------

TRUSTED_DATA_DIRS: list[Path] = []


def _build_trusted_dirs() -> list[Path]:
    """Build the list of directories where data files may be read from."""
    _, DATA_DIR, EXAMPLES_DIR, OUTPUT_DIR = _bot_core_dirs()
    dirs = [DATA_DIR, EXAMPLES_DIR, OUTPUT_DIR]
    extra = os.environ.get("OMICSCLAW_DATA_DIRS", os.environ.get("SPATIALCLAW_DATA_DIRS", ""))
    if extra:
        for d in extra.split(","):
            d = d.strip()
            if d:
                p = Path(d)
                if p.is_absolute() and p.is_dir():
                    dirs.append(p)
                else:
                    logger.warning(f"OMICSCLAW_DATA_DIRS: ignoring '{d}' (not an absolute directory)")
    return dirs


def _ensure_trusted_dirs():
    # Mutate in place — omicsclaw.runtime.agent.state / omicsclaw.runtime.tools.builders.agent_executors / omicsclaw.surfaces.desktop.server
    # all import ``TRUSTED_DATA_DIRS`` by reference at module load time.
    # A rebind here would leave those importers stuck on the original empty
    # list, defeating the trusted-dir check (and silently breaking server.py's
    # workspace.append() handshake).
    if not TRUSTED_DATA_DIRS:
        TRUSTED_DATA_DIRS[:] = _build_trusted_dirs()
        logger.info(f"Trusted data dirs: {[str(d) for d in TRUSTED_DATA_DIRS]}")


def _is_trusted_root(directory: Path) -> bool:
    """True when *directory* lies inside a trusted data dir or the project root."""
    OMICSCLAW_DIR, _, _, _ = _bot_core_dirs()
    try:
        resolved = directory.resolve()
    except OSError:
        return False
    for root in (*TRUSTED_DATA_DIRS, OMICSCLAW_DIR):
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def validate_input_path(filepath: str, *, allow_dir: bool = False) -> Path | None:
    """Validate that a user-supplied path points to a real file/dir in a trusted directory.

    Returns resolved Path if valid, None otherwise.
    """
    _ensure_trusted_dirs()
    OMICSCLAW_DIR, DATA_DIR, _, _ = _bot_core_dirs()
    p = Path(filepath).expanduser()
    if not p.is_absolute():
        def _matches(candidate: Path) -> bool:
            return candidate.exists() and (candidate.is_file() or (allow_dir and candidate.is_dir()))

        # Resolution order. The active Desktop workspace comes FIRST: the user's
        # files live there, so a workspace-relative path like ``data/x.h5ad`` must
        # bind to the workspace copy, not a same-named file under the project root
        # (this is what inspect_data / the autonomous engine should all agree on).
        # Then the project root, then the trusted data dirs, then a DATA_DIR fallback.
        search_roots: list[Path] = []
        workspace_env = os.environ.get("OMICSCLAW_WORKSPACE", "").strip()
        if workspace_env:
            ws_path = Path(workspace_env).expanduser()
            # Only prefer the workspace when it is itself a trusted location. An
            # untrusted OMICSCLAW_WORKSPACE must NOT shadow a valid trusted-dir
            # match: searching it first would resolve to a path the trailing trust
            # check then rejects, returning None instead of the trusted copy.
            if _is_trusted_root(ws_path):
                search_roots.append(ws_path)
        search_roots.append(OMICSCLAW_DIR)
        search_roots.extend(TRUSTED_DATA_DIRS)
        for d in search_roots:
            candidate = d / p
            if _matches(candidate):
                p = candidate
                break
        else:
            # Fall back to DATA_DIR (existence/trust checked below).
            p = DATA_DIR / p

    resolved = p.resolve()
    if not resolved.exists():
        return None
    if not resolved.is_file() and not (allow_dir and resolved.is_dir()):
        return None

    for trusted in TRUSTED_DATA_DIRS:
        try:
            resolved.relative_to(trusted.resolve())
            return resolved
        except ValueError:
            continue

    # Also allow files anywhere under project root
    try:
        resolved.relative_to(OMICSCLAW_DIR.resolve())
        return resolved
    except ValueError:
        pass

    logger.warning(f"Path not in trusted dirs: {resolved}")
    audit("security", severity="MEDIUM", detail="untrusted_path_rejected", path=str(resolved))
    return None


def discover_file(filename_or_pattern: str) -> list[Path]:
    """Search trusted data directories for files matching the given name or glob pattern.

    Returns a list of matching paths, sorted by modification time (newest first).
    """
    _ensure_trusted_dirs()

    # Handle absolute paths directly
    if filename_or_pattern.startswith('/'):
        p = Path(filename_or_pattern)
        if p.is_file():
            return [p]
        return []

    matches: list[Path] = []
    for d in TRUSTED_DATA_DIRS:
        if not d.exists():
            continue
        if "*" in filename_or_pattern or "?" in filename_or_pattern:
            matches.extend(f for f in d.rglob(filename_or_pattern) if f.is_file())
        else:
            exact = d / filename_or_pattern
            if exact.is_file():
                matches.append(exact)
            for f in d.rglob(filename_or_pattern):
                if f.is_file() and f not in matches:
                    matches.append(f)
    matches.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return matches
