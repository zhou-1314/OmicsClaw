"""Unit tests for ``omicsclaw.runtime.agent.loop_pathology`` — L0 gate
of ADR 0007.

Covers the six behaviours pinned in the ADR §Verification L0 list:

1. Empty state -> None.
2. 3 same-(name, digest) entries in last 6 -> None (below threshold).
3. 4 same-(name, digest) entries in last 6 -> PathologySignal(pingpong).
4. 4 same-tool_name failure entries in last 8 -> PathologySignal(repeated_failure).
5. Mixed populations -> correct discrimination (pingpong takes precedence).
6. args-digest sensitivity: alternating digests under same tool name
   do not fire pingpong.

Also covers LoopState bounded-deque truncation and the
``compute_args_digest`` stability guarantee.
"""

from __future__ import annotations

import pytest

from omicsclaw.runtime.agent.loop_pathology import (
    FAILURE_THRESHOLD,
    FAILURE_WINDOW,
    PINGPONG_THRESHOLD,
    PINGPONG_WINDOW,
    detect,
)
from omicsclaw.runtime.agent.loop_state import (
    LoopState,
    PathologySignal,
    ToolCallRecord,
    ToolErrorRecord,
    compute_args_digest,
)


def _push_call(state: LoopState, name: str, digest: str, *, succeeded: bool = True) -> None:
    state.iteration += 1
    state.tool_calls.append(
        ToolCallRecord(
            name=name,
            args_digest=digest,
            iteration=state.iteration,
            succeeded=succeeded,
        )
    )


def _push_error(state: LoopState, tool_name: str, *, error_class: str = "RuntimeError") -> None:
    state.iteration += 1
    state.errors.append(
        ToolErrorRecord(
            tool_name=tool_name,
            iteration=state.iteration,
            error_class=error_class,
            message_head="boom",
        )
    )


def test_detect_returns_none_on_empty_state() -> None:
    state = LoopState()
    assert detect(state) is None


def test_three_same_calls_below_pingpong_threshold() -> None:
    state = LoopState()
    for _ in range(3):
        _push_call(state, "read_file", "digest-A")
    for _ in range(3):
        _push_call(state, "noop", f"unique-{state.iteration}")
    assert detect(state) is None


def test_four_same_calls_in_last_six_fires_pingpong() -> None:
    state = LoopState()
    for _ in range(2):
        _push_call(state, "noop", f"unique-{state.iteration}")
    for _ in range(PINGPONG_THRESHOLD):
        _push_call(state, "read_file", "digest-A")
    signal = detect(state)
    assert signal is not None
    assert signal.kind == "pingpong"
    assert signal.tool_name == "read_file"
    assert signal.count == PINGPONG_THRESHOLD
    assert signal.iteration == state.iteration


def test_pingpong_respects_window_size() -> None:
    state = LoopState()
    for _ in range(PINGPONG_THRESHOLD):
        _push_call(state, "read_file", "digest-A")
    for _ in range(PINGPONG_WINDOW):
        _push_call(state, "noop", f"unique-{state.iteration}")
    assert detect(state) is None


def test_repeated_failure_fires_after_threshold() -> None:
    state = LoopState()
    for i in range(FAILURE_WINDOW - FAILURE_THRESHOLD):
        _push_error(state, f"flaky_{i}")
    for _ in range(FAILURE_THRESHOLD):
        _push_error(state, "bad_tool")
    signal = detect(state)
    assert signal is not None
    assert signal.kind == "repeated_failure"
    assert signal.tool_name == "bad_tool"
    assert signal.count == FAILURE_THRESHOLD


def test_pingpong_takes_precedence_over_failure() -> None:
    state = LoopState()
    for _ in range(PINGPONG_THRESHOLD):
        _push_call(state, "read_file", "digest-A", succeeded=False)
        _push_error(state, "read_file")
    signal = detect(state)
    assert signal is not None
    assert signal.kind == "pingpong"


def test_args_digest_sensitivity_prevents_false_pingpong() -> None:
    state = LoopState()
    for i in range(PINGPONG_THRESHOLD * 2):
        digest = "digest-A" if i % 2 == 0 else "digest-B"
        _push_call(state, "grep", digest)
    assert detect(state) is None


def test_compute_args_digest_is_stable_under_key_reordering() -> None:
    a = compute_args_digest({"pattern": "X", "path": "/tmp/foo"})
    b = compute_args_digest({"path": "/tmp/foo", "pattern": "X"})
    assert a == b


def test_compute_args_digest_distinguishes_values() -> None:
    a = compute_args_digest({"pattern": "X"})
    b = compute_args_digest({"pattern": "Y"})
    assert a != b


def test_compute_args_digest_handles_non_serialisable() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "Opaque()"

    digest = compute_args_digest({"obj": Opaque()})
    assert isinstance(digest, str) and len(digest) == 40


def test_loop_state_deques_are_bounded() -> None:
    state = LoopState()
    for i in range(50):
        _push_call(state, f"tool_{i}", f"digest-{i}")
    assert len(state.tool_calls) == 20
    assert state.tool_calls[0].name == "tool_30"

    for i in range(20):
        _push_error(state, f"tool_{i}")
    assert len(state.errors) == 10


def test_signals_list_is_unbounded() -> None:
    state = LoopState()
    for i in range(100):
        state.signals.append(
            PathologySignal(
                kind="pingpong",
                tool_name=f"t_{i}",
                iteration=i,
                count=4,
                reason="x",
            )
        )
    assert len(state.signals) == 100


def test_pathology_signal_is_immutable() -> None:
    signal = PathologySignal(
        kind="pingpong",
        tool_name="t",
        iteration=1,
        count=4,
        reason="x",
    )
    with pytest.raises(AttributeError):
        signal.count = 5
