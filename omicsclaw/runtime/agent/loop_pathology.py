"""Pathology detection over ``LoopState``.

Per ADR 0007. ``detect(state)`` is a pure function over the current
``LoopState``. It inspects the bounded tool-call and error histories
and returns at most one ``PathologySignal`` per call — the most recent
unhealthy pattern that crossed a threshold.

Three patterns are recognised today:

- **Pingpong** — the same ``(tool_name, args_digest)`` pair appears at
  least ``PINGPONG_THRESHOLD`` times within the trailing
  ``PINGPONG_WINDOW`` entries of ``state.tool_calls``. The args-digest
  granularity is intentional: ``grep(pattern="A")`` followed by
  ``grep(pattern="B")`` is *not* a ping-pong even though the tool name
  repeats.

- **Repeated read** — the same *file* (``ToolCallRecord.target``) is
  read at least ``REPEATED_READ_THRESHOLD`` times within the trailing
  ``REPEATED_READ_WINDOW`` entries, *regardless of which read tool or
  arguments were used*. This catches the pattern pingpong cannot: a
  report opened via ``file_read``, then ``grep_files``, then a
  line-range ``file_read`` — three different ``(name, args_digest)``
  keys, one file already in context. (Real trace: a QC run re-read its
  ``completion_report.json`` five times across a verification storm.)

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

import os
from collections import Counter
from typing import Any

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

# Reading the *same* file 3 times is already wasteful — its contents are in
# context after the first read. The window is wider than pingpong's because
# storm reads are interleaved with other inspection calls (glob, list_directory).
REPEATED_READ_WINDOW = 8
REPEATED_READ_THRESHOLD = 3

# Read-like tools and the argument(s) that name the single file they read,
# in priority order. ``grep_files`` and ``read_knowhow`` are handled specially
# in ``read_access_target`` (root+glob / knowhow name).
_READ_TARGET_ARGS: dict[str, tuple[str, ...]] = {
    "file_read": ("path", "file_path"),
    "inspect_file": ("file_path", "path"),
    "inspect_data": ("file_path", "path"),
}


def _normalize_fs_target(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normpath(os.path.expanduser(text))


def read_access_target(name: str, arguments: Any) -> str:
    """Normalized identifier of the single file/resource a read-like tool reads.

    Returns ``""`` for tools that are not single-resource reads (so they never
    count toward the repeated-read pattern). A ``grep_files`` call only pins a
    file when its ``glob`` is a concrete filename — a wildcard glob (``*.py``)
    is a broad search that legitimately repeats with new patterns.

    This is computed at record time (where the raw arguments are available) and
    stored on ``ToolCallRecord.target`` so the detector can match the same file
    across ``file_read`` / ``grep_files`` / ``inspect_*`` calls.
    """
    if not isinstance(arguments, dict):
        return ""
    if name == "read_knowhow":
        kn = str(arguments.get("name", "") or "").strip()
        return f"knowhow:{kn}" if kn else ""
    if name == "grep_files":
        glob = str(arguments.get("glob", "") or "").strip()
        if not glob or any(ch in glob for ch in "*?[]"):
            return ""
        root = str(arguments.get("root", "") or "").strip()
        return _normalize_fs_target(os.path.join(root, glob) if root else glob)
    for arg in _READ_TARGET_ARGS.get(name, ()):  # file_read / inspect_*
        value = arguments.get(arg)
        if value:
            return _normalize_fs_target(str(value))
    return ""


# ── Phantom completion (ADR 0027) ────────────────────────────────────
#
# The tools that *actually run an analysis* (as opposed to inspecting /
# searching / planning). If none of these has run in the loop, a final
# message that claims analysis work was done is a phantom completion.
EXECUTION_TOOLS: frozenset[str] = frozenset(
    {
        "omicsclaw",
        "autonomous_analysis_execute",
        "replot_skill",
    }
)

# Best-effort intent markers (EN + ZH) that signal the model is *claiming
# or announcing* analysis work — "I will run…", "已生成…". Curated, not
# exhaustive: a miss degrades to current behaviour (the loop returns the
# message as-is), which is harmless. Markers are deliberately specific to
# running/executing/producing analysis so a generic "I will help you" does
# not trip them. Lower-cased substring match for EN; raw substring for ZH.
_PHANTOM_INTENT_MARKERS: tuple[str, ...] = (
    # English — first-person commitment to act (paired with an action verb so a
    # generic "I will help you" does not trip) or a claim of completion.
    "i will run",
    "i will start",
    "i will begin",
    "i will execute",
    "i will perform",
    "i will proceed",
    "i will apply",
    "i will use the",
    "i will generate",
    "i will analyze",
    "i will analyse",
    "i will preprocess",
    "i will now",
    "i will go ahead",
    "i'll run",
    "i'll start",
    "i'll begin",
    "i'll execute",
    "i'll perform",
    "i'll proceed",
    "i'll apply",
    "i'll generate",
    "i'll go ahead",
    "let me run",
    "let me start",
    "let me begin",
    "let me execute",
    "let me proceed",
    "let me apply",
    "i'm going to",
    "i am going to",
    "going to run",
    "going to start",
    "going to proceed",
    "i have run",
    "i've run",
    "i ran the",
    "i have performed",
    "i have executed",
    "i've executed",
    "i have started",
    "i've started",
    "i have generated",
    "i've generated",
    "i have completed",
    "i have analyzed",
    "i have analysed",
    "i have processed",
    "proceeding with",
    "running the analysis",
    "running the preprocessing",
    "preprocessing pipeline",
    "here are the results",
    "here are the initial results",
    "execution report",
    "analysis report",
    "qc report",
    "my plan",
    "first step",
    # Chinese — first-person commitment ("我将/我会/我已/正在/我来/让我", which a
    # capability-describing intro ("我可以帮你…") does not use) or a completion
    # claim. Bare because the verb that follows varies (采用/按照/直接开始/执行…).
    "我将",
    "我会",
    "我已",
    "正在",
    "我来",
    "让我",
    "准备对",
    "计划执行",
    "按照以下",
    "开始第一步",
    "第一步",
    "已为您",
    "执行情况",
    "执行过程",
    "初步结果",
    "分析报告",
    "已生成",
    "已完成分析",
    "已完成预处理",
    "已启动",
    "已运行",
    "已执行",
)


def _announces_analysis_work(content: str) -> bool:
    """True if ``content`` claims/announces analysis execution or results."""
    text = content.strip()
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _PHANTOM_INTENT_MARKERS)


def _execution_tool_ran(state: LoopState) -> bool:
    return any(record.name in EXECUTION_TOOLS for record in state.tool_calls)


def detect_phantom_completion(
    *,
    content: str,
    state: LoopState,
    enabled: bool,
) -> PathologySignal | None:
    """Detect a *phantom completion* at the no-tool-call termination branch.

    Per ADR 0027. Fires only when ALL hold:

    - ``enabled`` — gated to providers whose models silently truncate and
      miss tool calls (Ollama today); the caller passes the config flag.
    - The terminating message ``content`` *claims or announces* analysis
      work (``_announces_analysis_work``).
    - No execution tool (``EXECUTION_TOOLS``) has run in this loop — so a
      genuine post-run summary, or a model still mid-execution, is not
      flagged.

    Unlike :func:`detect`, this is evaluated against the *current* message
    rather than the post-execution history, because the symptom is the
    absence of a tool call, not a pattern over prior calls. It returns no
    ``tool_name`` (none was called) and a ``count`` of 1.
    """
    if not enabled:
        return None
    if _execution_tool_ran(state):
        return None
    if not _announces_analysis_work(content):
        return None
    return PathologySignal(
        kind="phantom_completion",
        tool_name=None,
        iteration=state.iteration,
        count=1,
        reason=(
            "model described or claimed analysis work but called no tool, "
            "so nothing actually ran"
        ),
    )


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
    repeated_read = _detect_repeated_file_access(state)
    if repeated_read is not None:
        return repeated_read
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


def _detect_repeated_file_access(state: LoopState) -> PathologySignal | None:
    if not state.tool_calls:
        return None
    window: list[ToolCallRecord] = list(state.tool_calls)[-REPEATED_READ_WINDOW:]
    targets = [record.target for record in window if record.target]
    if len(targets) < REPEATED_READ_THRESHOLD:
        return None
    target, count = Counter(targets).most_common(1)[0]
    if count < REPEATED_READ_THRESHOLD:
        return None
    # Report the tool from the most recent access to this target.
    last_name = next(
        (record.name for record in reversed(window) if record.target == target),
        None,
    )
    return PathologySignal(
        kind="repeated_read",
        tool_name=last_name,
        iteration=window[-1].iteration,
        count=count,
        reason=(
            f"the same resource {target!r} was read {count} times in the last "
            f"{len(window)} tool calls (via file_read / grep_files / inspect_*); "
            "it is already in context"
        ),
        target=target,
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
    "REPEATED_READ_WINDOW",
    "REPEATED_READ_THRESHOLD",
    "EXECUTION_TOOLS",
    "detect",
    "detect_phantom_completion",
    "read_access_target",
]
