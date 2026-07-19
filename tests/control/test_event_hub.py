from __future__ import annotations

import asyncio

import pytest

from omicsclaw.control.event_hub import (
    EventHubCapacityError,
    EventHubLoopAffinityError,
    EventHistoryGap,
    EventObserverDetached,
    TurnEventHub,
)
from omicsclaw.runtime.agent.events import Final, StreamContent


@pytest.mark.asyncio
async def test_event_hub_replays_then_streams_through_terminal_event():
    hub = TurnEventHub(max_events_per_turn=8, max_turns=4)
    hub.open_turn("turn-1")
    hub.publish("turn-1", StreamContent("a"))

    observed = []

    async def observe():
        async for frame in hub.subscribe("turn-1"):
            observed.append(frame)

    task = asyncio.create_task(observe())
    await asyncio.sleep(0)
    hub.publish("turn-1", StreamContent("b"))
    hub.publish("turn-1", Final("ab"), terminal=True)
    await task

    assert [frame.sequence for frame in observed] == [1, 2, 3]
    assert all(frame.schema_version == 1 for frame in observed)
    assert all(frame.emitted_at_ms > 0 for frame in observed)
    assert [frame.event for frame in observed] == [
        StreamContent("a"),
        StreamContent("b"),
        Final("ab"),
    ]
    assert observed[-1].terminal is True


@pytest.mark.asyncio
async def test_event_hub_reports_reconnect_gap_after_bounded_eviction():
    hub = TurnEventHub(max_events_per_turn=2, max_turns=4)
    hub.open_turn("turn-1")
    hub.publish("turn-1", StreamContent("a"))
    hub.publish("turn-1", StreamContent("b"))
    hub.publish("turn-1", Final("done"), terminal=True)

    with pytest.raises(EventHistoryGap, match="oldest available sequence is 2"):
        async for _frame in hub.subscribe("turn-1", after_sequence=-1):
            pass


@pytest.mark.asyncio
async def test_event_hub_rejects_future_cursor_for_live_stream():
    hub = TurnEventHub()
    hub.open_turn("turn-1")
    hub.publish("turn-1", StreamContent("zero"))

    iterator = hub.subscribe("turn-1", after_sequence=10)
    with pytest.raises(EventHistoryGap, match="ahead of latest sequence 1"):
        await anext(iterator)


@pytest.mark.asyncio
async def test_event_hub_rejects_future_cursor_for_terminal_stream():
    hub = TurnEventHub()
    hub.open_turn("turn-1")
    hub.publish("turn-1", Final("done"), terminal=True)

    iterator = hub.subscribe("turn-1", after_sequence=10)
    with pytest.raises(EventHistoryGap, match="ahead of latest sequence 1"):
        await anext(iterator)


@pytest.mark.asyncio
async def test_event_hub_opens_gap_recovery_and_live_subscription_atomically():
    hub = TurnEventHub(max_events_per_turn=2, max_turns=4)
    hub.open_turn("turn-1")
    hub.publish("turn-1", StreamContent("one"))
    hub.publish("turn-1", StreamContent("two"))
    hub.publish("turn-1", StreamContent("three"))

    observation = hub.open_observation("turn-1", after_sequence=0)
    assert observation.gap is not None
    assert observation.gap.reason == "cursor_evicted"
    assert observation.gap.requested_after_sequence == 0
    assert observation.gap.oldest_available_sequence == 2
    assert observation.gap.latest_sequence == 3

    next_frame = asyncio.create_task(anext(observation))
    await asyncio.sleep(0)
    hub.publish("turn-1", StreamContent("four"))
    assert (await next_frame).sequence == 4
    hub.publish("turn-1", Final("done"), terminal=True)
    assert (await anext(observation)).sequence == 5
    with pytest.raises(StopAsyncIteration):
        await anext(observation)


@pytest.mark.asyncio
async def test_event_hub_terminal_cursor_opens_an_empty_but_bounded_observation():
    hub = TurnEventHub()
    hub.open_turn("turn-1")
    terminal = hub.publish("turn-1", Final("done"), terminal=True)

    observation = hub.open_observation(
        "turn-1",
        after_sequence=terminal.sequence,
    )

    assert observation.gap is None
    assert observation.oldest_available_sequence == terminal.sequence
    assert observation.latest_sequence == terminal.sequence
    with pytest.raises(StopAsyncIteration):
        await anext(observation)


@pytest.mark.asyncio
async def test_slow_observer_is_detached_without_blocking_publication():
    hub = TurnEventHub(
        max_events_per_turn=8,
        max_turns=4,
        subscriber_queue_size=1,
    )
    hub.open_turn("turn-1")
    iterator = hub.subscribe("turn-1")
    first = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)

    hub.publish("turn-1", StreamContent("a"))
    assert (await first).event == StreamContent("a")
    hub.publish("turn-1", StreamContent("b"))
    hub.publish("turn-1", StreamContent("c"))
    hub.publish("turn-1", Final("done"), terminal=True)

    with pytest.raises(EventObserverDetached):
        await anext(iterator)


@pytest.mark.asyncio
async def test_event_hub_bounds_live_observers_per_turn():
    hub = TurnEventHub(max_subscribers_per_turn=1)
    hub.open_turn("turn-1")
    first = hub.open_observation("turn-1")

    with pytest.raises(EventHubCapacityError, match="observer capacity"):
        hub.open_observation("turn-1")

    await first.aclose()
    replacement = hub.open_observation("turn-1")
    await replacement.aclose()


@pytest.mark.asyncio
async def test_observation_close_wakes_pending_next_without_detached_error():
    hub = TurnEventHub()
    hub.open_turn("turn-1")
    observation = hub.open_observation("turn-1")
    pending = asyncio.create_task(anext(observation))
    await asyncio.sleep(0)

    await observation.aclose()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=0.2)


@pytest.mark.asyncio
async def test_cross_loop_publish_fails_before_mutating_retained_history():
    hub = TurnEventHub()
    hub.open_turn("turn-1")
    observation = hub.open_observation("turn-1")

    with pytest.raises(EventHubLoopAffinityError, match="producer event loop"):
        await asyncio.to_thread(hub.publish, "turn-1", StreamContent("wrong-loop"))

    assert hub.retained_frames("turn-1") == ()
    published = hub.publish("turn-1", StreamContent("same-loop"))
    assert await anext(observation) == published
    await observation.aclose()


@pytest.mark.asyncio
async def test_observation_cannot_bind_its_queue_from_another_event_loop():
    hub = TurnEventHub()
    hub.open_turn("turn-1")
    observation = hub.open_observation("turn-1")

    def consume_from_new_loop():
        return asyncio.run(anext(observation))

    with pytest.raises(EventHubLoopAffinityError, match="opening event loop"):
        await asyncio.to_thread(consume_from_new_loop)

    assert hub.retained_frames("turn-1") == ()
    await observation.aclose()


@pytest.mark.asyncio
async def test_first_observer_cannot_take_loop_authority_from_turn_producer():
    hub = TurnEventHub()
    hub.open_turn("turn-1")

    async def open_from_new_loop():
        return hub.open_observation("turn-1")

    def run_new_loop():
        return asyncio.run(open_from_new_loop())

    with pytest.raises(EventHubLoopAffinityError, match="producer event loop"):
        await asyncio.to_thread(run_new_loop)

    published = hub.publish("turn-1", StreamContent("producer-still-authoritative"))
    assert hub.retained_frames("turn-1") == (published,)


@pytest.mark.asyncio
async def test_future_cursor_never_consumes_observer_capacity():
    hub = TurnEventHub(max_subscribers_per_turn=1)
    hub.open_turn("turn-1")
    existing = hub.open_observation("turn-1")

    future = hub.open_observation("turn-1", after_sequence=9)

    assert future.gap is not None
    assert future.gap.reason == "cursor_ahead"
    assert len(hub._streams["turn-1"].subscribers) == 1
    with pytest.raises(StopAsyncIteration):
        await anext(future)
    await existing.aclose()
