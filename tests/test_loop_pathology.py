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
    detect_phantom_completion,
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


# ── Phantom completion (ADR 0027) ────────────────────────────────────


def test_phantom_disabled_never_fires() -> None:
    # Cloud providers (guard off) are untouched even on a claiming message.
    state = LoopState()
    _push_call(state, "inspect_data", "d")
    assert (
        detect_phantom_completion(
            content="I will run the preprocessing pipeline now.",
            state=state,
            enabled=False,
        )
        is None
    )


def test_phantom_fires_when_claiming_work_with_no_execution_tool() -> None:
    # The reproduced bug: explored with prep tools, then narrated a claim
    # without ever calling an execution tool.
    state = LoopState()
    _push_call(state, "inspect_data", "d")
    _push_call(state, "resolve_capability", "d")
    signal = detect_phantom_completion(
        content="I will proceed with the preprocessing pipeline (QC, normalization).",
        state=state,
        enabled=True,
    )
    assert signal is not None
    assert signal.kind == "phantom_completion"
    assert signal.tool_name is None
    assert signal.count == 1
    assert signal.iteration == state.iteration


def test_phantom_fires_on_turn_zero_with_no_prior_tools() -> None:
    # A claim on the very first turn (no tools yet) is still a phantom.
    state = LoopState()
    signal = detect_phantom_completion(
        content="我已启动空间转录组预处理分析，执行情况报告：QC 已生成。",
        state=state,
        enabled=True,
    )
    assert signal is not None
    assert signal.kind == "phantom_completion"


def test_phantom_silent_on_conversational_reply() -> None:
    # "介绍你自己" style answer: no claim of analysis work -> not a phantom.
    state = LoopState()
    assert (
        detect_phantom_completion(
            content="你好，我是 OmicsBot，可以帮你做多组学分析。",
            state=state,
            enabled=True,
        )
        is None
    )


def test_phantom_silent_after_execution_tool_ran() -> None:
    # Genuine post-run summary: an execution tool actually ran, so a results
    # description is legitimate, not a phantom.
    state = LoopState()
    _push_call(state, "inspect_data", "d")
    _push_call(state, "omicsclaw", "d")
    assert (
        detect_phantom_completion(
            content="Here are the results of the analysis report: QC passed.",
            state=state,
            enabled=True,
        )
        is None
    )


def test_phantom_silent_on_empty_content() -> None:
    state = LoopState()
    _push_call(state, "inspect_data", "d")
    assert (
        detect_phantom_completion(content="", state=state, enabled=True) is None
    )


def test_phantom_intent_markers_cover_en_and_zh() -> None:
    state = LoopState()
    for claim in (
        "I'll proceed with running the analysis.",
        "Running the analysis now and generating QC.",
        "正在执行预处理流程。",
        "已为您准备好 qc 和 count 图表。",
        # Real gemma4 narration that announces an intent/plan without acting.
        "我将采用 `spatial-preprocess` 指令。我将按照以下计划执行：1. 执行预处理。",
    ):
        signal = detect_phantom_completion(content=claim, state=state, enabled=True)
        assert signal is not None and signal.kind == "phantom_completion", claim


def test_phantom_silent_on_capability_intro() -> None:
    # A capability-describing intro ("我可以帮你…" / "I can help") is not a
    # commitment to act and must not trip the guard.
    state = LoopState()
    for reply in (
        "你好，我是 OmicsBot，可以帮你做多组学分析。",
        "Hi, I'm OmicsBot. I can help you analyze multi-omics data.",
    ):
        assert (
            detect_phantom_completion(content=reply, state=state, enabled=True) is None
        ), reply
