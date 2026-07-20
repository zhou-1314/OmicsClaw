"""Feishu authoritative ingress cutover (ADR 0060/0063, text-only slice).

Feishu events arrive on the lark WebSocket thread and are bridged into the
Channel's background loop.  These tests pin the normalization contract and
prove the handler produces no reply of its own: every terminal reply must
leave through the persistent Delivery Outbox.
"""

from __future__ import annotations

import asyncio
import threading
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
    text: object = "run a spatial preprocess",
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


def _install_fake_lark(monkeypatch):
    """Install an SDK-shaped lark module whose Client owns a blocking start()."""

    import sys
    from types import ModuleType, SimpleNamespace as NS

    client_mod = ModuleType("lark_oapi.ws.client")
    client_mod.loop = None
    ws_mod = ModuleType("lark_oapi.ws")
    ws_mod.client = client_mod
    ws_mod.Client = None
    fake = ModuleType("lark_oapi")
    fake.LogLevel = NS(DEBUG=1, INFO=2)
    fake.ws = ws_mod
    fake.EventDispatcherHandler = NS(
        builder=lambda *_a: NS(
            register_p2_im_message_receive_v1=lambda *_a: NS(build=lambda: object())
        )
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", ws_mod)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", client_mod)
    return client_mod, ws_mod


def _sdk_client_type(
    client_mod,
    *,
    connect=None,
    disconnect=None,
    reconnect_on_connect_error=False,
):
    """Build the lifecycle shape exposed by lark-oapi 1.7.1."""

    class _Client:
        instances = []

        def __init__(self, *_args, **_kwargs):
            self._conn = None
            self.force_exit = threading.Event()
            self.reconnect_exit = threading.Event()
            self.reconnect_caught = threading.Event()
            self.reconnect_attempts = 0
            self.__class__.instances.append(self)

        async def _connect(self):
            if connect is not None:
                await connect(self)
            else:
                self._conn = object()

        async def _disconnect(self):
            if disconnect is not None:
                await disconnect(self)
            self._conn = None
            self.force_exit.set()

        async def _select(self):
            while not self.force_exit.is_set():
                await asyncio.sleep(0.005)

        def start(self):
            while not self.reconnect_exit.is_set():
                try:
                    client_mod.loop.run_until_complete(self._connect())
                    break
                except Exception:
                    if not reconnect_on_connect_error:
                        raise
                    self.reconnect_attempts += 1
                    self.reconnect_caught.set()
                    client_mod.loop.run_until_complete(asyncio.sleep(0.005))
            else:
                return
            client_mod.loop.run_until_complete(self._select())

    return _Client


@pytest.mark.asyncio
async def test_submit_control_inbound_builds_stable_raw_inbound():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    channel._running = True
    channel.activate_ingress()

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
    assert dict(raw.transport_facts) == {"provider_event_kind": "im.message.receive_v1"}
    assert ports.user_id == "ou_owner"


def _drive(channel: FeishuChannel, event, *, activate: bool = True) -> None:
    """Run `_handle_event` with a real background loop, as `start()` would."""

    if activate and not channel.ingress_active:
        channel._running = True
        channel.activate_ingress()
    loop = asyncio.new_event_loop()
    import threading

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    channel._control_loop = loop
    try:
        channel._handle_event(event)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_pre_activation_event_creates_no_turn():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    _drive(channel, _event(text="too early"), activate=False)

    assert runtime.calls == []


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


@pytest.mark.parametrize("chat_type", [None, "", "future_group"])
def test_unknown_chat_types_create_no_turn_or_rate_limit_work(chat_type):
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    duplicate_checks: list[str] = []
    parse_calls: list[str] = []
    rate_limit_calls: list[str] = []
    channel._is_duplicate_feishu = lambda message_id: (
        duplicate_checks.append(message_id) or False
    )
    channel._extract_owner_text = lambda content: parse_calls.append(content) or "text"
    channel.check_rate_limit = lambda sender_id: (
        rate_limit_calls.append(sender_id) or True
    )

    _drive(channel, _event(chat_type=chat_type))

    assert duplicate_checks == []
    assert parse_calls == []
    assert rate_limit_calls == []
    assert runtime.calls == []


@pytest.mark.parametrize("text", [None, [], {}], ids=["null", "array", "object"])
def test_non_string_text_creates_no_turn_or_rate_limit_work(text):
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    rate_limit_calls: list[str] = []
    channel.check_rate_limit = lambda sender_id: (
        rate_limit_calls.append(sender_id) or True
    )

    _drive(channel, _event(text=text))

    assert rate_limit_calls == []
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


def test_rejected_message_types_do_not_consume_the_owner_rate_limit():
    """Rate budget is spent on Turns, not on messages that fail the gates.

    An image, or group chatter that never mentions this Bot, must not be able
    to exhaust the Owner's hourly budget and starve their real requests.
    """

    channel = FeishuChannel(
        FeishuConfig(
            app_id="cli_app_1",
            app_secret="secret",
            allowed_senders={"ou_owner"},
            bot_open_id="ou_bot",
            rate_limit_per_hour=1,
        )
    )
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime

    # Neither of these may become a Turn, so neither may spend the one unit.
    _drive(channel, _event(message_type="image", message_id="om_img"))
    _drive(
        channel,
        _event(
            chat_type="group",
            mentions=[_mention("ou_someone_else")],
            message_id="om_group",
        ),
    )
    assert runtime.calls == []

    # The Owner's first real text still gets through.
    _drive(channel, _event(message_id="om_real"))

    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_start_reports_ready_only_after_connect_returns_with_connection(
    monkeypatch,
):
    connect_entered = threading.Event()
    allow_connect = threading.Event()

    async def _connect(client):
        connect_entered.set()
        while not allow_connect.is_set():
            await asyncio.sleep(0.005)
        client._conn = object()

    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(client_mod, connect=_connect)
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 1.0
    channel._control_runtime = _RecordingControlRuntime()
    channel._lark_client = object()
    start_task = asyncio.create_task(channel.start())

    try:
        assert await asyncio.to_thread(connect_entered.wait, 1.0)
        assert channel._ws_ready.is_set() is False
        assert channel._running is False
        assert start_task.done() is False

        allow_connect.set()
        await asyncio.wait_for(start_task, timeout=1.0)

        assert channel._ws_ready.is_set() is True
        assert channel._running is True
        await channel.stop()
    finally:
        allow_connect.set()
        for client in client_type.instances:
            client.force_exit.set()
        if not start_task.done():
            await asyncio.wait_for(
                asyncio.gather(start_task, return_exceptions=True),
                timeout=2.0,
            )
        thread = channel._ws_thread
        if thread is not None:
            thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_start_timeout_rolls_back_a_delayed_connection(monkeypatch):
    connect_entered = threading.Event()
    release_connect = threading.Event()

    async def _connect(client):
        connect_entered.set()
        while not release_connect.is_set():
            await asyncio.sleep(0.005)
        client._conn = object()

    async def _disconnect(_client):
        release_connect.set()

    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(
        client_mod,
        connect=_connect,
        disconnect=_disconnect,
    )
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 0.2
    channel.feishu_config.ws_join_seconds = 1.0
    channel._control_runtime = _RecordingControlRuntime()
    channel._lark_client = object()

    try:
        with pytest.raises(RuntimeError, match="failed to become ready"):
            await channel.start()

        assert connect_entered.is_set()
        assert channel._running is False
        assert channel._ws_client is None
        assert channel._ws_thread is None
        assert channel._ws_loop is None
    finally:
        release_connect.set()
        for client in client_type.instances:
            client.force_exit.set()
        thread = channel._ws_thread
        if thread is not None:
            thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_start_timeout_never_signals_ready_after_stopping_connect(
    monkeypatch,
):
    lifecycle_lock = asyncio.Lock()
    connect_entered = threading.Event()
    release_connect = threading.Event()
    connect_returned = threading.Event()
    disconnect_entered = threading.Event()
    disconnect_completed = threading.Event()

    async def _connect(client):
        async with lifecycle_lock:
            connect_entered.set()
            while not release_connect.is_set():
                await asyncio.sleep(0.005)
            client._conn = object()
            connect_returned.set()

    async def _disconnect(_client):
        disconnect_entered.set()
        async with lifecycle_lock:
            disconnect_completed.set()

    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(
        client_mod,
        connect=_connect,
        disconnect=_disconnect,
        reconnect_on_connect_error=True,
    )
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 0.02
    channel.feishu_config.ws_join_seconds = 0.02
    channel._control_runtime = _RecordingControlRuntime()
    channel._lark_client = object()

    try:
        with pytest.raises(RuntimeError, match="failed to become ready"):
            await channel.start()

        client = client_type.instances[0]
        thread = channel._ws_thread
        assert connect_entered.is_set()
        assert disconnect_entered.is_set()
        assert channel._ws_stopping.is_set()
        assert channel._ws_ready.is_set() is False
        assert channel._ws_client is client
        assert thread is not None
        assert thread.is_alive()

        release_connect.set()
        assert await asyncio.to_thread(connect_returned.wait, 1.0)
        assert await asyncio.to_thread(disconnect_completed.wait, 1.0)
        await asyncio.sleep(0.05)

        assert client.reconnect_caught.is_set() is False
        assert client.reconnect_attempts == 0
        assert await asyncio.to_thread(channel._ws_exit.wait, 1.0)

        assert channel._ws_ready.is_set() is False
        assert client._conn is None
        assert channel._ws_client is client
        assert channel._ws_thread is thread

        await channel.stop()
        assert channel._ws_client is None
        assert channel._ws_thread is None
        assert channel._ws_loop is None
    finally:
        release_connect.set()
        for client in client_type.instances:
            client.force_exit.set()
            client.reconnect_exit.set()
        thread = channel._ws_thread
        if thread is not None:
            thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_cancelled_start_rolls_back_before_propagating_cancellation(
    monkeypatch,
):
    lifecycle_lock = asyncio.Lock()
    connect_entered = threading.Event()
    release_connect = threading.Event()
    disconnect_entered = threading.Event()

    async def _connect(client):
        async with lifecycle_lock:
            connect_entered.set()
            while not release_connect.is_set():
                await asyncio.sleep(0.005)
            client._conn = object()

    async def _disconnect(_client):
        disconnect_entered.set()
        release_connect.set()
        async with lifecycle_lock:
            return None

    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(
        client_mod,
        connect=_connect,
        disconnect=_disconnect,
    )
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 1.0
    channel.feishu_config.ws_join_seconds = 1.0
    channel._control_runtime = _RecordingControlRuntime()
    channel._lark_client = object()
    start_task = asyncio.create_task(channel.start())

    try:
        assert await asyncio.to_thread(connect_entered.wait, 1.0)
        start_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start_task, timeout=2.0)

        assert disconnect_entered.is_set()
        assert channel._ws_exit.is_set()
        assert channel._ws_ready.is_set() is False
        assert channel._ws_client is None
        assert channel._ws_thread is None
        assert channel._ws_loop is None
        assert channel._running is False
    finally:
        release_connect.set()
        for client in client_type.instances:
            client.force_exit.set()
        thread = channel._ws_thread
        if thread is not None:
            thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_cancelled_start_retains_ownership_when_rollback_cannot_run(
    monkeypatch,
):
    connect_entered = threading.Event()
    release_connect = threading.Event()

    async def _connect(client):
        connect_entered.set()
        release_connect.wait(2.0)
        client._conn = object()

    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(client_mod, connect=_connect)
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 1.0
    channel.feishu_config.ws_join_seconds = 0.02
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    channel._lark_client = object()
    start_task = asyncio.create_task(channel.start())

    try:
        assert await asyncio.to_thread(connect_entered.wait, 1.0)
        start_task.cancel()

        with pytest.raises(RuntimeError, match="shutdown failed"):
            await asyncio.wait_for(start_task, timeout=1.0)

        client = client_type.instances[0]
        thread = channel._ws_thread
        assert channel._ws_stopping.is_set()
        assert channel._ws_client is client
        assert thread is not None
        assert thread.is_alive()
        assert channel._ws_loop is not None
        assert channel._control_runtime is runtime
        assert channel._running is False
    finally:
        release_connect.set()
        for client in client_type.instances:
            client.force_exit.set()
        thread = channel._ws_thread
        if thread is not None:
            thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_start_rolls_back_when_connect_fails(monkeypatch):
    async def _connect(_client):
        await asyncio.sleep(0.01)
        raise RuntimeError("secret credential detail")

    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(client_mod, connect=_connect)
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 1.0
    channel._control_runtime = _RecordingControlRuntime()
    channel._lark_client = object()

    with pytest.raises(RuntimeError, match="failed to become ready") as raised:
        await channel.start()

    assert "secret credential detail" not in str(raised.value)
    assert channel._running is False
    assert channel._ws_client is None
    assert channel._ws_thread is None
    assert channel._ws_loop is None


@pytest.mark.asyncio
async def test_start_waits_for_listener_exit_after_delayed_connect_failure(
    monkeypatch,
):
    teardown_entered = threading.Event()
    allow_listener_exit = threading.Event()
    real_all_tasks = asyncio.all_tasks

    def _pause_ws_teardown(loop=None):
        if threading.current_thread().name == "feishu-ws":
            teardown_entered.set()
            allow_listener_exit.wait(2.0)
        return real_all_tasks(loop)

    async def _connect(_client):
        await asyncio.sleep(0.01)
        raise RuntimeError("delayed connection failure")

    monkeypatch.setattr(asyncio, "all_tasks", _pause_ws_teardown)
    client_mod, ws_mod = _install_fake_lark(monkeypatch)
    client_type = _sdk_client_type(client_mod, connect=_connect)
    ws_mod.Client = client_type
    channel = _channel()
    channel.feishu_config.ws_start_probe_seconds = 1.0
    channel.feishu_config.ws_join_seconds = 0.02
    channel._control_runtime = _RecordingControlRuntime()
    channel._lark_client = object()
    start_task = asyncio.create_task(channel.start())

    try:
        assert await asyncio.to_thread(teardown_entered.wait, 1.0)
        assert channel._ws_error is not None
        assert channel._ws_exit.is_set() is False

        await asyncio.sleep(0.05)

        assert start_task.done() is False
        assert channel._ws_client is not None
        assert channel._ws_thread is not None
        assert channel._ws_loop is not None

        allow_listener_exit.set()
        with pytest.raises(RuntimeError, match="failed to become ready"):
            await asyncio.wait_for(start_task, timeout=1.0)

        assert channel._ws_exit.is_set() is True
        assert channel._ws_client is None
        assert channel._ws_thread is None
        assert channel._ws_loop is None
    finally:
        allow_listener_exit.set()
        await asyncio.wait_for(
            asyncio.gather(start_task, return_exceptions=True),
            timeout=2.0,
        )
        thread = channel._ws_thread
        if thread is not None:
            thread.join(timeout=2.0)


def _start_owned_loop(channel, *, after_stop=None):
    loop_ready = threading.Event()

    def _run():
        asyncio.set_event_loop(channel._ws_loop)
        loop_ready.set()
        channel._ws_loop.run_forever()
        if after_stop is not None:
            after_stop()
        channel._ws_loop.close()

    thread = threading.Thread(target=_run, daemon=True, name="test-feishu-ws")
    channel._ws_thread = thread
    thread.start()
    assert loop_ready.wait(1.0)
    return thread


@pytest.mark.asyncio
async def test_stop_executes_async_disconnect_on_owning_loop_and_joins():
    channel = _channel()
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    channel._running = True
    channel.activate_ingress()
    owner_loop = asyncio.new_event_loop()
    channel._ws_loop = owner_loop
    disconnect_call = {}

    class _Client:
        async def _disconnect(self):
            disconnect_call["loop"] = asyncio.get_running_loop()
            disconnect_call["thread"] = threading.current_thread()

    client = _Client()
    channel._ws_client = client
    channel._ws_ready.set()
    thread = _start_owned_loop(channel)

    await channel.stop()

    assert disconnect_call == {"loop": owner_loop, "thread": thread}
    assert not thread.is_alive()
    assert channel._ws_client is None
    assert channel._ws_thread is None
    assert channel._ws_loop is None
    assert channel._ws_ready.is_set() is False
    assert channel._control_runtime is None
    assert channel._running is False


@pytest.mark.asyncio
async def test_stop_timeout_retains_live_websocket_and_runtime_ownership():
    channel = _channel()
    channel.feishu_config.ws_join_seconds = 0.01
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    channel._running = True
    channel.activate_ingress()
    channel._ws_loop = asyncio.new_event_loop()
    release_thread = threading.Event()

    class _Client:
        async def _disconnect(self):
            return None

    client = _Client()
    channel._ws_client = client
    thread = _start_owned_loop(channel, after_stop=release_thread.wait)

    try:
        with pytest.raises(
            RuntimeError,
            match=r"Feishu channel shutdown failed \(RuntimeError\)",
        ):
            await channel.stop()

        assert channel.ingress_active is False
        assert channel._running is True
        assert channel._ws_client is client
        assert channel._ws_thread is thread
        assert channel._ws_loop is not None
        assert channel._control_runtime is runtime
    finally:
        release_thread.set()
        thread.join(timeout=2.0)
