"""A process restart never resubmits an admitted side effect."""

import asyncio

import pytest

from omicsclaw.entry import AppConfig, attach_sessions, build_app
from omicsclaw.entry.session import SubmissionRefused
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog
from omicsclaw.schema import Message, Role
from tests.entry.test_turn_runner import Scripted


def test_request_receipt_survives_process_restart(tmp_path):
    async def run():
        config = AppConfig(workspace=tmp_path, durable_turn_events=True)
        provider = Scripted(Message(role=Role.ASSISTANT, content="done"))
        app = attach_sessions(build_app(config, provider=provider))
        handle = await app.sessions.submit(
            "session", "do once", source_request_id="request"
        )
        await handle.wait()
        receipt = app.sessions.request_receipt("session", "request")
        assert receipt["turn_id"] == handle.turn_id
        await app.aclose()
        resumed = attach_sessions(build_app(config, provider=provider))
        try:
            assert resumed.sessions.request_receipt("session", "request") == receipt
            with pytest.raises(SubmissionRefused):
                await resumed.sessions.submit(
                    "session", "do once", source_request_id="request"
                )
            with pytest.raises(SubmissionRefused):
                await resumed.sessions.submit(
                    "session", "different", source_request_id="request"
                )
        finally:
            await resumed.aclose()

    asyncio.run(run())


def test_receipt_has_stable_workspace_and_authority_identity(tmp_path):
    path = tmp_path / "state.db"
    first = Database(path)
    log = DurableTurnEventLog(first)
    receipt = log.claim_request("session", "request", "hash", "turn")
    identity = log.identity
    assert receipt["authority_id"] == identity.authority_id
    assert receipt["workspace_id"] == identity.workspace_id
    first.close()
    second = Database(path)
    try:
        reopened = DurableTurnEventLog(second)
        assert reopened.identity == identity
        assert reopened.request_receipt("session", "request") == receipt
    finally:
        second.close()


def test_admission_is_durable_before_lane_runs_and_ack_is_received(
    tmp_path, monkeypatch
):
    async def run():
        config = AppConfig(workspace=tmp_path, durable_turn_events=True)
        provider = Scripted(Message(role=Role.ASSISTANT, content="should not execute"))
        app = attach_sessions(build_app(config, provider=provider))
        executed = asyncio.Event()

        async def blocked_pump(_self, _lane):
            executed.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(type(app.sessions), "_pump", blocked_pump)
        await app.sessions.submit("session", "once", source_request_id="lost-ack")
        # Discard the acknowledgement. No lane or model has run yet.
        receipt = app.sessions.request_receipt("session", "lost-ack")
        assert receipt is not None
        assert not executed.is_set()
        await app.sessions.shutdown(0)
        await app.aclose()
        reopened = attach_sessions(build_app(config, provider=provider))
        try:
            assert reopened.sessions.request_receipt("session", "lost-ack") == receipt
            with pytest.raises(SubmissionRefused):
                await reopened.sessions.submit(
                    "session", "once", source_request_id="lost-ack"
                )
            assert not reopened.sessions.is_busy("session")
        finally:
            await reopened.aclose()

    asyncio.run(run())


def test_canonical_payload_order_reuses_live_handle(tmp_path):
    async def run():
        app = attach_sessions(
            build_app(
                AppConfig(workspace=tmp_path, durable_turn_events=True),
                provider=Scripted(Message(role=Role.ASSISTANT, content="done")),
            )
        )
        try:
            first = await app.sessions.submit(
                "session", "once", values={"a": 1, "b": 2}, source_request_id="same"
            )
            second = await app.sessions.submit(
                "session", "once", values={"b": 2, "a": 1}, source_request_id="same"
            )
            assert first is second
            await first.wait()
        finally:
            await app.aclose()

    asyncio.run(run())


def test_failed_receipt_write_cannot_schedule_a_turn(tmp_path, monkeypatch):
    async def run():
        app = attach_sessions(
            build_app(
                AppConfig(workspace=tmp_path, durable_turn_events=True),
                provider=Scripted(
                    Message(role=Role.ASSISTANT, content="should not execute")
                ),
            )
        )

        def unavailable(*_args):
            raise OSError("receipt storage unavailable")

        monkeypatch.setattr(app.sessions._event_log, "claim_request", unavailable)
        try:
            with pytest.raises(OSError, match="storage unavailable"):
                await app.sessions.submit("session", "once", source_request_id="id")
            assert not app.sessions.is_busy("session")
        finally:
            await app.aclose()

    asyncio.run(run())
