"""One Backend process owns exactly one control plane (ADR 0054/0060).

`control.db` takes an exclusive lifetime lock, so a per-Channel ControlRuntime
caps the process at one authoritative Channel. These tests pin the shared
composition root: several Channel Adapters share one repository, Sequencer,
Transcript Store and Delivery Pump, without any of them being able to claim
another's Reply Target sequence or read another's Owner scope.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omicsclaw.control import (
    ChannelSurfaceBinding,
    ControlRuntime,
    ControlRuntimePorts,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
)
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Final


class _RecordingAdapter:
    """Single-attempt adapter that records what it was asked to send."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    async def __call__(self, request) -> DeliveryAdapterResult:
        self.sent.append(request.text)
        return DeliveryAdapterResult(
            outcome=DeliveryAttemptOutcome.ACCEPTED,
            provider_evidence={"message_id": f"{self.name}-{len(self.sent)}"},
        )


def _raw(adapter: str, *, request_id: str, subject: str, destination: str, text: str):
    reply_target = {
        "schema_version": 1,
        "kind": "channel",
        "adapter": adapter,
        "account_namespace": f"{adapter}-account",
        "destination_id": destination,
    }
    return RawInboundV1(
        schema_version=1,
        surface="channel",
        source_namespace=f"channel/{adapter}/v1/{adapter}-account",
        source_request_id=request_id,
        external_subject={"kind": f"{adapter}_user", "value": subject},
        reply_target=reply_target,
        content=(RawContentBlockV1(kind="text", text=text),),
    )


def _bindings(telegram_adapter, feishu_adapter):
    return (
        ChannelSurfaceBinding(
            adapter="telegram",
            account_namespace="telegram-account",
            owner_identities={
                "channel/telegram/telegram-account/telegram_user": frozenset({"7"})
            },
            delivery_adapter=telegram_adapter,
            attachment_input_enabled=True,
        ),
        ChannelSurfaceBinding(
            adapter="feishu",
            account_namespace="feishu-account",
            owner_identities={
                "channel/feishu/feishu-account/feishu_user": frozenset({"ou_owner"})
            },
            delivery_adapter=feishu_adapter,
            attachment_input_enabled=False,
        ),
    )


async def _drain(adapter: _RecordingAdapter, *, expected: int) -> None:
    for _ in range(200):
        if len(adapter.sent) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} sends, saw {len(adapter.sent)}")


@pytest.mark.asyncio
async def test_two_channel_adapters_share_one_runtime_and_each_gets_its_reply(tmp_path):
    async def dispatch_events(envelope: MessageEnvelope):
        # Echo the inbound text so each reply is attributable to its Turn.
        yield Final(f"answer to {envelope.content}")

    telegram, feishu = _RecordingAdapter("tg"), _RecordingAdapter("fs")
    runtime = ControlRuntime.for_channel_surfaces(
        state_root=tmp_path,
        workspace_id="workspace-test",
        bindings=_bindings(telegram, feishu),
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        tg = await runtime.submit_and_wait(
            _raw(
                "telegram",
                request_id="tg-1",
                subject="7",
                destination="-100",
                text="from telegram",
            ),
            ControlRuntimePorts(user_id="7"),
        )
        fs = await runtime.submit_and_wait(
            _raw(
                "feishu",
                request_id="fs-1",
                subject="ou_owner",
                destination="oc_chat",
                text="from feishu",
            ),
            ControlRuntimePorts(user_id="ou_owner"),
        )
        assert tg.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert fs.acceptance.status is TurnAcceptanceStatus.ACCEPTED

        await _drain(telegram, expected=1)
        await _drain(feishu, expected=1)

        # Each Adapter delivered only its own Turn's reply.
        assert len(telegram.sent) == 1 and len(feishu.sent) == 1
        assert "from telegram" in telegram.sent[0]
        assert "from feishu" in feishu.sent[0]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_one_channels_owner_is_not_an_owner_of_the_other(tmp_path):
    """Merging Owner scopes must not merge Owner authority across Adapters."""

    async def dispatch_events(_envelope: MessageEnvelope):  # pragma: no cover
        raise AssertionError("a non-Owner must never reach the Agent")
        yield

    telegram, feishu = _RecordingAdapter("tg"), _RecordingAdapter("fs")
    runtime = ControlRuntime.for_channel_surfaces(
        state_root=tmp_path,
        workspace_id="workspace-test",
        bindings=_bindings(telegram, feishu),
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        # The Feishu Owner's subject value presented on the Telegram Adapter.
        result = await runtime.submit_and_wait(
            _raw(
                "telegram",
                request_id="tg-x",
                subject="ou_owner",
                destination="-100",
                text="cross-adapter identity",
            ),
            ControlRuntimePorts(user_id="ou_owner"),
        )

        assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert result.acceptance.code == "owner_denied"
        assert telegram.sent == [] and feishu.sent == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_attachment_input_is_enabled_per_adapter_not_process_wide(tmp_path):
    """Enabling attachments for one Adapter must not open bytes for another.

    Telegram has an Attachment Store cutover; Feishu does not. A shared runtime
    must keep that distinction rather than OR-ing one global switch.
    """

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("ok")

    telegram, feishu = _RecordingAdapter("tg"), _RecordingAdapter("fs")
    runtime = ControlRuntime.for_channel_surfaces(
        state_root=tmp_path,
        workspace_id="workspace-test",
        bindings=_bindings(telegram, feishu),
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        config = runtime._normalizer._config
        assert config.attachment_input_enabled is True
        assert config.attachment_input_adapters == frozenset({"telegram"})

        raw = _raw(
            "feishu",
            request_id="fs-att",
            subject="ou_owner",
            destination="oc_chat",
            text="with bytes",
        )
        from omicsclaw.attachments import SourceAttachmentDescriptorV1

        descriptor = SourceAttachmentDescriptorV1(
            schema_version=1,
            ordinal=0,
            source_attachment_id="photo-1",
            display_name="image.png",
            declared_media_type="image/png",
            declared_size=4,
            declared_sha256="0" * 64,
        )
        feishu_with_attachment = RawInboundV1(
            schema_version=1,
            surface=raw.surface,
            source_namespace=raw.source_namespace,
            source_request_id=raw.source_request_id,
            external_subject=dict(raw.external_subject),
            reply_target=dict(raw.reply_target),
            content=raw.content,
            attachments=(descriptor,),
        )

        class _Source:
            async def open(self, _source_attachment_id):  # pragma: no cover
                raise AssertionError("Feishu bytes must not be opened")
                yield b""

        result = await runtime.submit_and_wait(
            feishu_with_attachment,
            ControlRuntimePorts(user_id="ou_owner"),
            attachment_source=_Source(),
        )

        assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert result.acceptance.code == "attachments_not_supported"
    finally:
        await runtime.close()


def test_duplicate_adapter_account_binding_is_rejected(tmp_path):
    adapter = _RecordingAdapter("dup")
    binding = ChannelSurfaceBinding(
        adapter="feishu",
        account_namespace="acct",
        owner_identities={"channel/feishu/acct/feishu_user": frozenset({"ou"})},
        delivery_adapter=adapter,
    )
    with pytest.raises(ValueError, match="duplicate Channel Surface binding"):
        ControlRuntime.for_channel_surfaces(
            state_root=tmp_path,
            workspace_id="workspace-test",
            bindings=(binding, binding),
        )


def test_at_least_one_binding_is_required(tmp_path):
    with pytest.raises(ValueError, match="at least one Channel Surface binding"):
        ControlRuntime.for_channel_surfaces(
            state_root=tmp_path,
            workspace_id="workspace-test",
            bindings=(),
        )


def test_binding_requires_an_owner_scope_for_its_own_account():
    with pytest.raises(ValueError, match="Owner Identity scope"):
        ChannelSurfaceBinding(
            adapter="feishu",
            account_namespace="acct",
            # Scope belongs to a DIFFERENT account.
            owner_identities={"channel/feishu/other/feishu_user": frozenset({"ou"})},
            delivery_adapter=_RecordingAdapter("x"),
        )


@pytest.mark.asyncio
async def test_channels_adopt_the_shared_runtime_without_composing_their_own(tmp_path):
    """The runner injects; a Channel must never build a second control plane."""

    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig
    from omicsclaw.surfaces.channels.telegram import TelegramChannel, TelegramConfig

    telegram = TelegramChannel(TelegramConfig(bot_token="t", admin_chat_id=7))
    feishu = FeishuChannel(
        FeishuConfig(app_id="cli_a", app_secret="s", allowed_senders={"ou_owner"})
    )
    shared = SimpleNamespace(name="shared-runtime")
    loop = asyncio.get_running_loop()

    for channel in (telegram, feishu):
        assert channel._control_runtime is None
        channel.bind_control_runtime(shared, loop=loop)
        assert channel._control_runtime is shared
        assert channel._control_loop is loop

    # Both refuse to start until a runtime is bound, and neither builds one.
    telegram._control_runtime = None
    with pytest.raises(RuntimeError, match="shared ControlRuntime"):
        await telegram.start()
    feishu._control_runtime = None
    with pytest.raises(RuntimeError, match="shared ControlRuntime"):
        await feishu.start()


@pytest.mark.asyncio
async def test_failed_runtime_start_releases_channels_and_the_control_db(tmp_path):
    """Composition is all-or-nothing.

    `control.db` takes an exclusive lifetime lock, so a runtime left open after
    a failed start makes the control plane unacquirable -- by a retry in this
    process or by any other Backend. The prepared Channels must be released too.
    """

    from omicsclaw.surfaces.channels import __main__ as runner

    stopped: list[str] = []
    closed: list[str] = []

    class _Channel:
        def __init__(self, name: str) -> None:
            self.name = name

        async def prepare_control_binding(self):
            return SimpleNamespace(adapter=self.name)

        async def stop(self):
            stopped.append(self.name)

        def bind_control_runtime(self, runtime, *, loop=None):
            raise AssertionError("no Channel may be bound to a runtime that failed")

    class _Runtime:
        async def start(self):
            raise RuntimeError("control.db is owned by another process")

        async def close(self):
            closed.append("runtime")

    manager = SimpleNamespace(
        channels={"telegram": _Channel("telegram"), "feishu": _Channel("feishu")}
    )
    original = runner.ControlRuntime if hasattr(runner, "ControlRuntime") else None
    import omicsclaw.control as control_module

    real_for_channel_surfaces = control_module.ControlRuntime.for_channel_surfaces
    control_module.ControlRuntime.for_channel_surfaces = staticmethod(
        lambda **_kwargs: _Runtime()
    )
    try:
        with pytest.raises(RuntimeError, match="owned by another process"):
            await runner._compose_shared_control_runtime(manager)
    finally:
        control_module.ControlRuntime.for_channel_surfaces = real_for_channel_surfaces
        if original is not None:
            runner.ControlRuntime = original

    assert closed == ["runtime"]
    assert sorted(stopped) == ["feishu", "telegram"]


@pytest.mark.asyncio
async def test_runner_does_not_close_runtime_under_a_channel_that_failed_to_stop():
    from omicsclaw.surfaces.channels import __main__ as runner

    class _Manager:
        async def stop_all(self):
            raise RuntimeError("sanitized channel shutdown failure")

    runtime = SimpleNamespace(close=AsyncMock())

    with pytest.raises(RuntimeError, match="channel shutdown failure"):
        await runner._stop_channels_then_close_runtime(_Manager(), runtime)

    runtime.close.assert_not_awaited()
