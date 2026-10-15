"""One shared live provider test, independent of settings files and HTTP."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from omicsclaw.provider import ProviderConfig, provider_for
from omicsclaw.schema import Message, Role

_log = logging.getLogger(__name__)


async def probe_provider(
    config: ProviderConfig,
    *,
    provider_factory: Callable[[ProviderConfig], Any] = provider_for,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    messages = (
        Message(role=Role.SYSTEM, content="Reply with the single word: pong."),
        Message(role=Role.USER, content="ping"),
    )
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            adapter = provider_factory(config)
            try:
                reply: Awaitable[Any] = adapter.generate(messages, None)
                await reply
            finally:
                await _close(adapter)
    except Exception as exc:  # noqa: BLE001 - every failure is a result
        return {
            "ok": False,
            "message": _scrub(str(exc) or type(exc).__name__, config.api_key),
            "detail": _failure_detail(exc),
            "duration_ms": _elapsed_ms(started),
        }
    return {
        "ok": True,
        "message": "Live provider test passed.",
        "provider": config.provider,
        "model": config.model,
        "duration_ms": _elapsed_ms(started),
    }


def _scrub(text: str, secret: str) -> str:
    if len(secret) >= 8:
        text = text.replace(secret, "…")
    return text[:300]


def _failure_detail(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return f"{name} (HTTP {status})" if isinstance(status, int) else name


async def _close(adapter: Any) -> None:
    """Close *adapter* through its ``aclose()`` or ``close()``, when it has one."""
    closer = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 - closing never decides the result
        _log.debug("closing the test adapter failed: %s", type(exc).__name__)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
