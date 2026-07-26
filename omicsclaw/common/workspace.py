"""Canonical OmicsClaw workspace-directory resolution.

Lives here — with a stdlib-only import surface — so that every consumer can
reach it without dragging in a heavy dependency chain. ``omicsclaw.runtime.agent.state``
(the previous home of this logic) imports ``openai`` and ``requests`` at module
load; the CLI entrypoint must be able to answer ``--version`` without paying
that cost, so the resolver was carved out rather than imported from there.

Note that ``omicsclaw.skill.registry`` and ``omicsclaw.skill.scaffolder`` keep
their own ``OMICSCLAW_DIR`` definitions. Those deliberately resolve to the
directory *containing* the ``omicsclaw`` package so they can find the sibling
``skills`` package, which pip installs as a top-level package of its own. That
is a different question from "where is the user's writable workspace", which is
what this module answers.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["resolve_omicsclaw_dir"]


def resolve_omicsclaw_dir(start: Path | None = None) -> Path:
    """Find a writable OmicsClaw workspace directory.

    A source checkout wants the repo root, but two install shapes have no
    usable repo root and need a per-user writable fallback instead:

    1. **Pip-installed** (e.g. ``pip install omicsclaw``): the package
       lives under site-packages/, with no project tree above it — and it
       is usually read-only inside a packaged app bundle.
    2. **OmicsClaw-App bundled runtime**: a signed/notarized .app bundle
       on macOS puts site-packages under
       ``/Applications/.../Contents/Resources``, which is strictly
       read-only. The audit-log directory that ``omicsclaw.runtime.agent.state``
       creates from this path would raise ``PermissionError`` at import time.

    Resolution priority:
      1. ``OMICSCLAW_DIR`` env var (explicit override — honoured first
         so operators can point at a shared or external workspace).
      2. Source-tree layout — the nearest ancestor that holds the
         ``omicsclaw.py`` CLI entrypoint *next to* the ``omicsclaw/``
         package. The depth is searched, not assumed: this code lived at
         ``bot/core.py`` (one level under the root, so ``parent.parent``
         was correct), moved to ``omicsclaw/runtime/agent/state.py``
         (three levels under it) in the ADR 0001 carve-out, and now lives
         here (two levels under it). A hardcoded ``parent.parent`` made
         every source-tree / editable install fall silently through to
         step 3; searching keeps the answer stable across every future
         move. ``omicsclaw.py`` can never be an importable module (it
         would collide with the ``omicsclaw`` package), so it only exists
         in a real checkout — a marker that never false-matches
         site-packages.
      3. ``~/.omicsclaw`` — the per-user writable fallback used by
         pip-installed / npm-installed / bundled-runtime deployments.
         Mirrors the convention used by jupyter / matplotlib / mypy.

    ``start`` overrides the file the upward search begins from; it exists
    for tests and defaults to this module's own location.
    """
    env = os.getenv("OMICSCLAW_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    here = (start or Path(__file__)).resolve()
    for candidate in here.parents:
        if (candidate / "omicsclaw.py").is_file() and (candidate / "omicsclaw").is_dir():
            return candidate

    return (Path.home() / ".omicsclaw").resolve()
