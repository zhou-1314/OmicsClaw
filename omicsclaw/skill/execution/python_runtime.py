"""Resolve the Python interpreter used to run skill subprocesses.

Carved out of ``omicsclaw.runtime.agent.state`` so the low-level skill
runner (``omicsclaw.skill.runner``) can honour the ``OMICSCLAW_RUN_PYTHON``
override **without** importing the heavyweight bot-engine ``state`` module
(which pulls in openai/requests/providers and would invert the skill→runtime
layering). ``state`` re-exports :func:`get_skill_runner_python` for the
existing callers (``agent_executors``, the desktop health endpoint).

Previously ``runner.py`` hardcoded ``PYTHON = sys.executable``, so the
documented ``OMICSCLAW_RUN_PYTHON`` escape hatch (for deployments where the
app server runs in a lighter env than the scientific analysis stack) silently
did nothing on the main ``run_skill`` / ``arun_skill`` path.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path


def get_skill_runner_python() -> str:
    """Return the Python executable used for skill subprocesses.

    By default this is the current interpreter, but advanced deployments can
    override it with ``OMICSCLAW_RUN_PYTHON`` when the app server itself runs
    in a lighter environment than the scientific analysis stack.
    """
    candidate = str(os.getenv("OMICSCLAW_RUN_PYTHON", "") or "").strip()
    if not candidate:
        return sys.executable

    expanded = os.path.expanduser(candidate)
    if os.path.sep in expanded or (os.path.altsep and os.path.altsep in expanded):
        resolved_path = Path(expanded)
        if resolved_path.exists():
            return str(resolved_path.resolve())
        logging.getLogger("omicsclaw.bot").warning(
            "OMICSCLAW_RUN_PYTHON=%s does not exist; falling back to sys.executable=%s",
            candidate,
            sys.executable,
        )
        return sys.executable

    resolved = shutil.which(expanded)
    if resolved:
        return resolved

    logging.getLogger("omicsclaw.bot").warning(
        "OMICSCLAW_RUN_PYTHON=%s was not found on PATH; falling back to sys.executable=%s",
        candidate,
        sys.executable,
    )
    return sys.executable


__all__ = ["get_skill_runner_python"]
