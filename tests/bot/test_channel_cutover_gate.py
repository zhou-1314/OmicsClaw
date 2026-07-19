from __future__ import annotations

import pytest

from omicsclaw.surfaces.channels.__main__ import (
    _require_authoritative_channels,
    _require_started_channels,
)


def test_telegram_is_the_only_authoritative_channel_cutover() -> None:
    _require_authoritative_channels(["telegram"])


@pytest.mark.parametrize("channel", ["feishu", "slack", "discord", "email"])
def test_legacy_direct_dispatch_channels_fail_closed(channel: str) -> None:
    with pytest.raises(RuntimeError, match="persistent Delivery Adapter"):
        _require_authoritative_channels([channel])


def test_mixed_channel_start_fails_as_one_unit() -> None:
    with pytest.raises(RuntimeError, match="feishu"):
        _require_authoritative_channels(["telegram", "feishu"])


def test_requested_channel_must_really_be_running() -> None:
    with pytest.raises(RuntimeError, match="telegram"):
        _require_started_channels(["telegram"], [])


def test_requested_running_channel_passes_startup_barrier() -> None:
    _require_started_channels(["telegram"], ["telegram"])


@pytest.mark.asyncio
async def test_programmatic_legacy_channel_start_is_also_gated() -> None:
    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig

    channel = FeishuChannel(FeishuConfig(app_id="app", app_secret="secret"))

    with pytest.raises(RuntimeError, match="ControlRuntime"):
        await channel.start()


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


def test_telegram_declares_authoritative_ingress() -> None:
    from omicsclaw.surfaces.channels.telegram import TelegramChannel, TelegramConfig

    channel = TelegramChannel(TelegramConfig(bot_token="token", admin_chat_id=7))

    channel.require_authoritative_ingress()
