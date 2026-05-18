"""ADR 0009 L4 — CLI ESC/Ctrl+C handler sets ``envelope.cancel_event``.

Per ADR 0009 §Verification, L4 is verified by manual smoke (``oc
interactive`` with a long-running skill, press Ctrl+C, verify prompt
returns and subprocess is gone). The full interactive REPL path is not
amenable to automated testing without a TTY.

This file provides a source-shape tripwire that fires if the wiring is
ever silently removed during a refactor.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


def _read_interactive_module_source() -> str:
    from omicsclaw.surfaces.cli import interactive

    return Path(inspect.getsourcefile(interactive)).read_text(encoding="utf-8")


def test_cli_interactive_constructs_envelope_with_cancel_event():
    """The CLI's per-turn ``MessageEnvelope`` must carry a
    ``cancel_event`` so the ESC/Ctrl+C handler has something to set."""
    source = _read_interactive_module_source()
    assert "MessageEnvelope(" in source, "CLI no longer uses MessageEnvelope"
    # Find the envelope construction near the dispatch consumer and confirm
    # ``cancel_event=`` is being passed.
    envelope_idx = source.index("MessageEnvelope(")
    snippet = source[envelope_idx : envelope_idx + 1500]
    assert "cancel_event=cancel_event" in snippet, (
        "CLI's MessageEnvelope construction is missing cancel_event; "
        "ADR 0009 L4 wiring has regressed."
    )


def test_cli_interactive_signals_cancel_event_before_task_cancel():
    """In the ESC/Ctrl+C handler, ``cancel_event.set()`` must precede
    ``llm_task.cancel()`` — otherwise the abort doesn't reach the
    skill subprocess and the user sees the prompt return while a
    Python child keeps burning CPU."""
    source = _read_interactive_module_source()
    # Find the watcher_task-resolved branch (User interrupted via ESC/Ctrl+C).
    marker = "User interrupted via ESC or Ctrl+C"
    assert marker in source, "ESC/Ctrl+C handler comment marker is gone"
    branch_idx = source.index(marker)
    branch = source[branch_idx : branch_idx + 800]

    set_pos = branch.find("cancel_event.set()")
    cancel_pos = branch.find("llm_task.cancel()")

    assert set_pos != -1, (
        "ESC/Ctrl+C handler no longer signals cancel_event.set(); "
        "ADR 0009 L4 wiring has regressed — the skill subprocess will "
        "be orphaned after user interrupt."
    )
    assert cancel_pos != -1, "ESC/Ctrl+C handler no longer calls llm_task.cancel()"
    assert set_pos < cancel_pos, (
        "cancel_event.set() must come before llm_task.cancel() so the "
        "abort signal propagates to subprocess_driver before the dispatch "
        "coroutine gets cancelled at its next await."
    )
