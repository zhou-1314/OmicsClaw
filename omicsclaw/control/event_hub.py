"""Bounded process-local Event Hub for Turn observation.

Execution publishes once to this Module.  Surface renderers attach through
Adapters; observer failure, slowness, or disconnect never becomes execution
authority and never changes a durable Turn Receipt.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import asyncio
import threading
import time
from typing import AsyncIterator, Literal

from omicsclaw.runtime.agent.events import Event


class EventHubCapacityError(RuntimeError):
    pass


class EventHubLoopAffinityError(RuntimeError):
    pass


class EventHistoryGap(RuntimeError):
    pass


class EventObserverDetached(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnEventFrame:
    schema_version: int = field(init=False, default=1)
    turn_id: str
    sequence: int
    emitted_at_ms: int
    event: Event
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class TurnEventGap:
    """Structured cursor discontinuity detected at observation-open time."""

    reason: Literal["cursor_evicted", "cursor_ahead"]
    requested_after_sequence: int
    oldest_available_sequence: int | None
    latest_sequence: int | None


@dataclass(slots=True)
class _TurnStream:
    frames: deque[TurnEventFrame]
    producer_loop: asyncio.AbstractEventLoop
    next_sequence: int = 1
    terminal: bool = False
    subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = field(
        default_factory=dict
    )


_DETACHED = object()
_CLOSED = object()


class TurnEventObservation:
    """An eagerly opened, atomically registered replay/live observation.

    Callers inspect ``gap`` before iterating.  On a gap, the incomplete
    retained suffix is deliberately skipped and iteration begins at the first
    frame published after the atomic open point.  ``aclose`` is idempotent and
    detaches only this observer.
    """

    def __init__(
        self,
        *,
        hub: "TurnEventHub",
        turn_id: str,
        queue: asyncio.Queue,
        observer_loop: asyncio.AbstractEventLoop,
        replay: tuple[TurnEventFrame, ...],
        registered: bool,
        stream_terminal: bool,
        gap: TurnEventGap | None,
        oldest_available_sequence: int | None,
        latest_sequence: int | None,
    ) -> None:
        self._hub = hub
        self._turn_id = turn_id
        self._queue = queue
        self._observer_loop = observer_loop
        self._replay = deque(replay)
        self._registered = registered
        self._exhausted = stream_terminal and not replay
        self._closed = False
        self.gap = gap
        self.oldest_available_sequence = oldest_available_sequence
        self.latest_sequence = latest_sequence

    def __aiter__(self) -> "TurnEventObservation":
        return self

    async def __anext__(self) -> TurnEventFrame:
        if self._closed or self._exhausted:
            await self.aclose()
            raise StopAsyncIteration
        self._require_observer_loop()

        if self._replay:
            frame = self._replay.popleft()
        else:
            item = await self._queue.get()
            if item is _CLOSED:
                self._registered = False
                self._exhausted = True
                raise StopAsyncIteration
            if item is _DETACHED:
                await self.aclose()
                raise EventObserverDetached(
                    "Turn Event observer detached after subscriber backpressure"
                )
            assert isinstance(item, TurnEventFrame)
            frame = item

        if frame.terminal:
            self._exhausted = True
            self._hub._detach_observer(self._turn_id, self._queue)
            self._registered = False
        return frame

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        """Synchronously detach, including before async iteration starts."""

        if self._closed:
            return
        self._require_observer_loop()
        self._closed = True
        if self._registered:
            self._hub._close_observer(self._turn_id, self._queue)
            self._registered = False

    def _require_observer_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise EventHubLoopAffinityError(
                "Turn Event observation requires its opening event loop"
            ) from exc
        if running is not self._observer_loop:
            raise EventHubLoopAffinityError(
                "Turn Event observation crossed its opening event loop"
            )


class TurnEventHub:
    """One bounded stream per live/recent Turn with replay by sequence."""

    def __init__(
        self,
        *,
        max_events_per_turn: int = 256,
        max_turns: int = 256,
        subscriber_queue_size: int = 64,
        max_subscribers_per_turn: int = 16,
    ) -> None:
        for name, value in (
            ("max_events_per_turn", max_events_per_turn),
            ("max_turns", max_turns),
            ("subscriber_queue_size", subscriber_queue_size),
            ("max_subscribers_per_turn", max_subscribers_per_turn),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_events_per_turn = max_events_per_turn
        self.max_turns = max_turns
        self.subscriber_queue_size = subscriber_queue_size
        self.max_subscribers_per_turn = max_subscribers_per_turn
        self._lock = threading.RLock()
        self._streams: OrderedDict[str, _TurnStream] = OrderedDict()

    def open_turn(self, turn_id: str) -> None:
        turn = str(turn_id).strip()
        if not turn:
            raise ValueError("turn_id must be non-empty")
        try:
            producer_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise EventHubLoopAffinityError(
                "Turn Event producer requires a running event loop"
            ) from exc
        with self._lock:
            if turn in self._streams:
                self._require_producer_loop(self._streams[turn])
                self._streams.move_to_end(turn)
                return
            while len(self._streams) >= self.max_turns:
                evictable = next(
                    (
                        key
                        for key, stream in self._streams.items()
                        if stream.terminal and not stream.subscribers
                    ),
                    None,
                )
                if evictable is None:
                    raise EventHubCapacityError(
                        "Event Hub has no evictable Turn stream"
                    )
                self._streams.pop(evictable)
            self._streams[turn] = _TurnStream(
                frames=deque(maxlen=self.max_events_per_turn),
                producer_loop=producer_loop,
            )

    def publish(
        self,
        turn_id: str,
        event: Event,
        *,
        terminal: bool = False,
    ) -> TurnEventFrame:
        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is None:
                raise KeyError(turn_id)
            if stream.terminal:
                raise RuntimeError("cannot publish after terminal Turn Event")
            self._require_producer_loop(stream)
            frame = TurnEventFrame(
                turn_id=turn_id,
                sequence=stream.next_sequence,
                emitted_at_ms=time.time_ns() // 1_000_000,
                event=event,
                terminal=terminal,
            )
            stream.next_sequence += 1
            stream.frames.append(frame)
            stream.terminal = terminal
            detached: list[asyncio.Queue] = []
            for queue in tuple(stream.subscribers):
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    detached.append(queue)
            for queue in detached:
                stream.subscribers.pop(queue, None)
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - race guard
                        break
                queue.put_nowait(_DETACHED)
            self._streams.move_to_end(turn_id)
            return frame

    async def subscribe(
        self,
        turn_id: str,
        *,
        after_sequence: int = -1,
    ) -> AsyncIterator[TurnEventFrame]:
        """Strict compatibility iterator that raises rather than recovers gaps."""

        observation = self._open_observation(
            turn_id,
            after_sequence=after_sequence,
            recover_gap=False,
        )
        try:
            async for frame in observation:
                yield frame
        finally:
            await observation.aclose()

    def open_observation(
        self,
        turn_id: str,
        *,
        after_sequence: int = -1,
    ) -> TurnEventObservation:
        """Atomically decide gap/replay state and register for future frames."""

        return self._open_observation(
            turn_id,
            after_sequence=after_sequence,
            recover_gap=True,
        )

    def _open_observation(
        self,
        turn_id: str,
        *,
        after_sequence: int,
        recover_gap: bool,
    ) -> TurnEventObservation:
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < -1
        ):
            raise ValueError("after_sequence must be -1 or a non-negative integer")
        try:
            observer_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise EventHubLoopAffinityError(
                "Turn Event observation requires a running event loop"
            ) from exc
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.subscriber_queue_size)
        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is None:
                raise KeyError(turn_id)
            if observer_loop is not stream.producer_loop:
                raise EventHubLoopAffinityError(
                    "Turn Event observer must use the producer event loop"
                )
            latest_sequence = stream.next_sequence - 1
            frames = tuple(stream.frames)
            oldest_sequence = frames[0].sequence if frames else None
            # ``-1`` is the pre-V1 internal sentinel.  EventFrameV1 is
            # one-based, so both ``-1`` and wire cursor ``0`` mean "before the
            # first frame" for replay/gap decisions.
            effective_after_sequence = max(after_sequence, 0)
            gap_reason: Literal["cursor_evicted", "cursor_ahead"] | None = None
            if effective_after_sequence > latest_sequence:
                gap_reason = "cursor_ahead"
            elif (
                oldest_sequence is not None
                and effective_after_sequence + 1 < oldest_sequence
            ):
                gap_reason = "cursor_evicted"

            if gap_reason is not None and not recover_gap:
                if gap_reason == "cursor_ahead":
                    raise EventHistoryGap(
                        f"Turn Event cursor {after_sequence} is ahead of latest "
                        f"sequence {latest_sequence}"
                    )
                raise EventHistoryGap(
                    "Turn Event history gap; oldest available sequence is "
                    f"{oldest_sequence}"
                )

            gap = (
                TurnEventGap(
                    reason=gap_reason,
                    requested_after_sequence=after_sequence,
                    oldest_available_sequence=oldest_sequence,
                    latest_sequence=(latest_sequence if latest_sequence > 0 else None),
                )
                if gap_reason is not None
                else None
            )
            replay = (
                ()
                if gap is not None
                else tuple(
                    frame
                    for frame in frames
                    if frame.sequence > effective_after_sequence
                )
            )
            replay_terminal = bool(replay and replay[-1].terminal)
            registered = False
            if (
                gap_reason != "cursor_ahead"
                and not replay_terminal
                and not stream.terminal
            ):
                if len(stream.subscribers) >= self.max_subscribers_per_turn:
                    raise EventHubCapacityError(
                        "Turn Event observer capacity exhausted"
                    )
                stream.subscribers[queue] = observer_loop
                registered = True
            self._streams.move_to_end(turn_id)
            return TurnEventObservation(
                hub=self,
                turn_id=turn_id,
                queue=queue,
                observer_loop=observer_loop,
                replay=replay,
                registered=registered,
                stream_terminal=stream.terminal or gap_reason == "cursor_ahead",
                gap=gap,
                oldest_available_sequence=oldest_sequence,
                latest_sequence=(latest_sequence if latest_sequence > 0 else None),
            )

    def _detach_observer(self, turn_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is not None:
                stream.subscribers.pop(queue, None)

    def _close_observer(self, turn_id: str, queue: asyncio.Queue) -> None:
        """Detach and wake an in-flight ``__anext__`` with clean exhaustion."""

        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is not None:
                self._require_producer_loop(stream)
                stream.subscribers.pop(queue, None)
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race guard
                    break
            queue.put_nowait(_CLOSED)

    @staticmethod
    def _require_producer_loop(stream: _TurnStream) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise EventHubLoopAffinityError(
                "Turn Event Hub mutation must run on the producer event loop"
            ) from exc
        if running is not stream.producer_loop:
            raise EventHubLoopAffinityError(
                "Turn Event Hub mutation crossed the producer event loop"
            )

    def retained_frames(self, turn_id: str) -> tuple[TurnEventFrame, ...]:
        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is None:
                raise KeyError(turn_id)
            return tuple(stream.frames)

    def forget_turn(self, turn_id: str) -> bool:
        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is None:
                return False
            if stream.subscribers or not stream.terminal:
                return False
            self._streams.pop(turn_id)
            return True

    def abandon_turn(self, turn_id: str) -> bool:
        """Drop a non-authoritative stream after runner integrity failure."""

        with self._lock:
            stream = self._streams.get(turn_id)
            if stream is None:
                return False
            self._require_producer_loop(stream)
            self._streams.pop(turn_id)
            for queue in tuple(stream.subscribers):
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - race guard
                        break
                queue.put_nowait(_DETACHED)
            stream.subscribers.clear()
            return True


__all__ = [
    "EventHistoryGap",
    "EventHubCapacityError",
    "EventHubLoopAffinityError",
    "EventObserverDetached",
    "TurnEventGap",
    "TurnEventFrame",
    "TurnEventHub",
    "TurnEventObservation",
]
