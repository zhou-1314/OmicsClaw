"""
Telegram channel implementation for OmicsClaw.

Extracts the platform-specific logic into a reusable Channel subclass.

This follows the EvoScientist Multi-Channel pattern where each platform
is a Channel subclass with start/stop/_send_chunk lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .base import Channel
from .capabilities import TELEGRAM as TELEGRAM_CAPS
from .config import BaseChannelConfig
from omicsclaw.control import ChannelSurfaceBinding

from .telegram_delivery import TelegramDeliveryAdapter

logger = logging.getLogger("omicsclaw.channel.telegram")

_PHOTO_REJECTED_NOTICE = "This Telegram photo was not accepted."
_PHOTO_ALBUM_UNSUPPORTED_NOTICE = "Telegram photo albums are not supported yet."
_DOCUMENT_UNSUPPORTED_NOTICE = (
    "Telegram documents are not supported on the authoritative path yet."
)
_MAX_TELEGRAM_PHOTO_BYTES = 20 * 1024 * 1024


@dataclass
class TelegramConfig(BaseChannelConfig):
    """Telegram-specific configuration."""

    bot_token: str = ""
    admin_chat_id: int = 0
    account_namespace: str = ""


class _TelegramPhotoSource:
    """Process-local capability translating opaque source IDs into Telegram bytes."""

    def __init__(self, bot: Any, file_ids: dict[str, str]) -> None:
        self._bot = bot
        self._file_ids = dict(file_ids)

    async def open(self, source_attachment_id: str) -> AsyncIterator[bytes]:
        try:
            file_id = self._file_ids[source_attachment_id]
        except KeyError as exc:
            raise ValueError("unknown Telegram attachment source") from exc
        telegram_file = await self._bot.get_file(file_id)
        payload = await telegram_file.download_as_bytearray()
        yield bytes(payload)


class TelegramChannel(Channel):
    """Telegram channel using python-telegram-bot with long polling.

    Text and one photo per message enter the authoritative ControlRuntime;
    canonical terminal replies leave only through the persistent Delivery Pump.
    Albums, documents and outbound media remain fail-closed.
    """

    name = "telegram"
    capabilities = TELEGRAM_CAPS
    authoritative_ingress = True

    def __init__(self, config: TelegramConfig):
        super().__init__(config)
        self.tg_config = config
        self._app = None
        self._updater = None
        self._control_runtime = None
        self._token_redact_filter = None

    # ── Lifecycle ────────────────────────────────────────────────────

    async def prepare_control_binding(self) -> ChannelSurfaceBinding:
        """Authenticate far enough to describe this Bot's control binding.

        The shared ControlRuntime is composed by the runner, so this method
        must bring the Application up to the point where the authenticated Bot
        identity is readable and a Delivery Adapter can be built over its bot.
        """

        owners = self._owner_subjects()
        if not owners:
            raise RuntimeError(
                "Telegram authoritative ingress requires TELEGRAM_CHAT_ID "
                "or TELEGRAM_ALLOWED_SENDERS"
            )
        if self._app is None:
            self._build_application()
        try:
            await self._app.initialize()
            await self._app.start()
            bot_identity = await self._app.bot.get_me()
        except BaseException:
            await self.stop()
            raise
        bot_id = getattr(bot_identity, "id", None)
        if isinstance(bot_id, bool) or not isinstance(bot_id, int):
            await self.stop()
            raise RuntimeError("Telegram Bot identity is unavailable")
        account_namespace = f"bot-{bot_id}"
        configured_namespace = self.tg_config.account_namespace.strip()
        if configured_namespace and configured_namespace != account_namespace:
            await self.stop()
            raise RuntimeError(
                "TELEGRAM_ACCOUNT_NAMESPACE must equal the authenticated Bot identity"
            )
        self.tg_config.account_namespace = account_namespace
        return ChannelSurfaceBinding(
            adapter="telegram",
            account_namespace=account_namespace,
            owner_identities={
                f"channel/telegram/{account_namespace}/telegram_user": owners
            },
            delivery_adapter=TelegramDeliveryAdapter(self._app.bot),
            attachment_input_enabled=True,
        )

    def _build_application(self) -> None:
        if not self.tg_config.bot_token:
            raise RuntimeError("Telegram bot token is required")

        try:
            from telegram.ext import (
                Application,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            raise RuntimeError(
                "python-telegram-bot not installed. "
                "Install with: pip install python-telegram-bot"
            )

        self._setup_token_redaction()

        self._app = Application.builder().token(self.tg_config.bot_token).build()

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("skills", self._cmd_skills))
        self._app.add_handler(CommandHandler("demo", self._cmd_demo))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("health", self._cmd_health))

        # Error handler
        self._app.add_error_handler(self._error_handler)

        # Message handlers
        self._app.add_handler(
            MessageHandler(
                filters.PHOTO | (filters.Document.IMAGE & ~filters.COMMAND),
                self._handle_photo,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.Document.ALL & ~filters.Document.IMAGE & ~filters.COMMAND,
                self._handle_document,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_message,
            )
        )

    async def start(self) -> None:
        """Phase 2: begin polling once the shared ControlRuntime is bound."""

        if self._control_runtime is None:
            raise RuntimeError(
                "Telegram requires the shared ControlRuntime to be bound "
                "before start()"
            )
        if self._app is None:  # pragma: no cover - prepare runs first
            raise RuntimeError("Telegram Application was not prepared")
        await self._activate_application()
        self._running = True
        logger.info("Telegram channel started (polling)")

    async def _activate_application(self) -> None:
        """Begin polling, rolling the whole Channel back on failure."""

        try:
            self._updater = self._app.updater
            await self._updater.start_polling(
                drop_pending_updates=False,
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._running = False
        try:
            await self._typing_manager.stop_all()
        except Exception as error:
            logger.warning("Telegram typing shutdown failed (%s)", type(error).__name__)
        if self._updater and self._updater.running:
            try:
                await self._updater.stop()
            except Exception as error:
                logger.warning(
                    "Telegram polling shutdown failed (%s)", type(error).__name__
                )
        # Detach only. The ControlRuntime is shared with every other cut-over
        # Channel and owned by the runner; closing it here would tear down
        # their control plane because this Channel's polling failed.
        self._control_runtime = None
        if self._app:
            if self._app.running:
                try:
                    await self._app.stop()
                except Exception as error:
                    logger.warning(
                        "Telegram Application stop failed (%s)",
                        type(error).__name__,
                    )
            try:
                await self._app.shutdown()
            except Exception as error:
                logger.warning(
                    "Telegram Application shutdown failed (%s)",
                    type(error).__name__,
                )
            logger.info("Telegram channel stopped")
        self._updater = None
        self._app = None


    def run_polling(self) -> None:
        """Synchronous entry point — run the Telegram bot with polling.

        This replaces the old ``app.run_polling()`` and is the primary
        way to start the Telegram bot (backward-compatible).
        Calls start() internally, then blocks until interrupted.
        """
        from omicsclaw.runtime.agent import state as core

        logger.info(
            f"Starting OmicsClaw Telegram bot "
            f"(provider: {core.LLM_PROVIDER_NAME}, model: {core.OMICSCLAW_MODEL})"
        )
        logger.info(f"OmicsClaw directory: {core.OMICSCLAW_DIR}")
        if self.tg_config.admin_chat_id:
            logger.info(f"Admin chat ID: {self.tg_config.admin_chat_id}")
        core.audit(
            "bot_start",
            platform="telegram",
            provider=core.LLM_PROVIDER_NAME,
            model=core.OMICSCLAW_MODEL,
            admin_chat=self.tg_config.admin_chat_id,
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.start())
            print("OmicsClaw Telegram bot is running. Press Ctrl+C to stop.")
            try:
                loop.run_forever()
            except KeyboardInterrupt:
                pass
        finally:
            try:
                loop.run_until_complete(self.stop())
            finally:
                loop.close()
                asyncio.set_event_loop(None)

    # ── Core send implementation ─────────────────────────────────────

    async def process_message(self, *args, **kwargs) -> str:
        raise RuntimeError(
            "Telegram messages must enter through the authoritative ControlRuntime"
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        raise RuntimeError(
            "Telegram terminal text must leave through the persistent Delivery Outbox"
        )

    async def _send_chunk(
        self,
        chat_id: str,
        formatted_text: str,
        raw_text: str,
        metadata: dict[str, Any],
    ) -> None:
        raise RuntimeError(
            "Telegram text chunks must leave through the persistent Delivery Outbox"
        )

    async def send_media(
        self,
        chat_id: str,
        file_path: str,
        caption: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        raise RuntimeError(
            "Telegram media Delivery is disabled until durable artifact references land"
        )

    # ── Typing indicator ─────────────────────────────────────────────

    async def _send_typing(self, chat_id: str) -> None:
        if self._app:
            await self._app.bot.send_chat_action(
                chat_id=int(chat_id),
                action="typing",
            )

    # ── Admin check ──────────────────────────────────────────────────

    def _is_admin(self, sender_id: str) -> bool:
        if not self.tg_config.admin_chat_id:
            return False
        try:
            return int(sender_id) == self.tg_config.admin_chat_id
        except (ValueError, TypeError):
            return False

    def _owner_subjects(self) -> frozenset[str]:
        configured = {
            str(value).strip()
            for value in (self.config.allowed_senders or set())
            if str(value).strip()
        }
        if self.tg_config.admin_chat_id:
            configured.add(str(self.tg_config.admin_chat_id))
        return frozenset(configured)

    def _owner_update_allowed(self, update) -> bool:
        user = getattr(update, "effective_user", None)
        if user is None or str(user.id) not in self._owner_subjects():
            logger.warning("Ignored Telegram update from a non-Owner")
            return False
        return True

    # ── Token redaction ──────────────────────────────────────────────

    def _setup_token_redaction(self) -> None:
        """Add a logging filter to redact the bot token from log output."""
        token = self.tg_config.bot_token
        if not token:
            return

        class _TokenRedactFilter(logging.Filter):
            def __init__(self, tok: str):
                super().__init__()
                self._token = tok

            def filter(self, record: logging.LogRecord) -> bool:
                if self._token and self._token in record.getMessage():
                    record.msg = record.getMessage().replace(
                        self._token,
                        "[REDACTED]",
                    )
                    record.args = ()
                return True

        redact = _TokenRedactFilter(token)
        self._token_redact_filter = redact
        protected_loggers = [
            value
            for name, value in logging.Logger.manager.loggerDict.items()
            if isinstance(value, logging.Logger)
            and name.startswith(("httpx", "telegram", "httpcore"))
        ]
        protected_loggers.extend(
            logging.getLogger(name) for name in ("httpx", "telegram", "httpcore")
        )
        for protected_logger in protected_loggers:
            protected_logger.addFilter(redact)
            for handler in protected_logger.handlers:
                handler.addFilter(redact)
        # Descendant records are filtered by ancestor handlers, not ancestor
        # Logger filters. Root handler coverage closes that propagation gap.
        for handler in logging.getLogger().handlers:
            handler.addFilter(redact)

    # ── Helpers ──────────────────────────────────────────────────────

    async def _send_long_message(self, update, text: str) -> None:
        """Send a potentially long text message, auto-chunking."""
        from omicsclaw.runtime.agent import state as core

        text = core.strip_markup(text)
        limit = self.capabilities.max_text_length
        if len(text) <= limit:
            await update.message.reply_text(text)
            return
        from .base import chunk_text

        for chunk in chunk_text(text, limit):
            if chunk.strip():
                await update.message.reply_text(chunk)

    async def _submit_control_inbound(
        self,
        update,
        text: str,
        *,
        attachments: tuple[object, ...] = (),
        attachment_source: object | None = None,
    ):
        """Normalize one Telegram message and await its durable Turn Receipt."""

        from omicsclaw.control import (
            ControlRuntimePorts,
            RawContentBlockV1,
            RawInboundV1,
            TurnAcceptanceStatus,
        )
        from omicsclaw.runtime.agent import state as core

        if self._control_runtime is None:
            raise RuntimeError("Telegram ControlRuntime is not started")
        message = update.message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return None
        account_namespace = self.tg_config.account_namespace.strip()
        reply_target: dict[str, object] = {
            "schema_version": 1,
            "kind": "channel",
            "adapter": "telegram",
            "account_namespace": account_namespace,
            "destination_id": str(chat.id),
        }
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None:
            reply_target["thread_id"] = str(thread_id)
        raw = RawInboundV1(
            schema_version=1,
            surface="channel",
            source_namespace=f"channel/telegram/v1/{account_namespace}",
            source_request_id=f"{chat.id}:{message.message_id}",
            external_subject={"kind": "telegram_user", "value": str(user.id)},
            reply_target=reply_target,
            content=(RawContentBlockV1(kind="text", text=text),) if text else (),
            attachments=attachments,
            transport_facts={"provider_event_kind": "message"},
        )
        ports = ControlRuntimePorts(
            user_id=str(user.id),
            workspace=str(core.DATA_DIR),
            thread_id=str(thread_id) if thread_id is not None else "",
        )
        if attachment_source is None:
            result = await self._control_runtime.submit_and_wait(raw, ports)
        else:
            result = await self._control_runtime.submit_and_wait(
                raw,
                ports,
                attachment_source=attachment_source,
            )
        if result.acceptance.status is TurnAcceptanceStatus.REJECTED:
            if result.acceptance.code == "owner_denied":
                logger.warning("Ignored Telegram message from a non-Owner")
            else:
                notices = {
                    "delivery_backpressure": (
                        "OmicsClaw is busy delivering earlier replies. "
                        "Please retry later."
                    ),
                    "control_not_ready": "OmicsClaw is still starting. Please retry.",
                }
                if result.acceptance.code.startswith("attachment_"):
                    notice = _PHOTO_REJECTED_NOTICE
                else:
                    notice = notices.get(
                        result.acceptance.code,
                        "This Telegram request was not accepted.",
                    )
                await message.reply_text(notice)
        return result

    async def _submit_control_text(self, update, text: str):
        """Submit a text-only Telegram message through the shared helper."""

        return await self._submit_control_inbound(update, text)

    # ── Command handlers ─────────────────────────────────────────────

    async def _cmd_start(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        await update.message.reply_text(
            "Welcome to OmicsClaw -- multi-omics analysis at your fingertips!\n\n"
            "I can analyze spatial, single-cell, genomics, proteomics, and metabolomics data "
            "through a unified skill system.\n\n"
            "Commands:\n"
            "  /skills  -- list available analysis skills\n"
            "  /demo <skill>  -- run a demo (preprocess, domains, de, genes, statistics, ...)\n"
            "  /status  -- bot info\n"
            "  /health  -- system health check\n\n"
            "Or just chat -- ask any multi-omics analysis question, or send one "
            "photo with an optional caption. Photo albums and documents are not "
            "supported yet.\n\n"
            "OmicsClaw is a research tool, not a medical device. "
            "Consult a domain expert before making decisions based on these results."
        )

    async def _cmd_skills(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        try:
            from omicsclaw.runtime.agent import state as core

            output = core.format_skills_table()
            await self._send_long_message(update, output or "No skills found.")
        except Exception:
            logger.exception("Telegram skill listing failed")
            await update.message.reply_text("Unable to list skills right now.")

    async def _cmd_demo(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        skill = context.args[0] if context.args else "preprocess"
        await update.message.reply_text(
            f"Running {skill} demo -- this may take a moment..."
        )
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )
        try:
            await self._submit_control_text(
                update,
                f"Run the {skill} demo using the omicsclaw tool with mode='demo'.",
            )
        except Exception:
            logger.exception("Telegram demo submission failed")

    async def _cmd_status(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        from omicsclaw.runtime.agent import state as core

        uptime_secs = int(time.time() - core.BOT_START_TIME)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, secs = divmod(remainder, 60)

        skills_dir = core.OMICSCLAW_DIR / "skills"
        skill_count = (
            sum(
                1
                for d in skills_dir.iterdir()
                if d.is_dir() and (d / "SKILL.md").exists()
            )
            if skills_dir.exists()
            else 0
        )

        provider_label = core.LLM_PROVIDER_NAME or "auto"
        status_msg = (
            f"OmicsClaw Bot Status\n"
            f"====================\n"
            f"Bot uptime: {hours}h {minutes}m {secs}s\n"
            f"LLM provider: {provider_label}\n"
            f"LLM model: {core.OMICSCLAW_MODEL}\n"
            f"Skills available: {skill_count}\n"
        )
        await update.message.reply_text(status_msg)

    async def _cmd_health(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        from omicsclaw.runtime.agent import state as core

        checks = []
        if core.OMICSCLAW_PY.exists():
            checks.append("OmicsClaw CLI: OK")
        else:
            checks.append("OmicsClaw CLI: MISSING")
        if core.SOUL_MD.exists():
            checks.append("SOUL.md: OK")
        else:
            checks.append("SOUL.md: MISSING (using fallback)")

        skills_dir = core.OMICSCLAW_DIR / "skills"
        if skills_dir.exists():
            implemented = [
                d.name
                for d in sorted(skills_dir.iterdir())
                if d.is_dir() and (d / "SKILL.md").exists() and any(d.glob("*.py"))
            ]
            checks.append(f"Skills (implemented): {len(implemented)}")
        else:
            checks.append("Skills directory: MISSING")

        if core.OUTPUT_DIR.exists():
            output_count = sum(1 for _ in core.OUTPUT_DIR.iterdir())
            checks.append(f"Output runs: {output_count}")
        else:
            checks.append("Output directory: not yet created")

        await update.message.reply_text(
            "OmicsClaw Health Check\n" "======================\n" + "\n".join(checks)
        )

    # ── Message handlers ─────────────────────────────────────────────

    async def _handle_message(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        if not update.message or not update.message.text:
            return

        from omicsclaw.runtime.agent import state as core

        user_text = update.message.text
        logger.info(
            f"Message from {update.effective_user.first_name}: {user_text[:100]}"
        )
        core.audit(
            "message",
            user_id=update.effective_user.id if update.effective_user else None,
            text_preview=user_text[:200],
        )

        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="typing"
            )
            await self._submit_control_text(update, user_text)
        except Exception:
            # The Turn may already be durably accepted. A direct fallback reply
            # could then duplicate or overtake its canonical Outbox Delivery.
            logger.exception("Telegram text submission failed")

    async def _handle_photo(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        if not update.message:
            return
        message = update.message
        # The broad registration filter also routes image documents here. They
        # remain a separate unsupported contract and must never be fetched.
        if getattr(message, "document", None) is not None:
            await message.reply_text(_DOCUMENT_UNSUPPORTED_NOTICE)
            return
        # Telegram emits every item in an album as an individual update. Reject
        # before creating the process-local byte capability so no partial album
        # can cross the acceptance seam.
        if getattr(message, "media_group_id", None) is not None:
            await message.reply_text(_PHOTO_ALBUM_UNSUPPORTED_NOTICE)
            return
        photos = tuple(getattr(message, "photo", ()) or ())
        if not photos:
            return

        photo = photos[-1]
        source_attachment_id = getattr(photo, "file_unique_id", None)
        file_id = getattr(photo, "file_id", None)
        file_size = getattr(photo, "file_size", None)
        if not isinstance(source_attachment_id, str) or not source_attachment_id:
            await message.reply_text(_PHOTO_REJECTED_NOTICE)
            return
        if not isinstance(file_id, str) or not file_id:
            await message.reply_text(_PHOTO_REJECTED_NOTICE)
            return
        if (
            not isinstance(file_size, int)
            or isinstance(file_size, bool)
            or file_size <= 0
            or file_size > _MAX_TELEGRAM_PHOTO_BYTES
        ):
            await message.reply_text(_PHOTO_REJECTED_NOTICE)
            return

        from omicsclaw.attachments import SourceAttachmentDescriptorV1

        try:
            descriptor = SourceAttachmentDescriptorV1(
                schema_version=1,
                ordinal=0,
                source_attachment_id=source_attachment_id,
                display_name=f"telegram-photo-{message.message_id}.jpg",
                declared_media_type="image/jpeg",
                declared_size=file_size,
                declared_sha256=None,
            )
        except (TypeError, ValueError):
            await message.reply_text(_PHOTO_REJECTED_NOTICE)
            return

        source = _TelegramPhotoSource(
            context.bot,
            {source_attachment_id: file_id},
        )
        caption = getattr(message, "caption", None)
        if not isinstance(caption, str):
            caption = ""
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            await self._submit_control_inbound(
                update,
                caption,
                attachments=(descriptor,),
                attachment_source=source,
            )
        except Exception as error:
            # Acceptance may already be durable, so never race the canonical
            # Delivery with a direct fallback response.
            logger.error(
                "Telegram photo submission failed (%s)",
                type(error).__name__,
            )

    async def _handle_document(self, update, context) -> None:
        if not self._owner_update_allowed(update):
            return
        if not update.message or not update.message.document:
            return

        await update.message.reply_text(_DOCUMENT_UNSUPPORTED_NOTICE)

    # ── Error handler ────────────────────────────────────────────────

    async def _error_handler(self, update, context) -> None:
        from omicsclaw.runtime.agent import state as core

        err = context.error
        if err is None:
            return
        err_name = type(err).__name__
        if "Forbidden" in err_name or "forbidden" in str(err).lower():
            logger.info("Telegram provider rejected access (%s)", err_name)
            return
        if err_name in ("TimedOut", "NetworkError", "RetryAfter"):
            logger.warning("Transient Telegram provider error (%s)", err_name)
            return
        # Provider errors may embed credentials, request URLs or payloads. Keep
        # logging and audit evidence to the bounded exception classification.
        logger.error("Unhandled Telegram provider error (%s)", err_name)
        core.audit("error", severity="HIGH", error_type=err_name)
