"""Bounded read-only projections of the existing durable event log."""

from __future__ import annotations

import dataclasses
import json

from omicsclaw.entry.events import TurnEventType
from omicsclaw.entry.stream import event_from_durable
from omicsclaw.memory.turn_events import DurableTurnEventLog

from .interactions import TurnUsage
from .turn_observation import DesktopChatSSEBody, _count_usage


class _Rows:
    def __init__(self, rows):
        self.events = iter(
            event for row in rows if (event := event_from_durable(row)) is not None
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        pass


async def replay_frames(
    app,
    session_id,
    turn_id,
    *,
    after_seq=0,
    after_subframe=-1,
    epoch="",
    limit=64,
    interactions=None,
):
    if (
        type(after_seq) is not int
        or after_seq < 0
        or type(after_subframe) is not int
        or not -1 <= after_subframe <= 16
    ):
        raise ValueError("invalid event cursor")
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("invalid projection limit")
    log = DurableTurnEventLog(app.memory.database)
    stream_id = json.dumps([session_id, turn_id], separators=(",", ":"))
    oldest, latest = log.bounds(stream_id)
    if after_seq > latest:
        raise ValueError("event cursor ahead of authority")
    gap = (
        oldest is not None
        and (after_seq if after_subframe >= 0 else after_seq + 1) < oldest
    )
    # Include the current event because a terminal emits multiple subframes.
    before = max(0, after_seq - 1)
    rows = log.replay(stream_id, after_seq=before, limit=128)
    ledger = TurnUsage()
    if before:
        ledger.cover(1, before)
        for row in log.replay(
            stream_id, limit=256, event_type=TurnEventType.TURN_END.value
        ):
            if row.seq <= before:
                _count_usage(ledger, event_from_durable(row))
    terminal = any(row.type == TurnEventType.EXCHANGE_END.value for row in rows)
    frames, size, truncated = [], 0, False
    receipt = log.turn_receipt(session_id, turn_id)
    metadata = (receipt or {}).get("payload", {}).get("values", {}).get(
        "__desktop_model_snapshot__"
    ) or {}
    body = DesktopChatSSEBody(
        _Rows(rows),
        keepalive_s=None,
        usage_ledger=ledger,
        interactions=interactions,
        provider=metadata.get("provider", app.config.provider),
        model=metadata.get("model", app.resolved_model or app.config.model),
    )
    async with body:
        async for frame in body:
            if (
                not terminal
                and json.loads(frame.split("data: ", 1)[1]).get("type") == "done"
            ):
                continue  # EOF of a read page is not a business terminal.
            cursor = (body.last_seq, body.last_subframe)
            if cursor <= (after_seq, after_subframe):
                continue
            item = {
                "durableIdentity": dataclasses.asdict(log.identity),
                "epoch": epoch,
                "eventStreamId": stream_id,
                "sessionId": session_id,
                "turnId": turn_id,
                "cursor": body.last_seq,
                "subframe": body.last_subframe,
                "frame": frame,
            }
            encoded_size = len(json.dumps(item).encode())
            if encoded_size > 524288:
                raise ValueError("event_frame_requires_data_channel")
            if len(frames) >= limit or size + encoded_size > 524288:
                truncated = True
                break
            frames.append(item)
            size += encoded_size
    next_cursor = (
        {"seq": frames[-1]["cursor"], "subframe": frames[-1]["subframe"]}
        if frames
        else {"seq": rows[-1].seq if rows else after_seq, "subframe": -1}
    )
    return {
        "frames": frames,
        "nextCursor": next_cursor,
        "gap": gap,
        "oldest": oldest,
        "latest": latest,
        "hasMore": truncated or bool(rows and rows[-1].seq < latest),
        "durableIdentity": dataclasses.asdict(log.identity),
    }
