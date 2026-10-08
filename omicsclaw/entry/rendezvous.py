"""Numbered requests that one Task awaits and another Task answers.

:class:`Rendezvous` is the part of asking a person that is the same
whatever is asked: an id for each request, a future the asker waits on, a
deadline, and one settlement frame for every answer.
:class:`~omicsclaw.entry.approval.ApprovalBroker` builds on it and supplies
the frames and the answer an expired request gets.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Iterator
from typing import Generic, TypeVar

from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.stream import TurnStream

__all__ = ["Rendezvous"]

Req = TypeVar("Req")
Ans = TypeVar("Ans")


class Rendezvous(Generic[Req, Ans]):
    """Numbered requests that one Task awaits and another Task answers.

    One instance serves one exchange. :meth:`ask` publishes a frame for the
    request on the exchange's stream and suspends its Task until
    :meth:`settle` or :meth:`abandon` answers from another Task, the
    deadline passes, or the asking Task is cancelled. Each answered request
    gets exactly one settlement frame; a cancelled one gets none.

    Not thread-safe: call every method on the event loop's thread.
    """

    __slots__ = (
        "_asked",
        "_numbering",
        "_on_expired",
        "_pending",
        "_settled",
        "_stream",
        "_timeout_s",
    )

    def __init__(
        self,
        stream: TurnStream,
        *,
        asked: Callable[[Req, str], TurnEvent],
        settled: Callable[[str, Ans], TurnEvent],
        on_expired: Callable[[], Ans],
        timeout_s: float | None = None,
        numbering: Iterator[int] | None = None,
    ) -> None:
        """Ids are ``<turn id>#<n>`` with n drawn from *numbering*.

        Args:
            stream: The exchange's stream; its ``turn_id`` prefixes each id.
            asked: Builds the frame that announces a request, given the
                request and its id. Called on the asking Task, so it can
                read that Task's tool context.
            settled: Builds the frame that reports an answer, given the
                request's id and the answer.
            on_expired: Gives the answer for a request whose deadline
                passed.
            timeout_s: Seconds each request waits; ``None`` waits until it
                is answered or cancelled.
            numbering: Where the n of each id comes from. Two instances
                given the same iterator never issue the same id. ``None``
                counts from 1 privately.

        Raises:
            ValueError: *timeout_s* is zero or negative.
        """
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive, or None to wait")
        self._stream = stream
        self._asked = asked
        self._settled = settled
        self._on_expired = on_expired
        self._timeout_s = timeout_s
        self._numbering = itertools.count(1) if numbering is None else numbering
        self._pending: dict[str, tuple[asyncio.Future[Ans], float | None]] = {}

    async def ask(self, request: Req) -> Ans:
        """Publish the request, then wait for its answer or its deadline.

        Returns:
            The answer :meth:`settle` or :meth:`abandon` gave, or
            ``on_expired()`` when the deadline passed first.

        Raises:
            asyncio.CancelledError: The asking Task was cancelled. The
                request is dropped first, so a later :meth:`settle` for it
                returns ``False``, and no settlement frame is published.
        """
        loop = asyncio.get_running_loop()
        request_id = f"{self._stream.turn_id}#{next(self._numbering)}"
        future: asyncio.Future[Ans] = loop.create_future()
        deadline = None if self._timeout_s is None else loop.time() + self._timeout_s
        self._pending[request_id] = (future, deadline)
        self._stream.publish(self._asked(request, request_id))

        try:
            answer = await self._wait(future, deadline)
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

        # Absent means abandon() already answered this request and
        # published its settlement frame.
        if self._pending.pop(request_id, None) is None:
            return answer
        self._stream.publish(self._settled(request_id, answer))
        return answer

    def settle(self, request_id: str, answer: Ans) -> bool:
        """Answer one outstanding request.

        Returns:
            ``False`` when *request_id* is unknown or already answered, and
            nothing changes.
        """
        waiting = self._pending.get(request_id)
        if waiting is None or waiting[0].done():
            return False
        waiting[0].set_result(answer)
        return True

    def abandon(self, answer: Ans) -> None:
        """Answer everything outstanding with *answer*, with a settlement frame each.

        Synchronous, so it completes inside a ``finally`` of a Task that
        has already been cancelled.
        """
        for request_id, (future, _deadline) in tuple(self._pending.items()):
            del self._pending[request_id]
            if not future.done():
                future.set_result(answer)
            self._stream.publish(self._settled(request_id, answer))

    def pending(self) -> tuple[str, ...]:
        """Ids still waiting, oldest first."""
        return tuple(self._pending)

    def expires_at(self, request_id: str) -> float | None:
        """Loop time at which the request's deadline passes.

        Returns:
            ``None`` when there is no deadline, and when *request_id* is
            unknown or already answered.
        """
        waiting = self._pending.get(request_id)
        if waiting is None or waiting[0].done():
            return None
        return waiting[1]

    async def _wait(self, future: asyncio.Future[Ans], deadline: float | None) -> Ans:
        """The future's result, or ``on_expired()`` once *deadline* has passed."""
        if deadline is None:
            return await future
        try:
            async with asyncio.timeout_at(deadline):
                return await future
        except TimeoutError:
            return self._on_expired()
