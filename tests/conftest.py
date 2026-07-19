"""Shared pytest configuration for OmicsClaw."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("NUMBA_DISABLE_JIT", "1")


def _dotenv_key_names() -> tuple[str, ...]:
    """Key names the repo-root ``.env`` would inject, without setting them.

    Uses ``python-dotenv``'s read-only ``dotenv_values`` (the same library
    ``omicsclaw/common/runtime_env.py`` uses to actually load the file) so
    this never has the side effect the fixture below guards against, and
    never duplicates that file's own parsing logic. Falls back to an empty
    tuple if the package or the file isn't present — nothing to isolate.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        from dotenv import dotenv_values
    except ImportError:
        return ()
    if not env_path.is_file():
        return ()
    return tuple(dotenv_values(env_path).keys())


_DOTENV_KEYS = _dotenv_key_names()


@pytest.fixture(autouse=True)
def _isolated_dotenv_environ(monkeypatch):
    """Clear repo-root-``.env``-sourced keys before every test.

    ``omicsclaw.runtime.agent.state`` and ``omicsclaw.routing.llm_router``
    each call ``load_project_dotenv(..., override=False)`` at *import* time,
    pulling a developer's local ``.env`` (real ``LLM_PROVIDER``,
    ``LLM_API_KEY``, ``OMICSCLAW_WORKSPACE``, etc.) into the process
    environment. Because Python imports a module only once per process,
    whichever test first triggers that import — directly or transitively,
    possibly during collection rather than any specific test's own body —
    permanently leaks those values into every test that runs afterward in
    the same invocation, silently changing results in tests that assert on
    "no provider configured" / "no workspace set" behavior based purely on
    unrelated execution order. Deleting exactly the keys ``.env`` could set
    (never touching anything else, including pytest's own
    ``PYTEST_CURRENT_TEST`` — a blanket ``os.environ`` snapshot/restore
    tried that first and broke pytest's internal teardown) makes results
    depend only on what each test explicitly sets via ``monkeypatch``,
    regardless of order or what ran before it. ``monkeypatch.delenv`` restores
    pytest's own recorded prior value automatically at teardown, so this
    only ever *narrows* the environment during the test body, never widens
    or corrupts it afterward.
    """
    for key in _DOTENV_KEYS:
        monkeypatch.delenv(key, raising=False)
