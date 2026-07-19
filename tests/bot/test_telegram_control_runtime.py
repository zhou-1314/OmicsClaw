from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimeResult,
    TurnAcceptanceResult,
    TurnAcceptanceStatus,
)
from omicsclaw.surfaces.channels.telegram import TelegramChannel, TelegramConfig


class _RecordingControlRuntime:
    def __init__(
        self,
        *,
        acceptance_status: TurnAcceptanceStatus = TurnAcceptanceStatus.ACCEPTED,
        acceptance_code: str = "",
    ) -> None:
        self.calls: list[tuple[object, object]] = []
        self.attachment_sources: list[object | None] = []
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

    async def submit_and_wait(
        self,
        raw,
        ports,
        *,
        attachment_source=None,
        on_accepted=None,
    ):
        self.calls.append((raw, ports))
        self.attachment_sources.append(attachment_source)
        return self._result


def _update(
    *,
    user_id: int = 7,
    chat_id: int = -100123,
    message_id: int = 42,
    text: str = "hello",
    thread_id: int | None = 99,
    document=None,
    photo=(),
    caption: str | None = None,
    media_group_id: str | None = None,
):
    message = SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        text=text,
        document=document,
        photo=photo,
        caption=caption,
        media_group_id=media_group_id,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="Owner"),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _context():
    bot = SimpleNamespace(
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(
            side_effect=AssertionError("attachment must not be fetched")
        ),
    )
    return SimpleNamespace(bot=bot)


def _photo(*, unique_id: str, file_id: str, file_size: int = 4):
    return SimpleNamespace(
        file_unique_id=unique_id,
        file_id=file_id,
        file_size=file_size,
    )


def _download_context(payload: bytes = b"jpeg"):
    telegram_file = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(payload))
    )
    bot = SimpleNamespace(
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(return_value=telegram_file),
    )
    return SimpleNamespace(bot=bot), telegram_file


@pytest.mark.asyncio
async def test_submit_control_text_builds_stable_raw_inbound_without_direct_final_reply():
    channel = TelegramChannel(
        TelegramConfig(
            bot_token="test-token",
            admin_chat_id=7,
            account_namespace="research",
        )
    )
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    update = _update()

    result = await channel._submit_control_text(update, "frozen user text")

    assert result is runtime._result
    assert len(runtime.calls) == 1
    raw, ports = runtime.calls[0]
    assert raw.schema_version == 1
    assert raw.surface == "channel"
    assert raw.source_namespace == "channel/telegram/v1/research"
    assert raw.source_request_id == "-100123:42"
    assert dict(raw.external_subject) == {
        "kind": "telegram_user",
        "value": "7",
    }
    assert dict(raw.reply_target) == {
        "schema_version": 1,
        "kind": "channel",
        "adapter": "telegram",
        "account_namespace": "research",
        "destination_id": "-100123",
        "thread_id": "99",
    }
    assert tuple((block.kind, block.text) for block in raw.content) == (
        ("text", "frozen user text"),
    )
    assert dict(raw.transport_facts) == {"provider_event_kind": "message"}
    assert ports.user_id == "7"
    assert ports.thread_id == "99"
    assert ports.response_sink is None
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_submits_to_control_runtime_and_never_calls_legacy_dispatch(
    monkeypatch,
):
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    update = _update(text="route me through control")
    context = _context()
    submit = AsyncMock()
    monkeypatch.setattr(channel, "_submit_control_text", submit)
    monkeypatch.setattr(
        "omicsclaw.runtime.agent.state.audit",
        lambda *_args, **_kwargs: None,
    )

    legacy_calls: list[object] = []

    def legacy_dispatch(envelope):
        legacy_calls.append(envelope)
        raise AssertionError("legacy dispatch must not be called by Telegram handler")

    monkeypatch.setattr(
        "omicsclaw.runtime.agent.dispatcher.dispatch",
        legacy_dispatch,
    )

    await channel._handle_message(update, context)

    submit.assert_awaited_once_with(update, "route me through control")
    context.bot.send_chat_action.assert_awaited_once_with(
        chat_id=-100123,
        action="typing",
    )
    assert legacy_calls == []
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("process_message", ("chat", "user", "text")),
        ("send", ("chat", "terminal text")),
        ("_send_chunk", ("chat", "formatted", "raw", {})),
        ("send_media", ("chat", "/tmp/artifact.png")),
    ],
)
async def test_legacy_telegram_dispatch_and_direct_delivery_seams_are_disabled(
    method_name,
    args,
):
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))

    with pytest.raises(RuntimeError, match="ControlRuntime|Delivery"):
        await getattr(channel, method_name)(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "application/octet-stream"])
async def test_document_handler_stays_fail_closed_before_fetch_or_download(mime_type):
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    document = SimpleNamespace(
        file_id="file-1",
        mime_type=mime_type,
        get_file=AsyncMock(
            side_effect=AssertionError("attachment must not be fetched")
        ),
        download_to_drive=AsyncMock(
            side_effect=AssertionError("attachment must not be downloaded")
        ),
    )
    update = _update(document=document)
    context = _context()

    await channel._handle_document(update, context)

    update.message.reply_text.assert_awaited_once_with(
        "Telegram documents are not supported on the authoritative path yet."
    )
    context.bot.get_file.assert_not_awaited()
    document.get_file.assert_not_awaited()
    document.download_to_drive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "expected_content"),
    [
        (None, ()),
        ("Please interpret this image", (("text", "Please interpret this image"),)),
    ],
)
async def test_owner_single_photo_builds_descriptor_without_persisting_file_id(
    caption,
    expected_content,
):
    channel = TelegramChannel(
        TelegramConfig(
            bot_token="test-token",
            admin_chat_id=7,
            account_namespace="research",
        )
    )
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    photos = (
        _photo(unique_id="unique-small", file_id="secret-file-small", file_size=2),
        _photo(unique_id="unique-large", file_id="secret-file-large", file_size=4),
    )
    update = _update(photo=photos, caption=caption, text="")
    context, telegram_file = _download_context(b"jpeg")

    await channel._handle_photo(update, context)

    assert len(runtime.calls) == 1
    raw, _ports = runtime.calls[0]
    assert tuple((block.kind, block.text) for block in raw.content) == expected_content
    assert len(raw.attachments) == 1
    descriptor = raw.attachments[0]
    assert descriptor.schema_version == 1
    assert descriptor.ordinal == 0
    assert descriptor.source_attachment_id == "unique-large"
    assert descriptor.display_name == "telegram-photo-42.jpg"
    assert descriptor.declared_media_type == "image/jpeg"
    assert descriptor.declared_size == 4
    assert descriptor.declared_sha256 is None
    serialized = json.dumps(raw.to_json_dict(), sort_keys=True)
    assert "secret-file-large" not in serialized
    assert "secret-file-small" not in serialized
    assert context.bot.get_file.await_count == 0
    assert telegram_file.download_as_bytearray.await_count == 0
    context.bot.send_chat_action.assert_awaited_once_with(
        chat_id=-100123,
        action="typing",
    )

    source = runtime.attachment_sources[0]
    chunks = [chunk async for chunk in source.open("unique-large")]
    assert chunks == [b"jpeg"]
    context.bot.get_file.assert_awaited_once_with("secret-file-large")
    telegram_file.download_as_bytearray.assert_awaited_once_with()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_photo_does_not_open_process_local_source():
    channel = TelegramChannel(
        TelegramConfig(
            bot_token="test-token",
            admin_chat_id=7,
            account_namespace="research",
        )
    )
    runtime = _RecordingControlRuntime(
        acceptance_status=TurnAcceptanceStatus.DUPLICATE,
    )
    channel._control_runtime = runtime
    update = _update(
        photo=(_photo(unique_id="same-photo", file_id="secret-file"),),
        text="",
    )
    context, telegram_file = _download_context()

    await channel._handle_photo(update, context)

    assert len(runtime.calls) == 1
    assert runtime.attachment_sources[0] is not None
    context.bot.get_file.assert_not_awaited()
    telegram_file.download_as_bytearray.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_group_is_rejected_before_runtime_or_fetch():
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    update = _update(
        photo=(_photo(unique_id="album-photo", file_id="secret-file"),),
        media_group_id="album-1",
        text="",
    )
    context, telegram_file = _download_context()

    await channel._handle_photo(update, context)

    assert runtime.calls == []
    context.bot.get_file.assert_not_awaited()
    telegram_file.download_as_bytearray.assert_not_awaited()
    context.bot.send_chat_action.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with(
        "Telegram photo albums are not supported yet."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("file_size", [None, 0, 20 * 1024 * 1024 + 1])
async def test_unbounded_photo_size_is_rejected_before_runtime_or_fetch(file_size):
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    photo = SimpleNamespace(
        file_unique_id="bounded-photo",
        file_id="secret-file",
        file_size=file_size,
    )
    update = _update(photo=(photo,), text="")
    context, telegram_file = _download_context()

    await channel._handle_photo(update, context)

    assert runtime.calls == []
    context.bot.get_file.assert_not_awaited()
    telegram_file.download_as_bytearray.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with(
        "This Telegram photo was not accepted."
    )


@pytest.mark.asyncio
async def test_image_document_routed_to_photo_handler_remains_fail_closed():
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    document = SimpleNamespace(file_id="secret-document", mime_type="image/png")
    update = _update(document=document)
    context = _context()

    await channel._handle_photo(update, context)

    assert runtime.calls == []
    context.bot.get_file.assert_not_awaited()
    context.bot.send_chat_action.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with(
        "Telegram documents are not supported on the authoritative path yet."
    )


@pytest.mark.asyncio
async def test_non_owner_photo_is_silent_before_runtime_or_fetch():
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    update = _update(
        user_id=9,
        photo=(_photo(unique_id="private-photo", file_id="secret-file"),),
        text="",
    )
    context, telegram_file = _download_context()

    await channel._handle_photo(update, context)

    assert runtime.calls == []
    context.bot.get_file.assert_not_awaited()
    telegram_file.download_as_bytearray.assert_not_awaited()
    context.bot.send_chat_action.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_rejection_uses_stable_notice_without_detail():
    channel = TelegramChannel(
        TelegramConfig(
            bot_token="test-token",
            admin_chat_id=7,
            account_namespace="research",
        )
    )
    runtime = _RecordingControlRuntime(
        acceptance_status=TurnAcceptanceStatus.REJECTED,
        acceptance_code="attachment_rejected",
    )
    channel._control_runtime = runtime
    update = _update(
        photo=(_photo(unique_id="bad-photo", file_id="secret-file"),),
        text="",
    )
    context, _telegram_file = _download_context()

    await channel._handle_photo(update, context)

    update.message.reply_text.assert_awaited_once_with(
        "This Telegram photo was not accepted."
    )
    context.bot.get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_owner_message_is_silent_and_never_reaches_control_runtime():
    channel = TelegramChannel(
        TelegramConfig(
            bot_token="test-token",
            admin_chat_id=7,
            allowed_senders={"8"},
        )
    )
    runtime = _RecordingControlRuntime()
    channel._control_runtime = runtime
    update = _update(user_id=9)
    context = _context()

    await channel._handle_message(update, context)

    assert runtime.calls == []
    context.bot.send_chat_action.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_denied_control_rejection_is_also_silent():
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    runtime = _RecordingControlRuntime(
        acceptance_status=TurnAcceptanceStatus.REJECTED,
        acceptance_code="owner_denied",
    )
    channel._control_runtime = runtime
    update = _update(user_id=9)

    await channel._submit_control_text(update, "untrusted text")

    assert len(runtime.calls) == 1
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_control_runtime_rejects_empty_owner_configuration():
    channel = TelegramChannel(TelegramConfig(bot_token="test-token"))

    with pytest.raises(
        RuntimeError,
        match="requires TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_SENDERS",
    ):
        await channel._start_control_runtime()

    assert channel._control_runtime is None


@pytest.mark.asyncio
async def test_start_control_runtime_derives_account_namespace_from_bot_identity(
    monkeypatch,
):
    created: list[dict[str, object]] = []
    runtime = SimpleNamespace(start=AsyncMock())

    def build_runtime(**kwargs):
        created.append(kwargs)
        return runtime

    monkeypatch.setattr(ControlRuntime, "for_channel_surface", build_runtime)
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=123456)),
    )
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    channel._app = SimpleNamespace(bot=bot)

    await channel._start_control_runtime()

    bot.get_me.assert_awaited_once()
    runtime.start.assert_awaited_once()
    assert channel.tg_config.account_namespace == "bot-123456"
    assert created[0]["account_namespace"] == "bot-123456"
    assert created[0]["owner_identities"] == {
        "channel/telegram/bot-123456/telegram_user": frozenset({"7"})
    }
    assert created[0]["attachment_input_enabled"] is True


@pytest.mark.asyncio
async def test_start_control_runtime_rejects_namespace_not_bound_to_bot_identity():
    channel = TelegramChannel(
        TelegramConfig(
            bot_token="test-token",
            admin_chat_id=7,
            account_namespace="friendly-alias",
        )
    )
    channel._app = SimpleNamespace(
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=123456)),
        )
    )

    with pytest.raises(RuntimeError, match="authenticated Bot identity"):
        await channel._start_control_runtime()

    assert channel._control_runtime is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    ["_cmd_start", "_cmd_skills", "_cmd_demo", "_cmd_status", "_cmd_health"],
)
async def test_all_telegram_commands_silently_reject_non_owner(handler_name):
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    update = _update(user_id=9)
    context = SimpleNamespace(
        args=[], bot=SimpleNamespace(send_chat_action=AsyncMock())
    )

    await getattr(channel, handler_name)(update, context)

    update.message.reply_text.assert_not_awaited()
    context.bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_activation_preserves_pending_updates():
    updater = SimpleNamespace(
        running=False,
        start_polling=AsyncMock(),
        stop=AsyncMock(),
    )
    app = SimpleNamespace(
        updater=updater,
        running=True,
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
    )
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    channel._app = app
    channel._start_control_runtime = AsyncMock()

    await channel._activate_application()

    updater.start_polling.assert_awaited_once_with(drop_pending_updates=False)


@pytest.mark.asyncio
async def test_polling_start_failure_releases_runtime_and_application():
    updater = SimpleNamespace(
        running=False,
        start_polling=AsyncMock(side_effect=RuntimeError("polling failed")),
        stop=AsyncMock(),
    )
    app = SimpleNamespace(
        updater=updater,
        running=True,
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
    )
    runtime = SimpleNamespace(close=AsyncMock())
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    channel._app = app

    async def start_control_runtime() -> None:
        channel._control_runtime = runtime

    channel._start_control_runtime = start_control_runtime

    with pytest.raises(RuntimeError, match="polling failed"):
        await channel._activate_application()

    runtime.close.assert_awaited_once()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()
    assert channel._control_runtime is None
    assert channel._app is None


@pytest.mark.asyncio
async def test_stop_attempts_every_cleanup_stage_and_sanitizes_failures(caplog):
    updater = SimpleNamespace(
        running=True,
        stop=AsyncMock(side_effect=RuntimeError("secret-polling-detail")),
    )
    app = SimpleNamespace(
        updater=updater,
        running=True,
        stop=AsyncMock(side_effect=RuntimeError("secret-app-detail")),
        shutdown=AsyncMock(),
    )
    runtime = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("secret-runtime-detail"))
    )
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    channel._typing_manager.stop_all = AsyncMock(
        side_effect=RuntimeError("secret-typing-detail")
    )
    channel._updater = updater
    channel._control_runtime = runtime
    channel._app = app

    await channel.stop()

    channel._typing_manager.stop_all.assert_awaited_once()
    updater.stop.assert_awaited_once()
    runtime.close.assert_awaited_once()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()
    assert "secret-" not in caplog.text
    assert channel._updater is None
    assert channel._control_runtime is None
    assert channel._app is None


@pytest.mark.asyncio
async def test_error_handler_does_not_log_or_audit_provider_error_detail(
    monkeypatch,
    caplog,
):
    audits: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "omicsclaw.runtime.agent.state.audit",
        lambda *args, **kwargs: audits.append((args, kwargs)),
    )
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    context = SimpleNamespace(error=RuntimeError("secret-provider-payload"))

    await channel._error_handler(None, context)

    assert "secret-provider-payload" not in caplog.text
    assert audits == [(("error",), {"severity": "HIGH", "error_type": "RuntimeError"})]


def test_token_redaction_covers_propagated_telegram_child_loggers(caplog):
    token = "123456:SECRET"
    channel = TelegramChannel(TelegramConfig(bot_token=token, admin_chat_id=7))
    channel._setup_token_redaction()

    logging.getLogger("telegram.ext.Application").warning(
        "provider URL contains %s",
        token,
    )

    assert token not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_run_polling_closes_event_loop_when_startup_fails(monkeypatch):
    created_loops: list[asyncio.AbstractEventLoop] = []
    real_new_event_loop = asyncio.new_event_loop

    def tracked_new_event_loop():
        loop = real_new_event_loop()
        created_loops.append(loop)
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", tracked_new_event_loop)
    channel = TelegramChannel(TelegramConfig(bot_token="test-token", admin_chat_id=7))
    channel.start = AsyncMock(side_effect=RuntimeError("startup failed"))
    channel.stop = AsyncMock()

    with pytest.raises(RuntimeError, match="startup failed"):
        channel.run_polling()

    channel.stop.assert_awaited_once()
    assert len(created_loops) == 1
    assert created_loops[0].is_closed()
