"""SSH requires confirmed event writes even when CLI durability is optional."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from omicsclaw.entry.desktop.turn_submission import ingress_identity
from omicsclaw.entry.events import TurnEvent, TurnEventType
from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.ssh.protocol import RpcError
from omicsclaw.entry.stream import TurnStream
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog


class Peer:
    def __init__(self):
        self.handlers = {}
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)


def setup_observer(tmp_path):
    peer = Peer()
    controller = Controller(tmp_path, peer)
    database = Database(tmp_path / ".omicsclaw" / "memory.db")
    log = DurableTurnEventLog(database)
    stream = TurnStream("synthetic", "a" * 32, durable_log=log)
    controller.app = SimpleNamespace(
        memory=SimpleNamespace(database=database),
        sessions=SimpleNamespace(durable_identity=log.identity),
    )
    controller.model_snapshot = SimpleNamespace(provider_id="custom", model="m")
    cancelled = []

    async def wait():
        await asyncio.Event().wait()

    handle = SimpleNamespace(
        session_id=stream.session_id,
        turn_id=stream.turn_id,
        stream=stream,
        observe=stream.observe,
        approvals=None,
        compaction_only=False,
        cancel=lambda: cancelled.append(True),
        wait=wait,
    )
    return controller, database, log, stream, peer, handle, cancelled


def publish_exchange(stream):
    stream.publish(
        TurnEvent(
            TurnEventType.TEXT_DELTA,
            0,
            stream.session_id,
            stream.turn_id,
            wire_payload={"delta": "synthetic confirmed text"},
        )
    )
    stream.publish(
        TurnEvent(
            TurnEventType.EXCHANGE_END,
            0,
            stream.session_id,
            stream.turn_id,
            terminal="converged",
        )
    )


def test_confirmed_events_are_broadcast_with_stable_durable_identity(tmp_path):
    async def run():
        controller, db, log, stream, peer, handle, _ = setup_observer(tmp_path)
        try:
            publish_exchange(stream)
            await controller._observe(handle)
            assert [
                json.loads(row["params"]["frame"].split("data: ", 1)[1])["type"]
                for row in peer.frames
            ] == ["text", "result", "done"]
            assert len(log.replay('["synthetic","' + "a" * 32 + '"]')) == 2
        finally:
            await stream.flush_durable()
            db.close()
            controller.owner.close()

    asyncio.run(run())


def test_append_failure_cancels_without_business_frames_and_sends_bounded_fault(
    tmp_path,
):
    async def run():
        controller, db, log, stream, peer, handle, cancelled = setup_observer(tmp_path)

        def reject(event):
            raise sqlite3.OperationalError("synthetic append fault")

        log.append = reject
        try:
            publish_exchange(stream)
            with pytest.raises(RpcError, match="durable_turn_write_failed"):
                await asyncio.wait_for(controller._observe(handle), 2)
            assert cancelled
            assert [row["method"] for row in peer.frames] == ["chat.fault"]
            assert peer.frames[0]["params"]["code"] == "durable_turn_write_failed"
            assert "durableIdentity" not in peer.frames[0]["params"]
            assert not log.replay('["synthetic","' + "a" * 32 + '"]')
        finally:
            await stream.flush_durable()
            db.close()
            controller.owner.close()

    asyncio.run(run())


def test_optional_shared_stream_preserves_cli_observation_on_append_failure(tmp_path):
    async def run():
        db = Database(tmp_path / "memory.db")
        log = DurableTurnEventLog(db)

        def reject(event):
            raise sqlite3.OperationalError("synthetic optional append fault")

        log.append = reject
        stream = TurnStream("cli", "b" * 32, durable_log=log)
        try:
            publish_exchange(stream)
            async with stream.observe() as observation:
                types = [event.type async for event in observation]
            assert types == [TurnEventType.TEXT_DELTA, TurnEventType.EXCHANGE_END]
            assert await stream.flush_durable() is False
        finally:
            await stream.flush_durable()
            db.close()

    asyncio.run(run())


def test_observer_cancellation_detaches_without_transport_fault(tmp_path):
    async def run():
        controller, db, _log, stream, peer, handle, _ = setup_observer(tmp_path)
        try:
            observing = asyncio.create_task(controller._observe(handle))
            await asyncio.sleep(0)
            observing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observing
            assert not peer.frames and stream.observer_count() == 0
        finally:
            db.close()
            controller.owner.close()

    asyncio.run(run())


def test_original_observer_confirms_full_raw_usage_and_terminal_before_done(tmp_path):
    async def run():
        controller, db, log, stream, peer, handle, _ = setup_observer(tmp_path)
        controller.installation_id = "1" * 32
        key = ingress_identity(controller.installation_id)[2] + "/" + "2" * 32
        log.claim_request(
            stream.session_id,
            key,
            "3" * 64,
            stream.turn_id,
            {
                "text": "first",
                "values": {
                    "__desktop_model_snapshot__": {
                        "provider": "accepted",
                        "model": "accepted-model",
                    }
                },
            },
        )
        try:
            stream.publish(
                TurnEvent(
                    TurnEventType.TEXT_DELTA,
                    0,
                    stream.session_id,
                    stream.turn_id,
                    wire_payload={"delta": "confirmed"},
                )
            )
            stream.publish(
                TurnEvent(
                    TurnEventType.TURN_END,
                    0,
                    stream.session_id,
                    stream.turn_id,
                    wire_payload={
                        "usage_reported": True,
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    },
                )
            )
            stream.publish(
                TurnEvent(
                    TurnEventType.EXCHANGE_END,
                    0,
                    stream.session_id,
                    stream.turn_id,
                    terminal="converged",
                )
            )
            await controller._observe(handle)
            page = await controller.session_snapshot(
                {"sessionId": stream.session_id, "turnId": stream.turn_id}
            )
            assert page["snapshot"]["coverage"] == {
                "firstSeq": 1,
                "lastSeq": 3,
                "lastSubframe": 1,
            }
            assert [row["frame"] for row in page["frames"]] == [
                row["params"]["frame"] for row in peer.frames
            ]
            data = json.loads(
                json.loads(page["frames"][-2]["frame"].split("data: ", 1)[1])["data"]
            )
            assert (
                data["usage_reported"]
                and data["model_calls"] == 1
                and data["provider"] == "accepted"
            )
        finally:
            await stream.flush_durable()
            db.close()
            controller.owner.close()

    asyncio.run(run())


def test_eof_done_has_no_durable_terminal_and_never_confirms_a_snapshot(tmp_path):
    async def run():
        controller, db, log, _stream, peer, handle, cancelled = setup_observer(tmp_path)

        class EmptyObservation:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                pass

        handle.observe = EmptyObservation
        try:
            with pytest.raises(RpcError, match="durable_turn_terminal_missing"):
                await controller._observe(handle)
            assert cancelled and [row["method"] for row in peer.frames] == [
                "chat.fault"
            ]
            assert (
                log.projection_page(handle.session_id, handle.turn_id)["snapshot"]
                is None
            )
        finally:
            db.close()
            controller.owner.close()

    asyncio.run(run())
