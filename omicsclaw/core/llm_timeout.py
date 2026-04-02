from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

DEFAULT_LLM_TIMEOUT_SECONDS = 120.0
DEFAULT_LLM_CONNECT_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger(__name__)


def _coerce_timeout_seconds(
    env_name: str,
    default: float,
    *,
    log: logging.Logger | None = None,
) -> float:
    raw = str(os.environ.get(env_name, "") or "").strip()
    if not raw:
        return float(default)

    active_logger = log or logger
    try:
        value = float(raw)
    except ValueError:
        active_logger.warning(
            "%s=%r is invalid; using default %.1fs",
            env_name,
            raw,
            default,
        )
        return float(default)

    if value <= 0:
        active_logger.warning(
            "%s=%r must be > 0; using default %.1fs",
            env_name,
            raw,
            default,
        )
        return float(default)

    return value


@dataclass(frozen=True)
class LLMTimeoutPolicy:
    total_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    connect_seconds: float = DEFAULT_LLM_CONNECT_TIMEOUT_SECONDS

    def as_httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.total_seconds, connect=self.connect_seconds)

    def as_anthropic_timeout(self) -> float:
        return self.total_seconds


def build_llm_timeout_policy(*, log: logging.Logger | None = None) -> LLMTimeoutPolicy:
    return LLMTimeoutPolicy(
        total_seconds=_coerce_timeout_seconds(
            "OMICSCLAW_LLM_TIMEOUT_SECONDS",
            DEFAULT_LLM_TIMEOUT_SECONDS,
            log=log,
        ),
        connect_seconds=_coerce_timeout_seconds(
            "OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS",
            DEFAULT_LLM_CONNECT_TIMEOUT_SECONDS,
            log=log,
        ),
    )
