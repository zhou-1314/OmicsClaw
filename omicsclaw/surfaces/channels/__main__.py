#!/usr/bin/env python3
"""
run.py — OmicsClaw Multi-Channel Runner
========================================
Unified entry point for running one or more bot channels concurrently.

Usage:
    # Run Telegram only:
    python -m omicsclaw.surfaces.channels.__main__ --channels telegram

    # Other Adapters are listed for migration visibility but fail closed until
    # they receive the same ControlRuntime + persistent Delivery cutover.

    # Run with health check server:
    python -m omicsclaw.surfaces.channels.__main__ --channels telegram --health-port 8080

    # List available channels:
    python -m omicsclaw.surfaces.channels.__main__ --list

Environment:
    All channel configs are read from .env (same as standalone scripts).
    See bot/README.md for full configuration reference.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import logging
import os
import sys
from pathlib import Path
from typing import Mapping

from omicsclaw.common.runtime_env import load_env_file
from omicsclaw.providers.registry import detect_provider_from_env, resolve_provider

# Ensure project root on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env
for _p in [_PROJECT_ROOT / ".env", Path.cwd() / ".env"]:
    if load_env_file(_p, override=False):
        break

from omicsclaw.runtime.agent import state as core  # noqa: E402
from omicsclaw.surfaces.channels import CHANNEL_REGISTRY  # noqa: E402

logger = logging.getLogger("omicsclaw.runner")


def _resolve_bootstrap_llm_config(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str | None, str, str, str, int]:
    """Resolve the effective LLM bootstrap config from environment variables.

    Returns ``(provider, base_url, model, api_key, auth_mode, ccproxy_port)``.
    ``auth_mode`` is ``"oauth"`` when ``LLM_AUTH_MODE=oauth`` is set, else
    ``"api_key"`` (default). ``ccproxy_port`` comes from ``CCPROXY_PORT`` or
    falls back to 11435.
    """
    source = os.environ if env is None else env
    explicit_provider = str(source.get("LLM_PROVIDER", "") or "").strip().lower()
    detected_provider = detect_provider_from_env(env=source)
    effective_provider = explicit_provider or detected_provider

    resolved_url, resolved_model, resolved_key = resolve_provider(
        provider=explicit_provider,
        base_url=str(source.get("LLM_BASE_URL", "") or ""),
        model=str(
            source.get("OMICSCLAW_MODEL", source.get("SPATIALCLAW_MODEL", "")) or ""
        ),
        api_key=str(source.get("LLM_API_KEY", "") or ""),
        env=source,
    )

    auth_mode = str(source.get("LLM_AUTH_MODE", "") or "").strip().lower() or "api_key"
    try:
        ccproxy_port = int(source.get("CCPROXY_PORT", "11435") or "11435")
    except (TypeError, ValueError):
        ccproxy_port = 11435

    return (
        effective_provider,
        resolved_url,
        resolved_model,
        resolved_key,
        auth_mode,
        ccproxy_port,
    )


def _build_telegram_channel():
    """Build a TelegramChannel from environment variables."""
    from omicsclaw.surfaces.channels.telegram import TelegramChannel, TelegramConfig

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_SENDERS", "")
    allowed = {value.strip() for value in allowed_raw.split(",") if value.strip()}
    return TelegramChannel(
        TelegramConfig(
            bot_token=token,
            admin_chat_id=int(os.environ.get("TELEGRAM_CHAT_ID", "0") or "0"),
            account_namespace=os.environ.get("TELEGRAM_ACCOUNT_NAMESPACE", "").strip(),
            allowed_senders=allowed or None,
        )
    )


def _build_feishu_channel():
    """Build a FeishuChannel from environment variables."""
    from omicsclaw.surfaces.channels.feishu import FeishuChannel, FeishuConfig

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET required")
    # Owner identity is Backend configuration, not a Surface decision: the
    # authoritative ingress refuses to start without it (ADR 0044/0060).
    allowed_senders = {
        value.strip()
        for value in os.environ.get("FEISHU_ALLOWED_SENDERS", "").split(",")
        if value.strip()
    }
    if not allowed_senders:
        raise RuntimeError(
            "FEISHU_ALLOWED_SENDERS is required: authoritative Feishu ingress "
            "admits only configured Owner open_id values"
        )
    return FeishuChannel(
        FeishuConfig(
            allowed_senders=allowed_senders,
            # Optional: without it group @-mentions cannot be attributed to this
            # Bot, so group chats fail closed and only p2p messages are served.
            bot_open_id=os.environ.get("FEISHU_BOT_OPEN_ID", "").strip(),
            app_id=app_id,
            app_secret=app_secret,
            thinking_threshold_ms=int(
                os.environ.get("FEISHU_THINKING_THRESHOLD_MS", "2500")
            ),
            max_inbound_image_mb=int(
                os.environ.get("FEISHU_MAX_INBOUND_IMAGE_MB", "12")
            ),
            max_inbound_file_mb=int(os.environ.get("FEISHU_MAX_INBOUND_FILE_MB", "40")),
            max_attachments=int(os.environ.get("FEISHU_MAX_ATTACHMENTS", "4")),
            rate_limit_per_hour=int(os.environ.get("FEISHU_RATE_LIMIT_PER_HOUR", "60")),
            debug=os.environ.get("FEISHU_BRIDGE_DEBUG", "") == "1",
        )
    )


def _build_dingtalk_channel():
    """Build a DingTalkChannel from environment variables."""
    from omicsclaw.surfaces.channels.dingtalk import DingTalkChannel, DingTalkConfig

    client_id = os.environ.get("DINGTALK_CLIENT_ID", "")
    client_secret = os.environ.get("DINGTALK_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required")
    return DingTalkChannel(
        DingTalkConfig(
            client_id=client_id,
            client_secret=client_secret,
            rate_limit_per_hour=int(
                os.environ.get("DINGTALK_RATE_LIMIT_PER_HOUR", "60")
            ),
        )
    )


def _build_discord_channel():
    """Build a DiscordChannel from environment variables."""
    from omicsclaw.surfaces.channels.discord import DiscordChannel, DiscordConfig

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    return DiscordChannel(
        DiscordConfig(
            bot_token=token,
            rate_limit_per_hour=int(
                os.environ.get("DISCORD_RATE_LIMIT_PER_HOUR", "60")
            ),
            proxy=os.environ.get("DISCORD_PROXY"),
        )
    )


def _build_slack_channel():
    """Build a SlackChannel from environment variables."""
    from omicsclaw.surfaces.channels.slack import SlackChannel, SlackConfig

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not bot_token or not app_token:
        raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN required")
    return SlackChannel(
        SlackConfig(
            bot_token=bot_token,
            app_token=app_token,
            rate_limit_per_hour=int(os.environ.get("SLACK_RATE_LIMIT_PER_HOUR", "60")),
        )
    )


def _build_wechat_channel():
    """Build a WeChatChannel from environment variables.

    Automatically selects backend based on which env vars are set:
    - WECOM_CORP_ID → wecom backend (企业微信)
    - WECHAT_APP_ID → wechatmp backend (公众号)
    """
    wecom_corp_id = os.environ.get("WECOM_CORP_ID", "")
    wechat_app_id = os.environ.get("WECHAT_APP_ID", "")

    if wecom_corp_id:
        from omicsclaw.surfaces.channels.wechat import WeChatChannel, WeComConfig

        return WeChatChannel(
            WeComConfig(
                corp_id=wecom_corp_id,
                agent_id=os.environ.get("WECOM_AGENT_ID", ""),
                secret=os.environ.get("WECOM_SECRET", ""),
                token=os.environ.get("WECOM_TOKEN", ""),
                encoding_aes_key=os.environ.get("WECOM_ENCODING_AES_KEY", ""),
                webhook_port=int(os.environ.get("WECOM_WEBHOOK_PORT", "9001")),
            ),
            backend="wecom",
        )
    elif wechat_app_id:
        from omicsclaw.surfaces.channels.wechat import WeChatChannel, WeChatMPConfig

        return WeChatChannel(
            WeChatMPConfig(
                app_id=wechat_app_id,
                app_secret=os.environ.get("WECHAT_APP_SECRET", ""),
                token=os.environ.get("WECHAT_TOKEN", ""),
                encoding_aes_key=os.environ.get("WECHAT_ENCODING_AES_KEY", ""),
                webhook_port=int(os.environ.get("WECHAT_WEBHOOK_PORT", "9001")),
            ),
            backend="wechatmp",
        )
    else:
        raise RuntimeError(
            "WeChat config not found. Set WECOM_CORP_ID (企业微信) "
            "or WECHAT_APP_ID (公众号)"
        )


def _build_qq_channel():
    """Build a QQChannel from environment variables."""
    from omicsclaw.surfaces.channels.qq import QQChannel, QQConfig

    app_id = os.environ.get("QQ_APP_ID", "")
    app_secret = os.environ.get("QQ_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("QQ_APP_ID and QQ_APP_SECRET required")
    allowed_raw = os.environ.get("QQ_ALLOWED_SENDERS", "")
    allowed = {s.strip() for s in allowed_raw.split(",") if s.strip()} or None
    return QQChannel(
        QQConfig(
            app_id=app_id,
            app_secret=app_secret,
            allowed_senders=allowed,
            rate_limit_per_hour=int(os.environ.get("QQ_RATE_LIMIT_PER_HOUR", "60")),
        )
    )


def _build_email_channel():
    """Build an EmailChannel from environment variables."""
    from omicsclaw.surfaces.channels.email import EmailChannel, EmailConfig

    imap_host = os.environ.get("EMAIL_IMAP_HOST", "")
    imap_user = os.environ.get("EMAIL_IMAP_USERNAME", "")
    smtp_host = os.environ.get("EMAIL_SMTP_HOST", "")
    smtp_user = os.environ.get("EMAIL_SMTP_USERNAME", "")
    if not imap_host or not imap_user or not smtp_host or not smtp_user:
        raise RuntimeError(
            "EMAIL_IMAP_HOST, EMAIL_IMAP_USERNAME, "
            "EMAIL_SMTP_HOST, EMAIL_SMTP_USERNAME required"
        )
    allowed_raw = os.environ.get("EMAIL_ALLOWED_SENDERS", "")
    allowed = {s.strip() for s in allowed_raw.split(",") if s.strip()} or None
    return EmailChannel(
        EmailConfig(
            imap_host=imap_host,
            imap_port=int(os.environ.get("EMAIL_IMAP_PORT", "993")),
            imap_username=imap_user,
            imap_password=os.environ.get("EMAIL_IMAP_PASSWORD", ""),
            imap_mailbox=os.environ.get("EMAIL_IMAP_MAILBOX", "INBOX"),
            imap_use_ssl=os.environ.get("EMAIL_IMAP_USE_SSL", "1") != "0",
            smtp_host=smtp_host,
            smtp_port=int(os.environ.get("EMAIL_SMTP_PORT", "587")),
            smtp_username=smtp_user,
            smtp_password=os.environ.get("EMAIL_SMTP_PASSWORD", ""),
            smtp_starttls=os.environ.get("EMAIL_SMTP_STARTTLS", "1") != "0",
            from_address=os.environ.get("EMAIL_FROM_ADDRESS", smtp_user),
            poll_interval=int(os.environ.get("EMAIL_POLL_INTERVAL", "30")),
            mark_seen=os.environ.get("EMAIL_MARK_SEEN", "1") != "0",
            allowed_senders=allowed,
        )
    )


def _build_imessage_channel():
    """Build an IMessageChannel from environment variables (macOS only)."""
    import sys

    if sys.platform != "darwin":
        raise RuntimeError("iMessage channel requires macOS")
    from omicsclaw.surfaces.channels.imessage import IMessageChannel, IMessageConfig

    allowed_raw = os.environ.get("IMESSAGE_ALLOWED_SENDERS", "")
    allowed = {s.strip() for s in allowed_raw.split(",") if s.strip()} or None
    return IMessageChannel(
        IMessageConfig(
            cli_path=os.environ.get("IMESSAGE_CLI_PATH", "imsg"),
            service=os.environ.get("IMESSAGE_SERVICE", "auto"),
            region=os.environ.get("IMESSAGE_REGION", "US"),
            allowed_senders=allowed,
        )
    )


# Channel factory registry
CHANNEL_BUILDERS = {
    "telegram": _build_telegram_channel,
    "feishu": _build_feishu_channel,
    "dingtalk": _build_dingtalk_channel,
    "discord": _build_discord_channel,
    "slack": _build_slack_channel,
    "wechat": _build_wechat_channel,
    "qq": _build_qq_channel,
    "email": _build_email_channel,
    "imessage": _build_imessage_channel,
}

# A Channel joins this set only after it has an equivalent cutover: a Backend
# ControlRuntime, a normalized RawInboundV1 ingress, and a single-attempt
# persistent Delivery Adapter. Feishu joined with its text-only slice
# (ADR 0060/0063); its inbound attachments and outbound media stay fail-closed.
AUTHORITATIVE_CHANNELS = frozenset({"telegram", "feishu"})


def _require_authoritative_channels(channel_names: list[str]) -> None:
    """Fail closed before starting a legacy or unshareable Channel composition."""

    unsupported = sorted(set(channel_names) - AUTHORITATIVE_CHANNELS)
    if unsupported:
        names = ", ".join(unsupported)
        raise RuntimeError(
            f"Channel(s) {names} are disabled until their ControlRuntime and "
            "persistent Delivery Adapter cutover is implemented"
        )
    duplicates = sorted({name for name in channel_names if channel_names.count(name) > 1})
    if duplicates:
        raise RuntimeError(
            "Channel(s) " + ", ".join(duplicates) + " were requested more than once"
        )


def _require_started_channels(
    requested: list[str],
    running: list[str],
) -> None:
    """Reject a partially started Channel set before advertising health."""

    missing = sorted(set(requested) - set(running))
    if missing:
        raise RuntimeError("Channel startup failed for: " + ", ".join(missing))


async def _compose_shared_control_runtime(manager):
    """Build, start and inject the one ControlRuntime every Channel shares.

    Ordering matters. Every binding is collected before the runtime is built,
    so a Channel that cannot authenticate fails the whole start rather than
    leaving a half-composed control plane. The runtime is started before it is
    injected, so no Channel can submit a Turn into an unstarted runtime.
    """

    import asyncio

    from omicsclaw.control import ControlRuntime
    from omicsclaw.runtime.agent import state as core

    channels = list(manager.channels.values())
    bindings = []
    try:
        for channel in channels:
            binding = await channel.prepare_control_binding()
            if binding is None:
                raise RuntimeError(
                    f"Channel '{channel.name}' produced no control binding; a "
                    "cut-over Channel must describe its Adapter and Owner scope"
                )
            bindings.append(binding)
    except BaseException:
        for channel in channels:
            with suppress(Exception):
                await channel.stop()
        raise

    runtime = None
    try:
        runtime = ControlRuntime.for_channel_surfaces(
            workspace_id=str(core.DATA_DIR),
            bindings=tuple(bindings),
        )
        await runtime.start()
        loop = asyncio.get_running_loop()
        for channel in channels:
            channel.bind_control_runtime(runtime, loop=loop)
    except BaseException:
        # Composition is all-or-nothing. Every prepared Channel is released and
        # the partially built runtime is closed, because `control.db` holds an
        # exclusive lifetime lock: leaving it open would make a retry -- or any
        # other Backend process -- unable to acquire the control plane at all.
        if runtime is not None:
            with suppress(Exception):
                await runtime.close()
        for channel in channels:
            with suppress(Exception):
                await channel.stop()
        raise
    logger.info(
        "Shared ControlRuntime composed for %d Channel Adapter(s)", len(bindings)
    )
    return runtime


async def _stop_channels_then_close_runtime(manager, runtime) -> None:
    """Close the shared runtime only after every provider transport stops."""

    await manager.stop_all()
    await runtime.close()


async def _run_channels(channel_names: list[str], health_port: int = 0) -> None:
    """Start and run the specified channels."""
    from omicsclaw.surfaces.channels.manager import ChannelManager

    _require_authoritative_channels(channel_names)

    # Initialize core LLM engine
    provider, base_url, model, api_key, auth_mode, ccproxy_port = (
        _resolve_bootstrap_llm_config()
    )
    # OAuth mode doesn't need an API key — ccproxy supplies the OAuth token.
    if not api_key and provider != "ollama" and auth_mode != "oauth":
        print(
            "Error: no LLM API key resolved. Set LLM_API_KEY or a provider-specific key "
            "(for example DEEPSEEK_API_KEY). See bot/README.md for setup."
        )
        sys.exit(1)

    # Bootstrap context: if OAuth setup fails, warn and degrade to
    # api_key mode rather than blocking bot startup entirely.
    core.init(
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider=provider,
        auth_mode=auth_mode,
        ccproxy_port=ccproxy_port,
        strict_oauth=False,
    )

    manager = ChannelManager()

    # Build and register requested channels
    for name in channel_names:
        if name not in CHANNEL_BUILDERS:
            print(
                f"Error: Unknown channel '{name}'. Available: {', '.join(sorted(CHANNEL_BUILDERS))}"
            )
            sys.exit(1)
        try:
            channel = CHANNEL_BUILDERS[name]()
            manager.register(channel)
            logger.info(f"Built channel: {name}")
        except Exception as e:
            print(f"Error building channel '{name}': {e}")
            sys.exit(1)

    # One Backend process owns exactly one control plane: `control.db` takes an
    # exclusive lifetime lock, so the ControlRuntime is composed HERE from every
    # Channel's binding rather than by each Channel. Phase 1 authenticates each
    # provider and collects bindings; the shared runtime is then started and
    # injected before any Channel begins receiving events in phase 2.
    control_runtime = await _compose_shared_control_runtime(manager)
    try:
        await manager.start_all()
        running = manager.running_channels()
        _require_started_channels(channel_names, running)
        # The health server binds inside this guard on purpose: a port already
        # in use must tear the process down the same way a failed Channel does,
        # rather than leaking the exclusive `control.db` lock and every started
        # Channel's provider connection.
        if health_port > 0:
            await manager.start_health_server(port=health_port)
    except BaseException:
        await _stop_channels_then_close_runtime(manager, control_runtime)
        raise

    print(
        f"OmicsClaw bot running with {len(running)} channel(s): "
        f"{', '.join(running)}. Press Ctrl+C to stop."
    )
    core.audit(
        "multi_channel_start",
        channels=running,
        provider=core.LLM_PROVIDER_NAME,
        model=core.OMICSCLAW_MODEL,
    )

    # Run until interrupted
    try:
        await manager.run()
    except KeyboardInterrupt:
        pass
    finally:
        # The runner owns the shared control plane, so it closes it after every
        # Channel has stopped producing Turns -- letting the Delivery Pump drain
        # its in-flight Attempt rather than being recovered as `unknown`.
        await _stop_channels_then_close_runtime(manager, control_runtime)
        print("OmicsClaw bot stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="OmicsClaw Multi-Channel Bot Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m omicsclaw.surfaces.channels.__main__ --channels telegram\n"
            "  python -m omicsclaw.surfaces.channels.__main__ --channels telegram --health-port 8080\n"
            "  python -m omicsclaw.surfaces.channels.__main__ --list\n"
        ),
    )
    parser.add_argument(
        "--channels",
        "-c",
        type=str,
        default="",
        help="Comma-separated list of channels to start",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=0,
        help="Port for HTTP health-check endpoint (0 = disabled)",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available channels and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.list:
        print("Available channels:")
        for name in sorted(CHANNEL_BUILDERS):
            cls_name = CHANNEL_REGISTRY[name][1]
            status = (
                "authoritative"
                if name in AUTHORITATIVE_CHANNELS
                else "disabled pending cutover"
            )
            print(f"  {name:12s} -> {cls_name} [{status}]")
        sys.exit(0)

    if not args.channels:
        parser.print_help()
        print("\nError: --channels is required (e.g. --channels telegram)")
        sys.exit(1)

    channel_names = [c.strip() for c in args.channels.split(",") if c.strip()]
    if not channel_names:
        print("Error: No channels specified")
        sys.exit(1)

    try:
        asyncio.run(_run_channels(channel_names, args.health_port))
    except RuntimeError as error:
        print(f"Error: {error}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
