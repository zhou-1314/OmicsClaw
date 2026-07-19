"""Feishu text slice through the real ControlRuntime and Delivery Outbox.

The other Feishu tests use a recording runtime double, which proves the
normalization contract but not that the cutover actually works.  This module
drives a real `ControlRuntime.for_channel_surface(adapter="feishu")` end to end:
inbound event -> Turn -> terminal Transcript -> Outbox -> exactly one Feishu
provider call carrying the frozen reply.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimePorts,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
)
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Final
from omicsclaw.surfaces.channels.feishu_delivery import FeishuDeliveryAdapter


class _FakeResponse:
    def __init__(self, *, ok: bool = True, code: int | None = None, message_id="om_1"):
        self._ok = ok
        self.code = code
        self.data = SimpleNamespace(message_id=message_id)

    def success(self) -> bool:
        return self._ok


class _FakeLarkClient:
    def __init__(self, responses=None):
        self.sent: list[dict] = []
        self._responses = list(responses or [])
        outer = self

        class _Message:
            def create(self, request):
                outer.sent.append(request)
                if outer._responses:
                    return outer._responses.pop(0)
                return _FakeResponse()

        self.im = SimpleNamespace(v1=SimpleNamespace(message=_Message()))


def _raw(text: str, *, request_id: str, chat_id: str = "oc_chat_1") -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="channel",
        source_namespace="channel/feishu/v1/cli_app_1",
        source_request_id=request_id,
        external_subject={"kind": "feishu_user", "value": "ou_owner"},
        reply_target={
            "schema_version": 1,
            "kind": "channel",
            "adapter": "feishu",
            "account_namespace": "cli_app_1",
            "destination_id": chat_id,
            "destination_kind": "chat_id",
        },
        content=(RawContentBlockV1(kind="text", text=text),),
    )


def _runtime(tmp_path, client, *, dispatch_events) -> ControlRuntime:
    return ControlRuntime.for_channel_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        adapter="feishu",
        account_namespace="cli_app_1",
        owner_identities={
            "channel/feishu/cli_app_1/feishu_user": frozenset({"ou_owner"})
        },
        delivery_adapter=FeishuDeliveryAdapter(
            client, request_builder=lambda arguments: arguments
        ),
        dispatch_events=dispatch_events,
    )


async def _drain(runtime: ControlRuntime, client, *, expected: int) -> None:
    for _ in range(200):
        if len(client.sent) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} sends, saw {len(client.sent)}")


@pytest.mark.asyncio
async def test_feishu_turn_delivers_its_reply_through_the_outbox(tmp_path):
    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("spatial preprocess finished")

    client = _FakeLarkClient()
    runtime = _runtime(tmp_path, client, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _raw("run a spatial preprocess", request_id="om_1"),
            ControlRuntimePorts(user_id="ou_owner"),
        )
        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        await _drain(runtime, client, expected=1)

        sent = client.sent[0]
        assert sent["receive_id"] == "oc_chat_1"
        assert sent["receive_id_type"] == "chat_id"
        assert sent["msg_type"] == "text"
        assert "spatial preprocess finished" in sent["content"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_duplicate_feishu_event_delivers_exactly_one_reply(tmp_path):
    dispatched = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatched
        dispatched += 1
        yield Final("answered once")

    client = _FakeLarkClient()
    runtime = _runtime(tmp_path, client, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        first = await runtime.submit_and_wait(
            _raw("hello", request_id="om_same"),
            ControlRuntimePorts(user_id="ou_owner"),
        )
        second = await runtime.submit_and_wait(
            _raw("hello", request_id="om_same"),
            ControlRuntimePorts(user_id="ou_owner"),
        )
        await _drain(runtime, client, expected=1)
        await asyncio.sleep(0.05)

        assert first.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert second.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert dispatched == 1
        # The provider must see one reply, not two.
        assert len(client.sent) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_replies_to_one_feishu_chat_are_serialized_in_turn_order(tmp_path):
    """ADR 0063: one Reply Target sees replies in target-sequence order."""

    async def dispatch_events(envelope: MessageEnvelope):
        text = envelope.stored_user_content
        yield Final(f"reply to {text}" if isinstance(text, str) else "reply")

    client = _FakeLarkClient()
    runtime = _runtime(tmp_path, client, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        for index in range(3):
            outcome = await runtime.submit_and_wait(
                _raw(f"question {index}", request_id=f"om_{index}"),
                ControlRuntimePorts(user_id="ou_owner"),
            )
            assert outcome.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        await _drain(runtime, client, expected=3)

        deliveries = runtime.repository.list_deliveries()
        sequences = [record.target_sequence for record in deliveries]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == 3
        assert len(client.sent) == 3
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_non_owner_feishu_sender_creates_no_turn_and_no_delivery(tmp_path):
    async def dispatch_events(_envelope: MessageEnvelope):  # pragma: no cover
        raise AssertionError("a non-Owner must never reach the Agent")
        yield

    client = _FakeLarkClient()
    runtime = _runtime(tmp_path, client, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        raw = _raw("let me in", request_id="om_x")
        stranger = RawInboundV1(
            schema_version=raw.schema_version,
            surface=raw.surface,
            source_namespace=raw.source_namespace,
            source_request_id=raw.source_request_id,
            external_subject={"kind": "feishu_user", "value": "ou_stranger"},
            reply_target=dict(raw.reply_target),
            content=raw.content,
        )
        result = await runtime.submit_and_wait(
            stranger, ControlRuntimePorts(user_id="ou_stranger")
        )

        assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert result.acceptance.code == "owner_denied"
        assert client.sent == []
        assert runtime.repository.list_deliveries() == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_permanent_provider_rejection_does_not_stall_the_next_reply(tmp_path):
    """A refused send must terminalize so the Reply Target barrier releases."""

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("some answer")

    client = _FakeLarkClient(
        responses=[_FakeResponse(ok=False, code=230002), _FakeResponse()]
    )
    runtime = _runtime(tmp_path, client, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        await runtime.submit_and_wait(
            _raw("first", request_id="om_a"),
            ControlRuntimePorts(user_id="ou_owner"),
        )
        await runtime.submit_and_wait(
            _raw("second", request_id="om_b"),
            ControlRuntimePorts(user_id="ou_owner"),
        )
        # The first Delivery is permanently rejected; the second must still be
        # attempted rather than blocked forever behind it.
        await _drain(runtime, client, expected=2)
    finally:
        await runtime.close()
