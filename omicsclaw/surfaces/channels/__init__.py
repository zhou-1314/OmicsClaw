"""OmicsClaw Multi-Channel communication framework.

Provides an extensible interface for different messaging channels
(Telegram, Feishu, DingTalk, Discord, Slack, WeChat, etc.)
to communicate with the OmicsClaw LLM engine.

Architecture:
    Channel handler → ``runtime.agent.dispatcher.dispatch`` → reply via channel.send

Each channel iterates ``dispatch(envelope)`` from its platform handler
(per ADR 0006). Cross-cutting concerns (rate limit, dedup, audit) live
in ``omicsclaw.runtime.agent.state`` / ``omicsclaw.services.rate_limit``
rather than a separate middleware pipeline.

Components:
    - Channel ABC:          base interface for all channels
    - ChannelCapabilities:  feature declaration per channel
    - ChannelManager:       multi-channel lifecycle + health
    - run.py:               CLI entry ``python -m omicsclaw.surfaces.channels.__main__ --channels telegram,feishu``
"""

from .base import Channel, chunk_text, DedupCache, RateLimiter, TypingManager
from .capabilities import ChannelCapabilities
from .config import BaseChannelConfig

# Channel registry: maps channel name → (module_path, class_name)
# Channels are lazy-imported to avoid pulling in platform SDKs unless needed.
CHANNEL_REGISTRY: dict[str, tuple[str, str]] = {
    "telegram": ("omicsclaw.surfaces.channels.telegram",  "TelegramChannel"),
    "feishu":   ("omicsclaw.surfaces.channels.feishu",    "FeishuChannel"),
    "dingtalk": ("omicsclaw.surfaces.channels.dingtalk",  "DingTalkChannel"),
    "discord":  ("omicsclaw.surfaces.channels.discord",   "DiscordChannel"),
    "slack":    ("omicsclaw.surfaces.channels.slack",     "SlackChannel"),
    "wechat":   ("omicsclaw.surfaces.channels.wechat",    "WeChatChannel"),
    "qq":       ("omicsclaw.surfaces.channels.qq",        "QQChannel"),
    "email":    ("omicsclaw.surfaces.channels.email",     "EmailChannel"),
    "imessage": ("omicsclaw.surfaces.channels.imessage",  "IMessageChannel"),
}


def get_channel_class(name: str) -> type:
    """Dynamically import and return a Channel subclass by name.

    Raises:
        KeyError: If the channel name is not registered.
        ImportError: If the channel module cannot be imported.
    """
    if name not in CHANNEL_REGISTRY:
        raise KeyError(
            f"Unknown channel: {name!r}. "
            f"Available: {', '.join(sorted(CHANNEL_REGISTRY))}"
        )
    module_path, class_name = CHANNEL_REGISTRY[name]
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


__all__ = [
    # Core abstractions
    "Channel",
    "ChannelCapabilities",
    "BaseChannelConfig",
    # Registry
    "CHANNEL_REGISTRY",
    "get_channel_class",
    # Utilities
    "chunk_text",
    "DedupCache",
    "RateLimiter",
    "TypingManager",
]
