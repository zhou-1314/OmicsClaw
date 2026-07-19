from __future__ import annotations

import asyncio
import json

import pytest

from omicsclaw.surfaces.desktop._skill_log_bridge import SkillLogCoalescer


@pytest.mark.asyncio
async def test_detached_skill_log_observer_drops_buffered_and_future_lines():
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    coalescer = SkillLogCoalescer(
        loop=asyncio.get_running_loop(),
        queue=queue,
    )
    run_id = coalescer.begin_skill("demo")
    coalescer.emit(run_id, "stdout", "before disconnect")

    coalescer.detach()
    coalescer.emit(run_id, "stdout", "after disconnect")
    await coalescer.flush_now()
    await coalescer.aclose()

    assert queue.empty()


@pytest.mark.asyncio
async def test_single_skill_log_line_is_bounded_before_buffering():
    from omicsclaw.surfaces.desktop._skill_log_bridge import (
        SKILL_LOG_MAX_LINE_BYTES,
    )

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    coalescer = SkillLogCoalescer(
        loop=asyncio.get_running_loop(),
        queue=queue,
    )
    run_id = coalescer.begin_skill("demo")
    original = "界" * SKILL_LOG_MAX_LINE_BYTES

    coalescer.emit(run_id, "stderr", original)
    await coalescer.flush_now()
    event = queue.get_nowait()
    assert event is not None
    payload = json.loads(event["data"])
    line = payload["lines"][0]
    assert len(line["text"].encode("utf-8")) <= SKILL_LOG_MAX_LINE_BYTES
    assert line["text_truncated"] is True
    assert line["text_size_bytes"] == len(original.encode("utf-8"))


@pytest.mark.asyncio
async def test_skill_log_batches_are_individually_bounded_and_report_drops():
    from omicsclaw.surfaces.desktop._chat_sse import (
        CHAT_SSE_MAX_FRAME_BYTES,
        render_chat_sse_frame,
    )
    from omicsclaw.surfaces.desktop._skill_log_bridge import (
        SKILL_LOG_MAX_BATCH_DATA_BYTES,
        SKILL_LOG_MAX_BUFFER_BYTES,
        SKILL_LOG_MAX_LINE_BYTES,
    )

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    coalescer = SkillLogCoalescer(
        loop=asyncio.get_running_loop(),
        queue=queue,
    )
    run_id = coalescer.begin_skill("demo")
    line = "x" * SKILL_LOG_MAX_LINE_BYTES
    count = SKILL_LOG_MAX_BUFFER_BYTES // SKILL_LOG_MAX_LINE_BYTES + 4
    for _ in range(count):
        coalescer.emit(run_id, "stdout", line)

    await coalescer.flush_now()
    events: list[dict] = []
    while not queue.empty():
        event = queue.get_nowait()
        assert event is not None
        events.append(event)

    assert len(events) > 1
    payloads = [json.loads(event["data"]) for event in events]
    assert any(payload.get("lines_omitted", 0) > 0 for payload in payloads)
    for event in events:
        assert len(event["data"].encode("utf-8")) <= SKILL_LOG_MAX_BATCH_DATA_BYTES
        frame = render_chat_sse_frame(event["type"], event["data"])
        assert len(frame.encode("utf-8")) <= CHAT_SSE_MAX_FRAME_BYTES
