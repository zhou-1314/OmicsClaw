from __future__ import annotations

import asyncio

import pytest

from omicsclaw.surfaces.channels.base import Channel
from omicsclaw.surfaces.channels.__main__ import (
    _require_authoritative_channels,
    _require_started_channels,
)


class _LifecycleChannel(Channel):
    authoritative_ingress = True

    def __init__(
        self,
        name: str,
        *,
        start_release: asyncio.Event | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.started = asyncio.Event()
        self._start_release = start_release
        self._stop_error = stop_error

    async def start(self) -> None:
        self._running = True
        self.started.set()
        if self._start_release is not None:
            await self._start_release.wait()

    async def stop(self) -> None:
        self.deactivate_ingress()
        if self._stop_error is not None:
            raise self._stop_error
        self._running = False

    async def _send_chunk(self, chat_id, formatted_text, raw_text, metadata) -> None:
        raise AssertionError("not used")


@pytest.mark.parametrize("channel", ["telegram", "feishu"])
def test_cutover_channels_pass_the_authoritative_gate(channel: str) -> None:
    _require_authoritative_channels([channel])


def test_cutover_channels_may_now_share_one_process() -> None:
    """The runner composes ONE ControlRuntime from every Channel's binding,
    so `control.db`'s exclusive lifetime lock is taken once rather than once
    per Channel."""

    _require_authoritative_channels(["telegram", "feishu"])


def test_the_same_channel_cannot_be_requested_twice() -> None:
    with pytest.raises(RuntimeError, match="more than once"):
        _require_authoritative_channels(["feishu", "feishu"])


@pytest.mark.parametrize("channel", ["slack", "discord", "email", "qq"])
def test_legacy_direct_dispatch_channels_fail_closed(channel: str) -> None:
    with pytest.raises(RuntimeError, match="persistent Delivery Adapter"):
        _require_authoritative_channels([channel])


def test_mixed_channel_start_fails_as_one_unit() -> None:
    with pytest.raises(RuntimeError, match="slack"):
        _require_authoritative_channels(["telegram", "slack"])


def test_requested_channel_must_really_be_running() -> None:
    with pytest.raises(RuntimeError, match="telegram"):
        _require_started_channels(["telegram"], [])


def test_requested_running_channel_passes_startup_barrier() -> None:
    _require_started_channels(["telegram"], ["telegram"])


@pytest.mark.asyncio
async def test_programmatic_legacy_channel_start_is_also_gated() -> None:
    from omicsclaw.surfaces.channels.slack import SlackChannel, SlackConfig

    channel = SlackChannel(SlackConfig(bot_token="token", app_token="app"))

    with pytest.raises(RuntimeError, match="ControlRuntime"):
        await channel.start()


@pytest.mark.asyncio
async def test_feishu_without_configured_owner_fails_closed() -> None:
    """The cutover must not admit a Feishu App with no Owner identity."""

    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig

    channel = FeishuChannel(FeishuConfig(app_id="app", app_secret="secret"))

    with pytest.raises(RuntimeError, match="FEISHU_ALLOWED_SENDERS"):
        await channel.prepare_control_binding()


@pytest.mark.asyncio
@pytest.mark.parametrize("bot_open_id", ["", "   "])
async def test_feishu_without_configured_bot_identity_fails_before_provider_work(
    monkeypatch,
    bot_open_id: str,
) -> None:
    """Authoritative startup requires proof of this Bot's group identity."""

    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig

    channel = FeishuChannel(
        FeishuConfig(
            app_id="app",
            app_secret="secret",
            allowed_senders={"ou_owner"},
            bot_open_id=bot_open_id,
        )
    )

    def provider_work_must_not_start():
        raise AssertionError("provider client construction must not run")

    monkeypatch.setattr(channel, "_build_lark_client", provider_work_must_not_start)

    with pytest.raises(RuntimeError, match="FEISHU_BOT_OPEN_ID"):
        await channel.prepare_control_binding()

    assert channel._lark_client is None
    assert channel._ws_client is None
    assert channel._ws_thread is None


@pytest.mark.asyncio
async def test_manager_propagates_sanitized_zero_channel_startup_failure(
    monkeypatch,
    caplog,
) -> None:
    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig
    from omicsclaw.surfaces.channels.manager import ChannelManager

    channel = FeishuChannel(FeishuConfig(app_id="app", app_secret="secret"))

    def reject_legacy_start() -> None:
        raise RuntimeError("secret-startup-detail")

    monkeypatch.setattr(
        channel,
        "require_authoritative_ingress",
        reject_legacy_start,
    )
    manager = ChannelManager()
    manager.register(channel)

    with pytest.raises(RuntimeError, match="Channel startup failed for: feishu"):
        await manager.start_all()

    health = manager.get_health()
    assert health["channels"]["running"] == []
    assert health["channel_health"]["feishu"]["last_error"] == "RuntimeError"
    assert "secret-startup-detail" not in caplog.text


@pytest.mark.asyncio
async def test_manager_activates_ingress_only_after_every_transport_starts() -> None:
    from omicsclaw.surfaces.channels.manager import ChannelManager

    release_second_start = asyncio.Event()
    telegram = _LifecycleChannel("telegram")
    feishu = _LifecycleChannel("feishu", start_release=release_second_start)
    manager = ChannelManager()
    manager.register(telegram)
    manager.register(feishu)

    start_task = asyncio.create_task(manager.start_all())
    await telegram.started.wait()
    await feishu.started.wait()

    assert telegram.ingress_active is False
    assert feishu.ingress_active is False

    release_second_start.set()
    await start_task

    assert telegram.ingress_active is True
    assert feishu.ingress_active is True


@pytest.mark.asyncio
async def test_manager_surfaces_sanitized_stop_failure_and_keeps_channel_running(
    caplog,
) -> None:
    from omicsclaw.surfaces.channels.manager import ChannelManager

    channel = _LifecycleChannel(
        "telegram",
        stop_error=RuntimeError("secret-provider-stop-detail"),
    )
    manager = ChannelManager()
    manager.register(channel)
    await manager.start_all()

    with pytest.raises(
        RuntimeError,
        match=r"Channel shutdown failed for: telegram \(RuntimeError\)",
    ):
        await manager.stop_all()

    assert channel.ingress_active is False
    assert manager.running_channels() == ["telegram"]
    assert manager.get_health()["channel_health"]["telegram"]["last_error"] == (
        "RuntimeError"
    )
    assert "secret-provider-stop-detail" not in caplog.text


@pytest.mark.asyncio
async def test_health_server_duplicate_start_fails_closed() -> None:
    from omicsclaw.surfaces.channels.manager import ChannelManager

    manager = ChannelManager()
    await manager.start_health_server(port=0)
    try:
        with pytest.raises(RuntimeError, match="Health server already started"):
            await manager.start_health_server(port=0)
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_health_stop_waits_for_inflight_start_and_releases_listener(
    monkeypatch,
) -> None:
    from omicsclaw.surfaces.channels import manager as manager_module

    real_start_server = asyncio.start_server
    listener_created = asyncio.Event()
    release_start = asyncio.Event()
    listener_port: int | None = None

    async def paused_start_server(*args, **kwargs):
        nonlocal listener_port
        health_server = await real_start_server(*args, **kwargs)
        assert health_server.sockets
        listener_port = health_server.sockets[0].getsockname()[1]
        listener_created.set()
        await release_start.wait()
        return health_server

    monkeypatch.setattr(manager_module.asyncio, "start_server", paused_start_server)
    manager = manager_module.ChannelManager()
    start_task = asyncio.create_task(manager.start_health_server(port=0))
    stop_task: asyncio.Task[None] | None = None
    try:
        await listener_created.wait()
        stop_task = asyncio.create_task(manager.stop_all())
        await asyncio.sleep(0)
        release_start.set()
        await start_task
        await stop_task

        assert manager._health_server is None
        assert listener_port is not None
        rebound = await real_start_server(
            lambda _reader, _writer: None,
            "0.0.0.0",
            listener_port,
        )
        rebound.close()
        await rebound.wait_closed()
    finally:
        release_start.set()
        tasks = [start_task]
        if stop_task is not None:
            tasks.append(stop_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        leaked_server = manager._health_server
        if leaked_server is not None:
            leaked_server.close()
            await leaked_server.wait_closed()
            manager._health_server = None


@pytest.mark.asyncio
async def test_health_server_closes_when_channel_stop_fails_and_port_rebinds() -> None:
    from omicsclaw.surfaces.channels.manager import ChannelManager

    channel = _LifecycleChannel(
        "telegram",
        stop_error=RuntimeError("secret-provider-stop-detail"),
    )
    manager = ChannelManager()
    manager.register(channel)
    await manager.start_all()
    await manager.start_health_server(port=0)
    health_server = manager._health_server
    assert health_server is not None
    assert health_server.sockets
    port = health_server.sockets[0].getsockname()[1]

    with pytest.raises(RuntimeError, match="Channel shutdown failed for: telegram"):
        await manager.stop_all()

    assert manager._health_server is None
    rebound = await asyncio.start_server(lambda _reader, _writer: None, "0.0.0.0", port)
    rebound.close()
    await rebound.wait_closed()

    channel._stop_error = None
    await manager.stop_all()


@pytest.mark.asyncio
async def test_manager_combines_sanitized_channel_and_health_shutdown_failures(
    caplog,
) -> None:
    from omicsclaw.surfaces.channels.manager import ChannelManager

    class FailingHealthServer:
        def __init__(self) -> None:
            self.close_calls = 0
            self.wait_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise RuntimeError("secret-health-stop-detail")

    channel = _LifecycleChannel(
        "telegram",
        stop_error=RuntimeError("secret-provider-stop-detail"),
    )
    health_server = FailingHealthServer()
    manager = ChannelManager()
    manager.register(channel)
    await manager.start_all()
    manager._health_server = health_server  # type: ignore[assignment]

    with pytest.raises(
        RuntimeError,
        match=(
            r"Channel shutdown failed for: telegram \(RuntimeError\); "
            r"Health server shutdown failed \(RuntimeError\)"
        ),
    ):
        await manager.stop_all()

    assert health_server.close_calls == 1
    assert health_server.wait_calls == 1
    assert manager._health_server is health_server
    assert manager.running_channels() == ["telegram"]
    assert "secret-provider-stop-detail" not in caplog.text
    assert "secret-health-stop-detail" not in caplog.text

    channel._stop_error = None
    await manager.stop_all()
    assert health_server.close_calls == 2
    assert health_server.wait_calls == 2
    assert manager._health_server is None


def test_telegram_declares_authoritative_ingress() -> None:
    from omicsclaw.surfaces.channels.telegram import TelegramChannel, TelegramConfig

    channel = TelegramChannel(TelegramConfig(bot_token="token", admin_chat_id=7))

    channel.require_authoritative_ingress()


def test_feishu_declares_authoritative_ingress() -> None:
    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig

    channel = FeishuChannel(FeishuConfig(app_id="app", app_secret="secret"))

    channel.require_authoritative_ingress()


@pytest.mark.asyncio
async def test_feishu_legacy_direct_send_paths_are_retired() -> None:
    """No Feishu reply may bypass the persistent Delivery Outbox."""

    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig

    channel = FeishuChannel(FeishuConfig(app_id="app", app_secret="secret"))

    with pytest.raises(RuntimeError, match="ControlRuntime"):
        await channel.process_message("chat", "hello")
    with pytest.raises(RuntimeError, match="Delivery Outbox"):
        await channel.send("chat", "hello")
    with pytest.raises(RuntimeError, match="Delivery Outbox"):
        await channel._send_chunk("chat", "hello", "hello", {})
    with pytest.raises(RuntimeError, match="durable artifact references"):
        await channel.send_media("chat", "/tmp/figure.png")

    # The legacy internal senders are gone, not merely unused, so they cannot
    # be rewired by a later change.
    for retired in (
        "_send_text_sync",
        "_send_long_text",
        "_send_media_items",
        "_update_text",
        "_process_message_async",
    ):
        assert not hasattr(channel, retired), f"{retired} is a retired direct path"


def test_feishu_env_wiring_requires_configured_owners(monkeypatch) -> None:
    """Owner identity is Backend configuration; ingress must not start without it."""

    from omicsclaw.surfaces.channels.__main__ import _build_feishu_channel

    monkeypatch.setenv("FEISHU_APP_ID", "cli_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.delenv("FEISHU_ALLOWED_SENDERS", raising=False)

    with pytest.raises(RuntimeError, match="FEISHU_ALLOWED_SENDERS"):
        _build_feishu_channel()


@pytest.mark.parametrize("bot_open_id", [None, "", "   "])
def test_feishu_env_wiring_requires_configured_bot_identity(
    monkeypatch,
    bot_open_id: str | None,
) -> None:
    from omicsclaw.surfaces.channels.__main__ import _build_feishu_channel

    monkeypatch.setenv("FEISHU_APP_ID", "cli_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_ALLOWED_SENDERS", "ou_owner")
    if bot_open_id is None:
        monkeypatch.delenv("FEISHU_BOT_OPEN_ID", raising=False)
    else:
        monkeypatch.setenv("FEISHU_BOT_OPEN_ID", bot_open_id)

    with pytest.raises(RuntimeError, match="FEISHU_BOT_OPEN_ID"):
        _build_feishu_channel()


def test_feishu_env_wiring_builds_owner_and_bot_identity(monkeypatch) -> None:
    from omicsclaw.surfaces.channels.__main__ import _build_feishu_channel

    monkeypatch.setenv("FEISHU_APP_ID", "cli_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_ALLOWED_SENDERS", "ou_1, ou_2")
    monkeypatch.setenv("FEISHU_BOT_OPEN_ID", "ou_bot")

    channel = _build_feishu_channel()

    assert channel.config.allowed_senders == {"ou_1", "ou_2"}
    assert channel.feishu_config.bot_open_id == "ou_bot"
    assert channel.authoritative_ingress is True
