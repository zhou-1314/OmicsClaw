"""Resolve model context and output limits.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

_UNKNOWN_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """One model's context window and single-response output ceiling."""

    context_tokens: int
    output_tokens: int = _UNKNOWN_OUTPUT_TOKENS


DEFAULT_MODEL_LIMITS = ModelLimits(context_tokens=256_000, output_tokens=8_192)
"""Returned for anything absent from :data:`KNOWN_MODEL_LIMITS`."""


_KNOWN: dict[str, ModelLimits] = {
    # --- Anthropic Claude 4.x (native spelling: 4-6; gateway spelling: 4.6) -
    "claude-opus-4-7": ModelLimits(1_000_000, 32_000),
    "claude-opus-4.7": ModelLimits(1_000_000, 32_000),
    "claude-opus-4-6": ModelLimits(1_000_000, 32_000),
    "claude-sonnet-4-6": ModelLimits(1_000_000, 64_000),
    "claude-sonnet-4.6": ModelLimits(1_000_000, 64_000),
    "claude-sonnet-4-5": ModelLimits(200_000, 64_000),
    "claude-haiku-4-5": ModelLimits(200_000, 8_192),
    "claude-haiku-4-5-20251001": ModelLimits(200_000, 8_192),
    # --- Anthropic Claude 3.x -----------------------------------------------
    "claude-3-5-sonnet-20241022": ModelLimits(200_000, 8_192),
    "claude-3-5-haiku-20241022": ModelLimits(200_000, 8_192),
    "claude-3-opus-20240229": ModelLimits(200_000, 4_096),
    # --- OpenAI GPT-5.x -----------------------------------------------------
    "gpt-5.5-pro": ModelLimits(1_050_000),
    "gpt-5.5": ModelLimits(1_050_000),
    "gpt-5.4": ModelLimits(1_050_000),
    "gpt-5.4-mini": ModelLimits(400_000),
    "gpt-5.3-codex": ModelLimits(400_000),
    "gpt-5": ModelLimits(400_000),
    "gpt-5-mini": ModelLimits(400_000),
    # --- OpenAI GPT-4.x and o-series ----------------------------------------
    "gpt-4o": ModelLimits(128_000, 16_384),
    "gpt-4o-mini": ModelLimits(128_000, 16_384),
    "gpt-4.1": ModelLimits(1_047_576, 32_768),
    "gpt-4.1-mini": ModelLimits(1_047_576, 32_768),
    "gpt-4.1-nano": ModelLimits(1_047_576, 32_768),
    "gpt-4-turbo": ModelLimits(128_000, 4_096),
    "o3": ModelLimits(200_000, 100_000),
    "o4-mini": ModelLimits(200_000, 100_000),
    # --- DeepSeek -----------------------------------------------------------
    "deepseek-v4-pro": ModelLimits(1_000_000),
    "deepseek-v4-flash": ModelLimits(1_000_000),
    "deepseek-chat": ModelLimits(1_000_000),
    "deepseek-reasoner": ModelLimits(1_000_000),
    "deepseek-v3.2": ModelLimits(131_072),
    "deepseek-v3": ModelLimits(64_000, 8_000),
    "deepseek-r1": ModelLimits(64_000, 8_000),
    # --- Google Gemini ------------------------------------------------------
    "gemini-3.1-pro-preview": ModelLimits(1_048_576),
    "gemini-3-flash-preview": ModelLimits(1_048_576),
    "gemini-2.5-pro": ModelLimits(1_048_576, 65_536),
    "gemini-2.5-flash": ModelLimits(1_048_576),
    "gemini-2.0-flash": ModelLimits(1_048_576, 8_192),
    # --- NVIDIA NIM ---------------------------------------------------------
    "nemotron-3-super-120b-a12b": ModelLimits(1_000_000),
    "qwen3.5-397b-a17b": ModelLimits(262_144),
    # --- Alibaba Qwen (DashScope) -------------------------------------------
    "qwen3.7-max": ModelLimits(1_000_000),
    "qwen3.6-plus": ModelLimits(1_000_000),
    "qwen3.6-27b": ModelLimits(262_000),
    "qwen3.6-35b-a3b": ModelLimits(262_000),
    "qwen3-max": ModelLimits(262_144),
    "qwen-max": ModelLimits(262_144),
    "qwen3-coder-plus": ModelLimits(1_000_000),
    "qwen3-235b-a22b": ModelLimits(131_072, 8_192),
    "qwen2.5-72b-instruct": ModelLimits(131_072, 8_192),
    "qwq-plus": ModelLimits(131_072),
    # --- Moonshot Kimi ------------------------------------------------------
    "kimi-k3": ModelLimits(1_048_576, 65_536),
    "kimi-k2.6": ModelLimits(262_144),
    "kimi-k2.5": ModelLimits(262_144),
    "kimi-k2-thinking": ModelLimits(262_144),
    # --- Zhipu / Z.AI GLM (also SiliconFlow's ``Pro/zai-org/`` reselling) ---
    "glm-5.1": ModelLimits(202_752),
    "glm-5": ModelLimits(202_752),
    "glm-5-turbo": ModelLimits(202_752),
    "glm-4.7": ModelLimits(202_752),
    # --- MiniMax ------------------------------------------------------------
    "minimax-m2.7": ModelLimits(196_608),
    "minimax-m2.5": ModelLimits(196_608),
    # --- ByteDance Doubao (Volcengine) --------------------------------------
    "doubao-seed-2-0-pro-260215": ModelLimits(1_000_000),
    "doubao-seed-2-0-lite-260215": ModelLimits(1_000_000),
    "doubao-seed-2-0-code-preview-260215": ModelLimits(1_000_000),
    "doubao-1.5-pro-256k": ModelLimits(256_000),
    "doubao-1.5-thinking-pro": ModelLimits(256_000),
    # --- Ollama (local). Tags keep their ``:suffix``; only ``/`` is stripped -
    "gemma4:e2b": ModelLimits(131_072),
    "gemma4:e4b": ModelLimits(131_072),
    "gemma4:12b": ModelLimits(262_144),
    "gemma4:26b": ModelLimits(262_144),
    "gemma4:31b": ModelLimits(262_144),
}

KNOWN_MODEL_LIMITS: Mapping[str, ModelLimits] = MappingProxyType(_KNOWN)
"""Read-only view of the table, exposed for inspection and tests."""


def bare_model_name(model: str) -> str:
    """Strip any gateway prefix: ``"openai/gpt-4o"`` → ``"gpt-4o"``.

    Lowercased, because the same model reaches us capitalized differently
    by different resellers (``Pro/zai-org/GLM-5`` versus ``glm-5``).
    """
    return str(model or "").strip().lower().rsplit("/", 1)[-1]


def get_model_limits(model: str) -> ModelLimits:
    """Return the limits for ``model``, falling back conservatively.

    Full identifier first, bare name second — so a gateway that publishes
    its own window can have an entry, while everything else inherits the
    vendor's without a per-gateway duplicate.
    """
    full = str(model or "").strip().lower()
    if full in _KNOWN:
        return _KNOWN[full]
    bare = bare_model_name(full)
    if bare in _KNOWN:
        return _KNOWN[bare]
    return DEFAULT_MODEL_LIMITS


__all__ = [
    "DEFAULT_MODEL_LIMITS",
    "KNOWN_MODEL_LIMITS",
    "ModelLimits",
    "bare_model_name",
    "get_model_limits",
]
