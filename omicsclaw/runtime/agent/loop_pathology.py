"""Pathology detection over ``LoopState``.

Per ADR 0007. ``detect(state)`` is a pure function over the current
``LoopState``. It inspects the bounded tool-call and error histories
and returns at most one ``PathologySignal`` per call — the most recent
unhealthy pattern that crossed a threshold.

Two patterns are recognised today:

- **Pingpong** — the same ``(tool_name, args_digest)`` pair appears at
  least ``PINGPONG_THRESHOLD`` times within the trailing
  ``PINGPONG_WINDOW`` entries of ``state.tool_calls``. The args-digest
  granularity is intentional: ``grep(pattern="A")`` followed by
  ``grep(pattern="B")`` is *not* a ping-pong even though the tool name
  repeats.

- **Repeated failure** — the same ``tool_name`` appears at least
  ``FAILURE_THRESHOLD`` times within the trailing ``FAILURE_WINDOW``
  entries of ``state.errors``. Argument-level granularity is *not*
  used here — a tool that fails with different arguments four times
  is still a tool that is unreliable in this loop.

Defaults inherit the reference implementation's numbers (4-of-6, 4-of-8). Tune in a follow
up PR if real traces motivate changes; this module exposes the
thresholds as module-level constants so a test or a `/diagnostics`
slash command can monkeypatch them.
"""

from __future__ import annotations

from collections import Counter

from omicsclaw.runtime.agent.loop_state import (
    LoopState,
    PathologySignal,
    ToolCallRecord,
    ToolErrorRecord,
)


PINGPONG_WINDOW = 6
PINGPONG_THRESHOLD = 4

FAILURE_WINDOW = 8
FAILURE_THRESHOLD = 4


def detect(state: LoopState) -> PathologySignal | None:
    """Return the most-recent pathology that crossed its threshold, or None.

    Pingpong takes precedence over repeated_failure: a tool that keeps
    failing with the same arguments is also pinging-pong, and the
    pingpong message is the more actionable one ("you are looping",
    not "you are failing").

    The detector only returns *new* findings — a caller that fires this
    after every tool execution will see the same pattern hold until
    enough additional benign calls push it out of the trailing window.
    The caller is responsible for deduplicating signals into
    ``state.signals`` if it wants single-shot semantics; see
    ``run_query_engine`` for the integration.
    """
    pingpong = _detect_pingpong(state)
    if pingpong is not None:
        return pingpong
    return _detect_repeated_failure(state)


def _detect_pingpong(state: LoopState) -> PathologySignal | None:
    if not state.tool_calls:
        return None
    window: list[ToolCallRecord] = list(state.tool_calls)[-PINGPONG_WINDOW:]
    if len(window) < PINGPONG_THRESHOLD:
        return None
    counts = Counter((record.name, record.args_digest) for record in window)
    most_common = counts.most_common(1)
    if not most_common:
        return None
    (tool_name, _digest), count = most_common[0]
    if count < PINGPONG_THRESHOLD:
        return None
    return PathologySignal(
        kind="pingpong",
        tool_name=tool_name,
        iteration=window[-1].iteration,
        count=count,
        reason=(
            f"tool {tool_name!r} called {count} times with same arguments "
            f"in last {len(window)} tool invocations"
        ),
    )


def _detect_repeated_failure(state: LoopState) -> PathologySignal | None:
    if not state.errors:
        return None
    window: list[ToolErrorRecord] = list(state.errors)[-FAILURE_WINDOW:]
    if len(window) < FAILURE_THRESHOLD:
        return None
    counts = Counter(record.tool_name for record in window)
    most_common = counts.most_common(1)
    if not most_common:
        return None
    tool_name, count = most_common[0]
    if count < FAILURE_THRESHOLD:
        return None
    return PathologySignal(
        kind="repeated_failure",
        tool_name=tool_name,
        iteration=window[-1].iteration,
        count=count,
        reason=(
            f"tool {tool_name!r} failed {count} times "
            f"in last {len(window)} errors"
        ),
    )


__all__ = [
    "PINGPONG_WINDOW",
    "PINGPONG_THRESHOLD",
    "FAILURE_WINDOW",
    "FAILURE_THRESHOLD",
    "detect",
]
