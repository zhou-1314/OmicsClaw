"""Verification-storm patterns the loop-pathology detector must catch.

Diagnosis 2026-07-25, from a real desktop trace (transcripts.db conversation
``55d32225…``, 12:04–12:12): one task burned all 20 tool iterations on 24 calls,
of which only ~5 did analysis work. The detector emitted **zero** signals for
the whole run, so nothing ever nudged the model off its loop.

The dominant waste was a *directory* verification storm — the model hunted for
output files in a path the analysis had never written, listing it three times.
``list_directory`` was invisible to the detector: it carries no read target, and
"Directory not found" comes back as a *successful* call, so neither the
repeated-read nor the repeated-failure lane could see it.
"""

from __future__ import annotations

from omicsclaw.runtime.agent.loop_pathology import (
    REPEATED_READ_THRESHOLD,
    detect,
    read_access_target,
)
from omicsclaw.runtime.agent.loop_state import (
    LoopState,
    ToolCallRecord,
    compute_args_digest,
)

GHOST_DIR = "/workspace/output/synthetic_analysis"


def _push(state: LoopState, name: str, arguments: dict, *, succeeded: bool = True) -> None:
    state.iteration += 1
    state.tool_calls.append(
        ToolCallRecord(
            name=name,
            args_digest=compute_args_digest(arguments),
            iteration=state.iteration,
            succeeded=succeeded,
            target=read_access_target(name, arguments),
        )
    )


def test_repeatedly_listing_the_same_directory_is_a_pathology():
    """The real trace's dominant waste: the same directory listed three times,
    interleaved with other calls so pingpong's tighter window never sees it."""
    state = LoopState()
    _push(state, "list_directory", {"path": GHOST_DIR})
    _push(state, "autonomous_analysis_execute", {"goal": "redo the analysis"})
    _push(state, "list_directory", {"path": GHOST_DIR})
    _push(state, "file_read", {"path": "/workspace/run/answer.txt"})
    _push(state, "list_directory", {"path": GHOST_DIR})

    signal = detect(state)
    assert signal is not None, "three identical directory listings must be flagged"
    assert signal.kind == "repeated_read"
    assert signal.target == GHOST_DIR
    assert signal.count >= REPEATED_READ_THRESHOLD


def test_listing_different_directories_is_not_a_pathology():
    """Walking a tree is legitimate: only the *same* directory repeating counts."""
    state = LoopState()
    _push(state, "list_directory", {"path": "/workspace/a"})
    _push(state, "list_directory", {"path": "/workspace/b"})
    _push(state, "list_directory", {"path": "/workspace/c"})

    assert detect(state) is None
