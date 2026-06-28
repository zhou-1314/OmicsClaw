"""Token pricing + per-process usage accumulator.

Carved out of ``bot/core.py`` per ADR 0001. Module owns the price table and
the running counters; ``omicsclaw.runtime.agent.state`` provides a zero-arg wrapper around
``get_usage_snapshot`` that fills in the active model + provider from its
own globals.
"""

from __future__ import annotations

import os


_usage: dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cached_input_tokens": 0,  # subset of prompt_tokens served from a prompt cache
    "api_calls": 0,
}


def _cache_read_discount() -> float:
    """Price multiplier for cache-READ input tokens vs a fresh-input token.

    Cache reads cost a fraction of a miss (DeepSeek/Anthropic ~0.1; OpenAI ~0.25–0.5).
    Billing cached input at FULL price overcharges cache-heavy sessions ~5–10x and
    contradicts the cache_hit_ratio surfaced alongside. Default 0.1 (the common
    floor); override via ``LLM_CACHE_READ_DISCOUNT``. Approximate — the price table
    itself is approximate (see module docstring)."""
    try:
        v = float(os.environ["LLM_CACHE_READ_DISCOUNT"])
        return min(1.0, max(0.0, v))
    except (KeyError, ValueError, TypeError):
        return 0.1


# Module-level constant for callers that want the default without the env read
# (e.g. tests, the desktop server's parallel cost calc). Env override still wins
# at compute time via _cache_read_discount().
CACHE_READ_DISCOUNT: float = 0.1


# Approximate pricing per 1M tokens (USD, input/output) — keyed by a substring
# of the (lower-cased) model id. Override via LLM_INPUT_PRICE / LLM_OUTPUT_PRICE
# env vars. Missing models fall through to (0.0, 0.0), which the chat UI treats
# as "no estimate available" and omits from the cost line.
_TOKEN_PRICES: dict[str, tuple[float, float]] = {
    # DeepSeek
    "deepseek-v4-flash":     (0.27,  1.10),
    "deepseek-v4-pro":       (0.55,  2.19),
    "deepseek-reasoner":    (0.55,  2.19),
    "deepseek-chat":        (0.27,  1.10),
    "deepseek-v3":          (0.27,  1.10),
    # Anthropic Claude
    "claude-opus-4":        (15.00, 75.00),
    "claude-sonnet-4":      (3.00, 15.00),
    "claude-haiku-4":       (1.00,  5.00),
    "claude-3-5-sonnet":    (3.00, 15.00),
    "claude-3-5-haiku":     (0.80,  4.00),
    "claude-3-opus":        (15.00, 75.00),
    "claude-3-sonnet":      (3.00, 15.00),
    "claude-3-haiku":       (0.25,  1.25),
    # OpenAI
    "gpt-5.4-mini":         (0.25,  2.00),
    "gpt-5.4":              (2.50, 10.00),
    "gpt-5.3-codex":        (2.50, 10.00),
    "gpt-5-mini":           (0.25,  2.00),
    "gpt-5":                (2.50, 10.00),
    "gpt-4.1-mini":         (0.40,  1.60),
    "gpt-4.1":              (2.00,  8.00),
    "o4-mini":              (1.10,  4.40),
    "o3-mini":              (1.10,  4.40),
    "gpt-4o-mini":          (0.15,  0.60),
    "gpt-4o":               (2.50, 10.00),
    "gpt-4-turbo":          (10.00, 30.00),
    "gpt-3.5-turbo":        (0.50,  1.50),
    # Google Gemini
    "gemini-3.1-pro":       (1.25, 10.00),
    "gemini-3-flash":       (0.15,  0.60),
    "gemini-2.5-pro":       (1.25, 10.00),
    "gemini-2.5-flash":     (0.15,  0.60),
    "gemini-2.0-flash":     (0.10,  0.40),
    "gemini-1.5-pro":       (1.25,  5.00),
    "gemini-1.5-flash":     (0.075, 0.30),
    # Moonshot / Kimi
    "kimi-k2-thinking":     (0.60,  2.50),
    "kimi-k2":              (0.60,  2.50),
    "moonshot-v1":          (0.30,  1.00),
    # Zhipu GLM
    "glm-5":                (0.60,  2.20),
    "glm-4.7":              (0.50,  1.80),
    "glm-4":                (0.50,  1.50),
    # Doubao (Volcengine)
    "doubao-seed-2":        (0.40,  1.00),
    "doubao-1.5-pro":       (0.50,  1.20),
    # Qwen / DashScope
    "qwen3-coder-plus":     (0.30,  1.20),
    "qwq-plus":             (0.35,  1.40),
    "qwen-max":             (1.60,  6.40),
    "qwen-plus":            (0.40,  1.20),
    "qwen-long":            (0.05,  0.20),
}


# Longest-first so sub-version ids (e.g. "gpt-5.4-mini") match their specific
# entry before falling back to a shorter family prefix (e.g. "gpt-5").
_TOKEN_PRICE_KEYS_BY_LENGTH: tuple[str, ...] = tuple(
    sorted(_TOKEN_PRICES, key=len, reverse=True)
)


def _get_token_price(model: str) -> tuple[float, float]:
    """Return (input_price, output_price) per 1M tokens for the given model."""
    try:
        return (
            float(os.environ["LLM_INPUT_PRICE"]),
            float(os.environ["LLM_OUTPUT_PRICE"]),
        )
    except (KeyError, ValueError, TypeError):
        pass
    model_lower = model.lower()
    for key in _TOKEN_PRICE_KEYS_BY_LENGTH:
        if key in model_lower:
            return _TOKEN_PRICES[key]
    return (0.0, 0.0)


def get_token_price(model: str) -> tuple[float, float]:
    """Public alias for ``_get_token_price``."""
    return _get_token_price(model)


def accumulate_usage(response_usage) -> dict[str, int]:
    """Add API response usage to the running counters; return per-call delta."""
    if response_usage is None:
        return {}
    # Cache-READ subtotal (a subset of prompt_tokens). Provider shapes vary:
    #   OpenAI:    prompt_tokens_details.cached_tokens
    #   DeepSeek:  prompt_cache_hit_tokens (top-level)
    #   Anthropic: cache_read_input_tokens (top-level)
    prompt_details = getattr(response_usage, "prompt_tokens_details", None)
    cached = int(getattr(prompt_details, "cached_tokens", 0) or 0)
    if not cached:
        cached = int(getattr(response_usage, "prompt_cache_hit_tokens", 0) or 0)
    if not cached:
        cached = int(getattr(response_usage, "cache_read_input_tokens", 0) or 0)

    delta = {
        "prompt_tokens":     getattr(response_usage, "prompt_tokens",     0) or 0,
        "completion_tokens": getattr(response_usage, "completion_tokens", 0) or 0,
        "total_tokens":      getattr(response_usage, "total_tokens",      0) or 0,
    }
    _usage["prompt_tokens"]       += delta["prompt_tokens"]
    _usage["completion_tokens"]   += delta["completion_tokens"]
    _usage["total_tokens"]        += delta["total_tokens"]
    _usage["cached_input_tokens"] += cached
    _usage["api_calls"]           += 1
    return delta


def reset_usage() -> None:
    """Reset session-level usage counters to zero."""
    for k in _usage:
        _usage[k] = 0


def get_usage_snapshot(model: str = "", provider: str = "") -> dict:
    """Snapshot of the running counters plus a cost estimate computed from
    the provided ``model`` (price table lookup). Callers that want the
    active bot context should use ``omicsclaw.runtime.agent.state.get_usage_snapshot()`` which
    fills in ``OMICSCLAW_MODEL`` / ``LLM_PROVIDER_NAME`` automatically."""
    inp_price, out_price = _get_token_price(model)
    # F: bill cache-READ input at a discount, fresh input at full price — else a
    # cache-heavy session is overcharged ~5–10x (and contradicts cache_hit_ratio).
    cached = _usage.get("cached_input_tokens", 0)
    fresh_input = max(0, _usage["prompt_tokens"] - cached)
    cost = (
        fresh_input                 / 1_000_000 * inp_price +
        cached                      / 1_000_000 * inp_price * _cache_read_discount() +
        _usage["completion_tokens"] / 1_000_000 * out_price
    )
    return {
        **_usage,
        "model": model,
        "provider": provider,
        "input_price_per_1m":  inp_price,
        "output_price_per_1m": out_price,
        "estimated_cost_usd":  round(cost, 6),
    }
