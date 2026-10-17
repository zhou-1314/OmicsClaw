"""Expose model provider interfaces and adapters.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from ._model_limits import DEFAULT_MODEL_LIMITS, ModelLimits, get_model_limits
from .anthropic_provider import AnthropicProvider
from .base import Completion, LLMProvider, ProviderDeadlineExceeded, ProviderError
from .config import (
    DETECT_ORDER,
    PRESETS,
    ProviderConfig,
    ProviderDialect,
    ProviderPreset,
    ProviderTier,
    detect_provider_from_env,
    dialect_for,
    preset_for,
    resolve_config,
)
from .factory import provider_for, provider_from_env
from .openai_provider import OpenAIProvider

__all__ = [
    "DEFAULT_MODEL_LIMITS",
    "DETECT_ORDER",
    "PRESETS",
    "AnthropicProvider",
    "Completion",
    "LLMProvider",
    "ModelLimits",
    "OpenAIProvider",
    "ProviderConfig",
    "ProviderDeadlineExceeded",
    "ProviderDialect",
    "ProviderError",
    "ProviderPreset",
    "ProviderTier",
    "detect_provider_from_env",
    "dialect_for",
    "get_model_limits",
    "preset_for",
    "provider_for",
    "provider_from_env",
    "resolve_config",
]
