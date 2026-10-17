"""A bounded producer queue feeding the existing durable event stream."""

import asyncio
import threading
from collections import deque

from omicsclaw.entry.render import to_wire
from omicsclaw.entry.stream import TurnStream, _stream_key

from .contracts import encoded


class OwnerOutputStream:
    def __init__(
        self,
        session_id,
        execution_id,
        events,
        *,
        queue_bytes=256 * 1024,
        queue_count=128,
        total_bytes=1024 * 1024,
        failure=None,
    ):
        self.stream = TurnStream(
            session_id, execution_id, durable_log=events, ring_size=32
        )
        self.events = events
        self.key = _stream_key(session_id, execution_id)
        self.queue = deque()
        self.lock = threading.Lock()
        self.bytes = 0
        self.byte_limit, self.count_limit = queue_bytes, queue_count
        self.total_bytes, self.total_limit = 0, total_bytes
        self.failure = failure or (lambda: None)
        self.error = False
        self.finished = False
        self.closed = False
        self.task = asyncio.create_task(self._pump())

    @property
    def _durable_owner_epoch(self):
        return self.stream._durable_owner_epoch

    def publish(self, event):
        if event is None:
            return
        cost = len(encoded(to_wire(event)))
        with self.lock:
            if self.closed or self.error:
                return
            if (
                self.bytes + cost > self.byte_limit
                or len(self.queue) >= self.count_limit
                or self.total_bytes + cost > self.total_limit
            ):
                self.error = True
                self.failure()
                return
            self.queue.append((event, cost))
            self.bytes += cost
            self.total_bytes += cost

    publish_threadsafe = publish

    async def _pump(self):
        try:
            while not self.closed:
                item = None
                with self.lock:
                    if self.queue:
                        item = self.queue.popleft()
                        self.bytes -= item[1]
                    elif self.finished:
                        return
                if item is None:
                    await asyncio.sleep(0.01)
                    continue
                self.stream.publish(item[0])
                if not await self.stream.flush_durable():
                    self.error = True
                    self.failure()
        except Exception:  # noqa: BLE001 - preserve a visible durable writer failure
            self.error = True
            self.failure()

    async def flush_durable(self):
        # Start and terminal flushes share this queue; a slow disk never grows it.
        for _ in range(1500):
            with self.lock:
                empty = not self.queue
            if empty:
                try:
                    result = await asyncio.wait_for(self.stream.flush_durable(), 15)
                    return result and not self.error
                except TimeoutError:
                    break
            await asyncio.sleep(0.01)
        self.error = True
        self.failure()
        return False

    async def renew(self):
        if not self.closed:
            await asyncio.to_thread(
                self.events.renew_owner, self.key, epoch=self._durable_owner_epoch
            )

    async def close(self):
        self.finished = True
        await self.flush_durable()
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
