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
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from omicsclaw.surfaces.desktop._chat_sse import utf8_size

# How often the background task drains buffered lines into the SSE queue.
_FLUSH_INTERVAL_SECONDS = 0.15
# Upper bound on buffered lines if the SSE consumer stalls — drop oldest beyond
# this so a runaway skill can't grow memory without limit.
_MAX_BUFFERED_LINES = 5000
SKILL_LOG_MAX_LINE_BYTES = 64 * 1024
SKILL_LOG_MAX_BUFFER_BYTES = 2 * 1024 * 1024
SKILL_LOG_MAX_BATCH_DATA_BYTES = 512 * 1024
_SKILL_NAME_MAX_BYTES = 1024
_UTF8_CHUNK_CHARS = 16 * 1024


def _bounded_utf8(value: str, limit: int) -> tuple[str, int, bool, int]:
    """Return bounded text, exact original size, truncation, retained size."""

    total = 0
    retained = bytearray()
    for offset in range(0, len(value), _UTF8_CHUNK_CHARS):
        encoded = value[offset : offset + _UTF8_CHUNK_CHARS].encode(
            "utf-8", errors="replace"
        )
        total += len(encoded)
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(encoded[:remaining])
    text = bytes(retained).decode("utf-8", errors="ignore")
    retained_size = utf8_size(text)
    return text, total, total > retained_size, retained_size


class SkillLogCoalescer:
    """Per-request buffer that turns thread-emitted skill log lines into
    coalesced ``tool_log`` SSE frames on ``queue``."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict | None] | None" = None,
        event_sink: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> None:
        if (queue is None) == (event_sink is None):
            raise ValueError("exactly one of queue or event_sink is required")
        self._loop = loop
        self._queue = queue
        self._event_sink = event_sink
        # (run_id, stream, text, original_size, truncated, retained_size)
        self._buf: deque[tuple[str, str, str, int, bool, int]] = deque()
        self._buffered_bytes = 0
        self._dropped_by_run: dict[str, int] = {}
        self._skill_by_run: dict[str, str] = {}
        self._run_seq = 0
        self._lock = threading.Lock()
        self._closed = False
        self._close_requested = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- producer side -------------------------------------------------------

    def begin_skill(self, skill: str) -> str:
        """Register a new skill run; returns its opaque run id. Called from the
        coroutine that runs the skill (before the reader threads start)."""
        bounded_skill, _, _, _ = _bounded_utf8(str(skill), _SKILL_NAME_MAX_BYTES)
        with self._lock:
            self._run_seq += 1
            run_id = str(self._run_seq)
            self._skill_by_run[run_id] = bounded_skill
        return run_id

    def emit(self, run_id: str, stream: str, line: str) -> None:
        """Buffer one captured line. Thread-safe; never raises."""
        if self._closed:
            return
        text, original_size, truncated, retained_size = _bounded_utf8(
            str(line), SKILL_LOG_MAX_LINE_BYTES
        )
        bounded_stream, _, _, _ = _bounded_utf8(str(stream), 32)
        with self._lock:
            # Check under the lock so an append can't race past aclose()'s final
            # flush: either the line lands before _closed is set (and the final
            # flush drains it) or the reader sees _closed and drops it.
            if self._closed:
                return
            self._buf.append(
                (
                    run_id,
                    bounded_stream,
                    text,
                    original_size,
                    truncated,
                    retained_size,
                )
            )
            self._buffered_bytes += retained_size
            while (
                len(self._buf) > _MAX_BUFFERED_LINES
                or self._buffered_bytes > SKILL_LOG_MAX_BUFFER_BYTES
            ):
                dropped = self._buf.popleft()
                self._buffered_bytes -= dropped[5]
                dropped_run = dropped[0]
                self._dropped_by_run[dropped_run] = (
                    self._dropped_by_run.get(dropped_run, 0) + 1
                )

    # -- consumer side (runs on the asyncio loop) ---------------------------

    def start(self) -> None:
        self._task = self._loop.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._closed:
                try:
                    await asyncio.wait_for(
                        self._close_requested.wait(),
                        timeout=_FLUSH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        with self._lock:
            if not self._buf:
                return
            items = self._buf
            self._buf = deque()
            self._buffered_bytes = 0
            dropped_by_run = self._dropped_by_run
            self._dropped_by_run = {}
            skill_by_run = dict(self._skill_by_run)
        # Group into runs, preserving first-seen order. One skill runs at a time
        # (analysis tools are serial barriers), so a batch is usually one run.
        order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for run_id, stream, text, original_size, truncated, _ in items:
            if run_id not in grouped:
                grouped[run_id] = []
                order.append(run_id)
            line_payload: dict[str, Any] = {"stream": stream, "text": text}
            if truncated:
                line_payload.update(
                    {
                        "text_truncated": True,
                        "text_size_bytes": original_size,
                    }
                )
            grouped[run_id].append(line_payload)
        for run_id in dropped_by_run:
            if run_id not in grouped:
                grouped[run_id] = []
                order.append(run_id)
        for run_id in order:
            metadata: dict[str, Any] = {
                "run_id": run_id,
                "skill": skill_by_run.get(run_id, ""),
            }
            omitted = dropped_by_run.get(run_id, 0)
            entries = grouped[run_id]
            batches: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            current_size = utf8_size(
                json.dumps({**metadata, "lines": []}, ensure_ascii=False)
            )
            for entry in entries:
                entry_size = (
                    utf8_size(json.dumps(entry, ensure_ascii=False, default=str)) + 1
                )
                if (
                    current
                    and current_size + entry_size > SKILL_LOG_MAX_BATCH_DATA_BYTES
                ):
                    batches.append(current)
                    current = []
                    current_size = utf8_size(
                        json.dumps({**metadata, "lines": []}, ensure_ascii=False)
                    )
                current.append(entry)
                current_size += entry_size
            if current or not batches:
                batches.append(current)

            for index, batch in enumerate(batches):
                payload: dict[str, Any] = {**metadata, "lines": batch}
                if index == 0 and omitted:
                    payload["lines_omitted"] = omitted
                data = json.dumps(payload, ensure_ascii=False, default=str)
                if utf8_size(data) > SKILL_LOG_MAX_BATCH_DATA_BYTES:
                    # A single line is capped at 64 KiB, so this indicates an
                    # internal accounting error rather than user data pressure.
                    raise RuntimeError("skill log batch exceeded its wire budget")
                if self._event_sink is not None:
                    await self._event_sink("tool_log", data)
                else:
                    assert self._queue is not None
                    await self._queue.put({"type": "tool_log", "data": data})

    async def flush_now(self) -> None:
        """Drain pending lines immediately — call before a tool's result/done
        frame so its trailing logs are not stranded in the buffer."""
        await self._flush()

    def detach(self) -> None:
        """Drop buffered/future renderer-only lines after an SSE disconnect.

        Execution may continue through the Control Runtime after its original
        Desktop observer disappears.  This synchronous seam keeps the
        compatibility renderer queue from accumulating batches that nobody can
        consume; it deliberately does not cancel the skill or its Turn.
        """

        with self._lock:
            self._closed = True
            self._buf = deque()
            self._buffered_bytes = 0
            self._dropped_by_run = {}
        self._close_requested.set()

    async def aclose(self) -> None:
        """Stop the background task and drain whatever remains. After this,
        ``emit`` becomes a no-op so straggling reader-thread callbacks during
        teardown are dropped (no queue-after-close)."""
        # Set under the lock so it's atomic with emit()'s closed-check: every
        # append either precedes this (and is drained by the final flush below)
        # or is dropped.
        with self._lock:
            self._closed = True
        self._close_requested.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                # External loop teardown may still cancel the coalescer task;
                # the final flush below drains anything it had not claimed.
                pass
            self._task = None
        await self._flush()


__all__ = [
    "SKILL_LOG_MAX_BATCH_DATA_BYTES",
    "SKILL_LOG_MAX_BUFFER_BYTES",
    "SKILL_LOG_MAX_LINE_BYTES",
    "SkillLogCoalescer",
]
