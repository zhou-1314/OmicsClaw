import asyncio

import pytest

from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.stream import TurnStream
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import (
    DurableTurnEvent,
    DurableTurnEventLog,
    EventLogConflict,
)


def test_append_replay_is_idempotent_and_conflicts_are_rejected():
    log = DurableTurnEventLog(Database(":memory:"))
    event = DurableTurnEvent("s:t", "s", "t", 1, "exchange_start", {"x": "data"}, 0)
    assert log.append(event).seq == 1
    assert log.append(event).payload == {"x": "data"}
    with pytest.raises(EventLogConflict):
        log.append(DurableTurnEvent("s:t", "s", "t", 1, "exchange_end", {}, 0))
    assert [item.seq for item in log.replay("s:t", after_seq=0)] == [1]


def test_retention_bounds_report_a_real_gap():
    log = DurableTurnEventLog(Database(":memory:"), retained=2)
    for seq in range(1, 4):
        log.append(
            DurableTurnEvent("s:t", "s", "t", seq, "queued", {"sequence": seq}, 0)
        )
    assert log.bounds("s:t") == (2, 3)
    assert [item.seq for item in log.replay("s:t", after_seq=0)] == [2, 3]


def test_stream_uses_durable_replay_when_ring_evicted():
    log = DurableTurnEventLog(Database(":memory:"))
    stream = TurnStream("s", "t", ring_size=1, durable_log=log)
    stream.publish(TurnEvent.exchange_start(session_id="s", turn_id="t"))
    stream.publish(TurnEvent.queued(0, session_id="s", turn_id="t"))
    observation = stream.observe(after_seq=0)

    async def read():
        return [
            (await observation.__anext__()).seq,
            (await observation.__anext__()).seq,
        ]

    assert asyncio.run(read()) == [1, 2]


def test_durable_payload_is_inert_data():
    payload = {"type": "text_delta", "delta": "untrusted output"}
    log = DurableTurnEventLog(Database(":memory:"))
    log.append(DurableTurnEvent("s:t", "s", "t", 1, "text_delta", payload, 0))
    assert log.replay("s:t")[0].payload == payload


def test_async_append_has_ring_fallback_before_flush():
    async def run():
        log = DurableTurnEventLog(Database(":memory:"))
        stream = TurnStream("s", "t", durable_log=log, ring_size=4)
        stream.publish(TurnEvent.exchange_start(session_id="s", turn_id="t"))
        stream.publish(TurnEvent.queued(0, session_id="s", turn_id="t"))
        observation = stream.observe(after_seq=0)
        assert (await observation.__anext__()).seq == 1
        assert (await observation.__anext__()).seq == 2
        await stream.flush_durable()
        assert log.bounds('["s","t"]')[1] == 2

    asyncio.run(run())


def test_ring_gap_is_explicit_before_async_durable_flush():
    async def run():
        log = DurableTurnEventLog(Database(":memory:"), retained=2)
        stream = TurnStream("s", "gap", ring_size=1, durable_log=log)
        stream.publish(TurnEvent.exchange_start(session_id="s", turn_id="gap"))
        stream.publish(TurnEvent.queued(0, session_id="s", turn_id="gap"))
        observation = stream.observe(after_seq=0)
        first = await observation.__anext__()
        second = await observation.__anext__()
        assert first.type.value == "gap"
        assert first.gap is not None
        assert first.gap == (2, 2)

        assert second.seq == 2
        assert await stream.flush_durable() is True

    asyncio.run(run())


def test_durable_append_failure_is_observable_after_flush():
    class BrokenLog:
        def acquire_owner(self, stream_id):
            return 1

        def bounds(self, stream_id):
            return (None, 0)

        def append(self, event):
            raise RuntimeError("sink unavailable")

        def replay(self, stream_id, after_seq=0):
            return []

    async def run():
        stream = TurnStream("s", "broken", durable_log=BrokenLog())
        stream.publish(TurnEvent.exchange_start(session_id="s", turn_id="broken"))
        assert await stream.flush_durable() is False
        assert isinstance(stream.durable_error, RuntimeError)

    asyncio.run(run())
