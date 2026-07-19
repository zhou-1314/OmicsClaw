"""Feishu authoritative ingress cutover (ADR 0060/0063, text-only slice).

Feishu events arrive on the lark WebSocket thread and are bridged into the
Channel's background loop.  These tests pin the normalization contract and
prove the handler produces no reply of its own: every terminal reply must
leave through the persistent Delivery Outbox.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.control import (
    ControlRuntimeResult,
    TurnAcceptanceResult,
    TurnAcceptanceStatus,
)
from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig


class _RecordingControlRuntime:
    def __init__(
        self,
        *,
        acceptance_status: TurnAcceptanceStatus = TurnAcceptanceStatus.ACCEPTED,
        acceptance_code: str = "",
    ) -> None:
        self.calls: list[tuple[object, object]] = []
        self._result = ControlRuntimeResult(
            acceptance=TurnAcceptanceResult(
                status=acceptance_status,
                turn_id="turn-1"
                if acceptance_status is not TurnAcceptanceStatus.REJECTED
                else "",
                conversation_id=(
                    "conversation-1"
                    if acceptance_status is not TurnAcceptanceStatus.REJECTED
                    else ""
                ),
                code=acceptance_code,
            ),
            receipt=None,
        )

    async def submit(self, raw, ports, **_kwargs):
        self.calls.append((raw, ports))
        return self._result

    async def submit_and_wait(self, raw, ports, **_kwargs):  # pragma: no cover
        raise AssertionError(
            "the Feishu WS thread must not block on terminal execution"
        )


def _channel(
    *, owners=("ou_owner",), app_id="cli_app_1", bot_open_id="ou_bot"
) -> FeishuChannel:
    return FeishuChannel(
        FeishuConfig(
            app_id=app_id,
            app_secret="secret",
            allowed_senders=set(owners),
            bot_open_id=bot_open_id,
        )
    )


def _mention(open_id: str):
    return SimpleNamespace(id=SimpleNamespace(open_id=open_id))


def _event(
    *,
    text: str = "run a spatial preprocess",
    sender: str = "ou_owner",
    chat_id: str = "oc_chat_1",
    message_id: str = "om_msg_1",
    chat_type: str = "p2p",
    mentions=None,
    message_type: str = "text",
):
    import json as _json

    message = SimpleNamespace(
        chat_id=chat_id,
        message_id=message_id,
        chat_type=chat_type,
        message_type=message_type,
        content=_json.dumps({"text": text}),
        mentions=mentions,
        create_time=None,
    )
    return SimpleNamespace(
        event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender)),
        )
    )


@pytest.mark.asyncio
async def test_submit_control_inbound_builds_stable_raw_inbound():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    result = await channel._submit_control_inbound(
        chat_id="oc_chat_1",
        message_id="om_msg_1",
        sender_open_id="ou_owner",
        text="frozen user text",
    )

    assert result is runtime._result
    assert len(runtime.calls) == 1
    raw, ports = runtime.calls[0]
    assert raw.schema_version == 1
    assert raw.surface == "channel"
    assert raw.source_namespace == "channel/feishu/v1/cli_app_1"
    # Feishu message_id is stable across event redelivery, so it is the
    # durable Source Request ID that binds a retry to the original Turn.
    assert raw.source_request_id == "om_msg_1"
    assert dict(raw.external_subject) == {
        "kind": "feishu_user",
        "value": "ou_owner",
    }
    assert dict(raw.reply_target) == {
        "schema_version": 1,
        "kind": "channel",
        "adapter": "feishu",
        "account_namespace": "cli_app_1",
        "destination_id": "oc_chat_1",
        "destination_kind": "chat_id",
    }
    assert tuple((block.kind, block.text) for block in raw.content) == (
        ("text", "frozen user text"),
    )
    assert dict(raw.transport_facts) == {
        "provider_event_kind": "im.message.receive_v1"
    }
    assert ports.user_id == "ou_owner"


def _drive(channel: FeishuChannel, event) -> None:
    """Run `_handle_event` with a real background loop, as `start()` would."""

    loop = asyncio.new_event_loop()
    import threading

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    channel._loop = loop
    try:
        channel._handle_event(event)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_owner_text_reaches_control_runtime_and_sends_no_reply():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(text="analyse this"))

    assert len(runtime.calls) == 1
    raw, _ports = runtime.calls[0]
    assert raw.content[0].text == "analyse this"


def test_non_owner_message_never_reaches_the_control_runtime():
    channel = _channel(owners=("ou_owner",))
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(sender="ou_stranger"))

    assert runtime.calls == []


def test_message_without_sender_identity_is_ignored():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(sender=""))

    assert runtime.calls == []


def test_duplicate_message_id_is_not_resubmitted():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(message_id="om_same"))
    _drive(channel, _event(message_id="om_same"))

    assert len(runtime.calls) == 1


def test_rejected_message_stays_eligible_for_provider_redelivery():
    """The local cache must not swallow a retry of a transiently rejected Turn.

    The durable Ingress Idempotency Binding is the deduplication authority; a
    local cache that marked the message before acceptance would silently lose
    it when Feishu redelivered inside the TTL.
    """

    channel = _channel()
    runtime = _RecordingControlRuntime(
        acceptance_status=TurnAcceptanceStatus.REJECTED,
        acceptance_code="turn_backpressure",
    )
    channel._control_runtime = runtime

    _drive(channel, _event(message_id="om_retry"))
    _drive(channel, _event(message_id="om_retry"))

    assert len(runtime.calls) == 2


def test_group_message_without_mention_is_ignored():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(
        channel,
        _event(chat_id="oc_chat_g", chat_type="group", mentions=None),
    )

    assert runtime.calls == []


def test_group_mention_of_another_human_is_ignored():
    """A non-empty mentions list is not proof that WE were addressed."""

    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(
        channel,
        _event(
            chat_id="oc_chat_g",
            chat_type="group",
            text="@_user_1 can you review this",
            mentions=[_mention("ou_someone_else")],
        ),
    )

    assert runtime.calls == []


def test_group_chat_fails_closed_without_a_configured_bot_open_id():
    channel = _channel(bot_open_id="")
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(
        channel,
        _event(
            chat_id="oc_chat_g",
            chat_type="group",
            text="@_user_1 run the demo",
            mentions=[_mention("ou_bot")],
        ),
    )

    assert runtime.calls == []


def test_group_mention_is_submitted_with_the_mention_stripped():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(
        channel,
        _event(
            chat_id="oc_chat_g",
            chat_type="group",
            text="@_user_1 run the demo",
            mentions=[_mention("ou_bot")],
        ),
    )

    assert len(runtime.calls) == 1
    raw, _ports = runtime.calls[0]
    assert raw.content[0].text == "run the demo"


@pytest.mark.parametrize(
    "message_type", ["image", "post", "file", "audio", "media", "sticker"]
)
def test_non_text_message_types_fail_closed(message_type: str):
    """ADR 0059 has no Feishu Attachment Store cutover; bytes must not be dropped.

    The gate is on the provider message type rather than on parsed attachments,
    because the legacy parser downloads content as a side effect and, when a
    download yields nothing, still returns synthesized text like "[image]".
    Admitting that would answer a question the Owner never asked.
    """

    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(message_type=message_type, text=""))

    assert runtime.calls == []


def test_inbound_download_and_parse_paths_are_retired():
    """The downloading parser is deleted, not merely bypassed.

    Keeping it would leave a rewireable path back to `[local path] /tmp/...`
    text and base64 image blobs that ADR 0059 moves into the Attachment Store.
    """

    channel = _channel()

    for retired in (
        "_parse_message",
        "_download_image_as_b64",
        "_download_file_to_tmp",
        "_extract_post_text",
    ):
        assert not hasattr(channel, retired), f"{retired} is a retired inbound path"


def test_local_path_side_channel_cannot_enter_a_turn():
    """A `file` message would embed "[local path] /tmp/..." in the legacy parser."""

    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(message_type="file", text="report.pdf"))

    assert runtime.calls == []


def test_empty_text_is_not_submitted():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(text="   "))

    assert runtime.calls == []


def test_control_rejection_does_not_open_a_second_send_path(caplog):
    channel = _channel()
    runtime = _RecordingControlRuntime(
        acceptance_status=TurnAcceptanceStatus.REJECTED,
        acceptance_code="delivery_backpressure",
    )
    channel._control_runtime = runtime

    with caplog.at_level("WARNING"):
        _drive(channel, _event())

    assert len(runtime.calls) == 1
    assert "delivery_backpressure" in caplog.text
