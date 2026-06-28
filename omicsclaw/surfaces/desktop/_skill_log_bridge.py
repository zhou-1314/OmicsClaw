"""Coalesce live skill log lines into ``tool_log`` SSE frames.

Skill subprocess stdout/stderr lines are emitted from raw reader threads (see
:mod:`omicsclaw.skill.execution.subprocess_driver`). A verbose skill (e.g. a
resolution sweep) can emit hundreds-to-thousands of lines, so this bridge
buffers them thread-safely and flushes them as batched ``tool_log`` events on a
short interval rather than one SSE frame per line.

Lines are correlated by a per-run id assigned in :meth:`begin_skill` (called from
the coroutine when a skill starts), not by tool-call id: at skill-execution time
the surface has no reliable signal of which tool call is running.

Wire shape (one frame, one run id):
    {"type": "tool_log",
     "data": "{\\"run_id\\": \\"3\\", \\"skill\\": \\"sc-clustering\\",
               \\"lines\\": [{\\"stream\\": \\"stderr\\",
                              \\"text\\": \\"INFO: Testing resolution 0.40 ...\\"}, ...]}"}

``begin_skill``/``emit`` are called from skill code (``emit`` from reader
threads); the periodic drain and ``flush_now``/``aclose`` run on the asyncio loop
that owns ``queue``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Optional

# How often the background task drains buffered lines into the SSE queue.
_FLUSH_INTERVAL_SECONDS = 0.15
# Upper bound on buffered lines if the SSE consumer stalls — drop oldest beyond
# this so a runaway skill can't grow memory without limit.
_MAX_BUFFERED_LINES = 5000


class SkillLogCoalescer:
    """Per-request buffer that turns thread-emitted skill log lines into
    coalesced ``tool_log`` SSE frames on ``queue``."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict | None]",
    ) -> None:
        self._loop = loop
        self._queue = queue
        self._buf: list[tuple[str, str, str]] = []  # (run_id, stream, text)
        self._skill_by_run: dict[str, str] = {}
        self._run_seq = 0
        self._lock = threading.Lock()
        self._closed = False
        self._task: Optional[asyncio.Task] = None

    # -- producer side -------------------------------------------------------

    def begin_skill(self, skill: str) -> str:
        """Register a new skill run; returns its opaque run id. Called from the
        coroutine that runs the skill (before the reader threads start)."""
        with self._lock:
            self._run_seq += 1
            run_id = str(self._run_seq)
            self._skill_by_run[run_id] = skill
        return run_id

    def emit(self, run_id: str, stream: str, line: str) -> None:
        """Buffer one captured line. Thread-safe; never raises."""
        with self._lock:
            # Check under the lock so an append can't race past aclose()'s final
            # flush: either the line lands before _closed is set (and the final
            # flush drains it) or the reader sees _closed and drops it.
            if self._closed:
                return
            overflow = len(self._buf) - _MAX_BUFFERED_LINES + 1
            if overflow > 0:
                del self._buf[:overflow]
            self._buf.append((run_id, stream, line))

    # -- consumer side (runs on the asyncio loop) ---------------------------

    def start(self) -> None:
        self._task = self._loop.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        with self._lock:
            if not self._buf:
                return
            items = self._buf
            self._buf = []
            skill_by_run = dict(self._skill_by_run)
        # Group into runs, preserving first-seen order. One skill runs at a time
        # (analysis tools are serial barriers), so a batch is usually one run.
        order: list[str] = []
        grouped: dict[str, list[dict[str, str]]] = {}
        for run_id, stream, text in items:
            if run_id not in grouped:
                grouped[run_id] = []
                order.append(run_id)
            grouped[run_id].append({"stream": stream, "text": text})
        for run_id in order:
            payload: dict[str, Any] = {
                "run_id": run_id,
                "skill": skill_by_run.get(run_id, ""),
                "lines": grouped[run_id],
            }
            await self._queue.put(
                {
                    "type": "tool_log",
                    "data": json.dumps(payload, ensure_ascii=False, default=str),
                }
            )

    async def flush_now(self) -> None:
        """Drain pending lines immediately — call before a tool's result/done
        frame so its trailing logs are not stranded in the buffer."""
        await self._flush()

    async def aclose(self) -> None:
        """Stop the background task and drain whatever remains. After this,
        ``emit`` becomes a no-op so straggling reader-thread callbacks during
        teardown are dropped (no queue-after-close)."""
        # Set under the lock so it's atomic with emit()'s closed-check: every
        # append either precedes this (and is drained by the final flush below)
        # or is dropped.
        with self._lock:
            self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush()


__all__ = ["SkillLogCoalescer"]
