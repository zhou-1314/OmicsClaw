from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from omicsclaw.control.run_dispatcher import (
    RunDispatcher,
    RunDispatcherIntegrityError,
)


@dataclass(frozen=True)
class _Payload:
    run_id: str


def test_dispatcher_reservation_is_bounded_and_releasable() -> None:
    async def scenario() -> None:
        dispatcher = RunDispatcher(max_buffered_runs=1, max_active_runs=1)
        await dispatcher.start(lambda _payload: asyncio.sleep(0))
        first = await dispatcher.try_reserve()
        assert first is not None
        assert await dispatcher.try_reserve() is None
        await first.release()
        assert await dispatcher.try_reserve() is not None
        await dispatcher.close()

    asyncio.run(scenario())


def test_dispatcher_runs_committed_fifo_with_bounded_active_count() -> None:
    async def scenario() -> None:
        started: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        all_done = asyncio.Event()

        async def worker(payload: _Payload) -> None:
            started.append(payload.run_id)
            if payload.run_id == "a":
                first_started.set()
                await release_first.wait()
            if len(started) == 2:
                all_done.set()

        dispatcher = RunDispatcher(max_buffered_runs=2, max_active_runs=1)
        await dispatcher.start(worker)
        first = await dispatcher.try_reserve()
        second = await dispatcher.try_reserve()
        assert first is not None and second is not None
        await first.commit(_Payload("a"))
        await second.commit(_Payload("b"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert started == ["a"]
        release_first.set()
        await asyncio.wait_for(all_done.wait(), timeout=1)
        assert started == ["a", "b"]
        await dispatcher.close()

    asyncio.run(scenario())


def test_discard_terminalized_removes_partially_committed_payload() -> None:
    async def scenario() -> None:
        invoked: list[str] = []
        armed = True

        def fault(name: str) -> None:
            if armed and name == "enqueue.after_append":
                raise RuntimeError("injected enqueue crash")

        async def worker(payload: _Payload) -> None:
            invoked.append(payload.run_id)

        dispatcher = RunDispatcher(
            max_buffered_runs=1,
            max_active_runs=1,
            fault_hook=fault,
        )
        await dispatcher.start(worker)
        reservation = await dispatcher.try_reserve()
        assert reservation is not None
        with pytest.raises(RuntimeError, match="injected"):
            await reservation.commit(_Payload("a"))
        armed = False
        await reservation.discard_terminalized()
        await asyncio.sleep(0)
        assert invoked == []
        assert await dispatcher.try_reserve() is not None
        await dispatcher.close()

    asyncio.run(scenario())


def test_quarantine_blocks_future_admission_after_uncertain_ownership() -> None:
    async def scenario() -> None:
        dispatcher = RunDispatcher(max_buffered_runs=1, max_active_runs=1)
        await dispatcher.start(lambda _payload: asyncio.sleep(0))
        reservation = await dispatcher.try_reserve()
        assert reservation is not None
        await reservation.quarantine()
        with pytest.raises(RunDispatcherIntegrityError, match="quarantined"):
            await dispatcher.try_reserve()
        await dispatcher.close()

    asyncio.run(scenario())


def test_cancel_removes_waiting_or_cancels_active_worker() -> None:
    async def scenario() -> None:
        active_started = asyncio.Event()
        active_canceled = asyncio.Event()

        async def worker(payload: _Payload) -> None:
            if payload.run_id == "active":
                active_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    active_canceled.set()
                    raise

        dispatcher = RunDispatcher(max_buffered_runs=2, max_active_runs=1)
        await dispatcher.start(worker)
        active = await dispatcher.try_reserve()
        waiting = await dispatcher.try_reserve()
        assert active is not None and waiting is not None
        await active.commit(_Payload("active"))
        await waiting.commit(_Payload("waiting"))
        await active_started.wait()

        assert await dispatcher.cancel("waiting", reason="owner") == "removed_waiting"
        assert await dispatcher.cancel("active", reason="owner") == "signaled_active"
        await asyncio.wait_for(active_canceled.wait(), timeout=1)
        await dispatcher.close()

    asyncio.run(scenario())


def test_canceled_admission_waiter_releases_bounded_guard_capacity() -> None:
    async def scenario() -> None:
        dispatcher = RunDispatcher(
            max_buffered_runs=1,
            max_active_runs=1,
            max_admission_guards=1,
        )
        await dispatcher.start(lambda _payload: asyncio.sleep(0))
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold() -> None:
            async with dispatcher.admission_guard("same"):
                holder_entered.set()
                await release_holder.wait()

        async def wait() -> None:
            async with dispatcher.admission_guard("same"):
                raise AssertionError("canceled waiter entered the guard")

        holder = asyncio.create_task(hold())
        await holder_entered.wait()
        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release_holder.set()
        await holder

        async with dispatcher.admission_guard("new"):
            pass
        await dispatcher.close()

    asyncio.run(scenario())


def test_worker_failure_is_observed_and_quarantines_novel_admission() -> None:
    async def scenario() -> None:
        async def worker(_payload: _Payload) -> None:
            raise RuntimeError("injected worker integrity failure")

        dispatcher = RunDispatcher(max_buffered_runs=1, max_active_runs=1)
        await dispatcher.start(worker)
        reservation = await dispatcher.try_reserve()
        assert reservation is not None
        await reservation.commit(_Payload("failed"))
        for _ in range(100):
            if dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert dispatcher.quarantined is True
        with pytest.raises(RunDispatcherIntegrityError, match="quarantined"):
            await dispatcher.try_reserve()
        await dispatcher.close()

    asyncio.run(scenario())
