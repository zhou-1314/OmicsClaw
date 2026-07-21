"""Bounded process-local FIFO for accepted top-level Runs.

This Module owns executable payload locality, active-Run pressure and the sole
ordering path into Assignment eligibility.  It deliberately knows nothing
about compute dimensions, durable replay, or scientific execution details.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol, TypeVar


class RunDispatcherIntegrityError(RuntimeError):
    pass


class AcceptedRunPayload(Protocol):
    run_id: str


RunWorker = Callable[[AcceptedRunPayload], Awaitable[None]]
FaultHook = Callable[[str], None]
_T = TypeVar("_T")


@dataclass(slots=True)
class _AdmissionGuard:
    lock: asyncio.Lock
    users: int = 0


class RunBufferReservation:
    """Stateful capacity token spanning Manifest and Control commit."""

    __slots__ = ("_dispatcher", "_payload", "_run_id", "_state")

    def __init__(self, dispatcher: "RunDispatcher") -> None:
        self._dispatcher = dispatcher
        self._payload: AcceptedRunPayload | None = None
        self._run_id: str | None = None
        self._state = "reserved"

    async def commit(self, payload: AcceptedRunPayload) -> None:
        await self._dispatcher._commit(self, payload)

    async def release(self) -> None:
        await self._dispatcher._release(self)

    async def discard_terminalized(self) -> None:
        """Remove every possible partial enqueue after durable failure wins."""

        await self._dispatcher._discard_terminalized(self)

    async def quarantine(self) -> None:
        await self._dispatcher._quarantine(self)


class RunDispatcher:
    """Strict FIFO Run buffer with a separate active-orchestrator bound."""

    def __init__(
        self,
        *,
        max_buffered_runs: int,
        max_active_runs: int,
        max_admission_guards: int = 256,
        fault_hook: FaultHook | None = None,
    ) -> None:
        for name, value in (
            ("max_buffered_runs", max_buffered_runs),
            ("max_active_runs", max_active_runs),
            ("max_admission_guards", max_admission_guards),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_buffered_runs = max_buffered_runs
        self.max_active_runs = max_active_runs
        self.max_admission_guards = max_admission_guards
        self._fault_hook = fault_hook
        self._condition = asyncio.Condition()
        self._admission_lock = asyncio.Lock()
        self._queue: deque[AcceptedRunPayload] = deque()
        self._locations: dict[str, str] = {}
        self._active: dict[str, asyncio.Task[None]] = {}
        self._cancel_reasons: dict[str, str] = {}
        self._guards: dict[str, _AdmissionGuard] = {}
        self._buffered_entries = 0
        self._worker: RunWorker | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._ready = False
        self._closing = False
        self._quarantined = False

    @property
    def ready(self) -> bool:
        return self._ready and not self._closing and not self._quarantined

    @property
    def quarantined(self) -> bool:
        return self._quarantined

    async def start(self, worker: RunWorker) -> None:
        if not callable(worker):
            raise TypeError("worker must be callable")
        async with self._condition:
            # Quarantine outranks the raw `_ready` flag: a quarantined Dispatcher
            # still carries `_ready`, so checking it first would silently no-op
            # a restart attempt instead of failing closed.
            if self._quarantined:
                raise RunDispatcherIntegrityError(
                    "quarantined Run Dispatcher cannot start"
                )
            if self._ready:
                return
            if self._closing:
                raise RunDispatcherIntegrityError("closed Run Dispatcher cannot start")
            if self._queue or self._active or self._buffered_entries:
                raise RunDispatcherIntegrityError(
                    "Run Dispatcher startup requires empty process-local state"
                )
            self._worker = worker
            self._ready = True
            self._pump_task = asyncio.create_task(
                self._pump(), name="omicsclaw-run-dispatcher"
            )
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            if self._closing:
                tasks = tuple(self._active.values())
                pump = self._pump_task
            else:
                self._closing = True
                self._ready = False
                queued = tuple(self._queue)
                self._queue.clear()
                for payload in queued:
                    self._locations.pop(payload.run_id, None)
                self._buffered_entries -= len(queued)
                if self._buffered_entries < 0:
                    raise RunDispatcherIntegrityError(
                        "Run Dispatcher capacity accounting underflow"
                    )
                tasks = tuple(self._active.values())
                for run_id, task in tuple(self._active.items()):
                    self._cancel_reasons[run_id] = "shutdown"
                    task.cancel("shutdown")
                pump = self._pump_task
                self._condition.notify_all()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if pump is not None:
            await asyncio.gather(pump, return_exceptions=True)

    async def try_reserve(self) -> RunBufferReservation | None:
        async with self._condition:
            self._require_available()
            if self._buffered_entries >= self.max_buffered_runs:
                return None
            self._buffered_entries += 1
            return RunBufferReservation(self)

    @asynccontextmanager
    async def serialize_admission(self) -> AsyncIterator[None]:
        """Preserve completed durable-admission order through FIFO append."""

        await self._admission_lock.acquire()
        try:
            self._require_available()
            yield
        finally:
            self._admission_lock.release()

    @asynccontextmanager
    async def admission_guard(self, key: str) -> AsyncIterator[None]:
        """Serialize one Run Submission ID across all provisional side effects."""

        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("admission guard key must be a bounded string")
        async with self._condition:
            self._require_available()
            entry = self._guards.get(key)
            if entry is None:
                if len(self._guards) >= self.max_admission_guards:
                    raise RunDispatcherIntegrityError(
                        "Run Dispatcher admission guards are exhausted"
                    )
                entry = _AdmissionGuard(asyncio.Lock())
                self._guards[key] = entry
            entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            self._require_available()
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._condition:
                entry.users -= 1
                if entry.users < 0:
                    raise RunDispatcherIntegrityError(
                        "Run admission guard accounting underflow"
                    )
                if entry.users == 0:
                    current = self._guards.get(key)
                    if current is not entry or entry.lock.locked():
                        raise RunDispatcherIntegrityError(
                            "Run admission guard ownership drift"
                        )
                    self._guards.pop(key, None)

    async def cancel(self, run_id: str, *, reason: str) -> str:
        if reason not in {"owner", "shutdown"}:
            raise ValueError("unsupported Run cancellation reason")
        async with self._condition:
            location = self._locations.get(run_id)
            if location == "waiting":
                matches = [
                    index
                    for index, payload in enumerate(self._queue)
                    if payload.run_id == run_id
                ]
                if len(matches) != 1:
                    raise RunDispatcherIntegrityError(
                        "waiting Run FIFO/index identity drift"
                    )
                del self._queue[matches[0]]
                self._locations.pop(run_id, None)
                self._buffered_entries -= 1
                if self._buffered_entries < 0:
                    raise RunDispatcherIntegrityError(
                        "Run Dispatcher capacity accounting underflow"
                    )
                self._condition.notify_all()
                return "removed_waiting"
            if location == "active":
                task = self._active.get(run_id)
                if task is None:
                    raise RunDispatcherIntegrityError("active Run task is missing")
                self._cancel_reasons[run_id] = reason
                task.cancel(reason)
                return "signaled_active"
            return "not_live"

    async def cancellation_reason(self, run_id: str) -> str | None:
        async with self._condition:
            return self._cancel_reasons.get(run_id)

    async def _commit(
        self,
        reservation: RunBufferReservation,
        payload: AcceptedRunPayload,
    ) -> None:
        run_id = str(getattr(payload, "run_id", "") or "")
        if not run_id:
            raise ValueError("accepted Run payload must carry run_id")
        async with self._condition:
            self._require_available()
            if reservation._state != "reserved":
                raise RunDispatcherIntegrityError("Run reservation is already finished")
            if run_id in self._locations:
                raise RunDispatcherIntegrityError("Run already exists in Dispatcher")
            reservation._state = "committing"
            reservation._run_id = run_id
            reservation._payload = payload
            self._queue.append(payload)
            self._fault("enqueue.after_append")
            self._locations[run_id] = "waiting"
            self._fault("enqueue.after_index")
            reservation._state = "committed"
            self._condition.notify_all()

    async def _release(self, reservation: RunBufferReservation) -> None:
        async with self._condition:
            if reservation._state != "reserved":
                return
            self._buffered_entries -= 1
            if self._buffered_entries < 0:
                raise RunDispatcherIntegrityError(
                    "Run Dispatcher capacity accounting underflow"
                )
            reservation._state = "released"
            self._condition.notify_all()

    async def _discard_terminalized(
        self,
        reservation: RunBufferReservation,
    ) -> None:
        async with self._condition:
            if reservation._state == "released":
                return
            if reservation._state == "quarantined":
                raise RunDispatcherIntegrityError(
                    "quarantined Run reservation cannot be released"
                )
            if reservation._state == "reserved":
                self._buffered_entries -= 1
            elif reservation._state in {"committing", "committed"}:
                run_id = reservation._run_id
                payload = reservation._payload
                if run_id is None or payload is None:
                    raise RunDispatcherIntegrityError(
                        "partially committed Run lost payload identity"
                    )
                if self._locations.get(run_id) == "active":
                    raise RunDispatcherIntegrityError(
                        "terminalized Run already entered active execution"
                    )
                matches = [
                    index
                    for index, candidate in enumerate(self._queue)
                    if candidate is payload or candidate.run_id == run_id
                ]
                if len(matches) != 1:
                    raise RunDispatcherIntegrityError(
                        "partially committed Run FIFO identity drift"
                    )
                del self._queue[matches[0]]
                self._locations.pop(run_id, None)
                self._buffered_entries -= 1
            else:
                raise RunDispatcherIntegrityError("unknown Run reservation state")
            if self._buffered_entries < 0:
                raise RunDispatcherIntegrityError(
                    "Run Dispatcher capacity accounting underflow"
                )
            reservation._state = "released"
            self._condition.notify_all()

    async def _quarantine(self, reservation: RunBufferReservation) -> None:
        async with self._condition:
            self._quarantined = True
            reservation._state = "quarantined"
            self._condition.notify_all()

    async def _pump(self) -> None:
        try:
            while True:
                async with self._condition:
                    await self._condition.wait_for(
                        lambda: (
                            self._closing
                            or self._quarantined
                            or (
                                bool(self._queue)
                                and len(self._active) < self.max_active_runs
                            )
                        )
                    )
                    if self._closing or self._quarantined:
                        return
                    payload = self._queue.popleft()
                    run_id = payload.run_id
                    if self._locations.get(run_id) != "waiting":
                        raise RunDispatcherIntegrityError(
                            "Run FIFO and location index diverged"
                        )
                    self._locations[run_id] = "active"
                    self._buffered_entries -= 1
                    if self._buffered_entries < 0:
                        raise RunDispatcherIntegrityError(
                            "Run Dispatcher capacity accounting underflow"
                        )
                    task = asyncio.create_task(
                        self._execute(payload), name=f"omicsclaw-run-{run_id}"
                    )
                    task.add_done_callback(self._consume_worker_completion)
                    self._active[run_id] = task
                    self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except BaseException:
            # A dead pump must never leave an apparently-live Dispatcher. Without
            # this the raw `_ready` flag keeps `ready` True, `try_reserve` keeps
            # handing out capacity, and every accepted Run silently never starts.
            async with self._condition:
                self._quarantined = True
                self._condition.notify_all()
            raise

    async def _execute(self, payload: AcceptedRunPayload) -> None:
        drift = False
        try:
            worker = self._worker
            if worker is None:
                raise RunDispatcherIntegrityError("Run Dispatcher has no worker")
            await worker(payload)
        except asyncio.CancelledError:
            raise
        except BaseException:
            async with self._condition:
                self._quarantined = True
                self._condition.notify_all()
            raise
        finally:
            async with self._condition:
                run_id = payload.run_id
                current = self._active.get(run_id)
                if current is not asyncio.current_task():
                    # The slot belongs to another Task: quarantine, but never
                    # evict its bookkeeping, and never raise from `finally` —
                    # that would mask the exception already unwinding, including
                    # CancelledError.
                    self._quarantined = True
                    drift = True
                else:
                    self._active.pop(run_id, None)
                    self._locations.pop(run_id, None)
                    self._cancel_reasons.pop(run_id, None)
                self._condition.notify_all()
        if drift:
            raise RunDispatcherIntegrityError("Run active-task ownership drift")

    @staticmethod
    def _consume_worker_completion(task: asyncio.Task[None]) -> None:
        """Retrieve every Task outcome; integrity failures already quarantine."""

        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def _require_available(self) -> None:
        if self._quarantined:
            raise RunDispatcherIntegrityError("Run Dispatcher is quarantined")
        if not self._ready or self._closing:
            raise RunDispatcherIntegrityError("Run Dispatcher is not ready")

    def _fault(self, name: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(name)


__all__ = [
    "RunBufferReservation",
    "RunDispatcher",
    "RunDispatcherIntegrityError",
]
