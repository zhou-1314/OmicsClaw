from __future__ import annotations

from bot import run
from omicsclaw.core.provider_registry import PROVIDER_PRESETS


def test_resolve_bootstrap_llm_config_uses_provider_specific_key():
    provider, base_url, model, api_key = run._resolve_bootstrap_llm_config(
        {
            "DEEPSEEK_API_KEY": "deepseek-key",
        }
    )

    assert provider == "deepseek"
    assert base_url == PROVIDER_PRESETS["deepseek"][0]
    assert model == PROVIDER_PRESETS["deepseek"][1]
    assert api_key == "deepseek-key"


def test_resolve_bootstrap_llm_config_respects_explicit_custom_endpoint():
    provider, base_url, model, api_key = run._resolve_bootstrap_llm_config(
        {
            "LLM_PROVIDER": "custom",
            "LLM_BASE_URL": "https://llm.internal.example/v1",
            "OMICSCLAW_MODEL": "omics-model",
            "LLM_API_KEY": "generic-key",
        }
    )

    assert provider == "custom"
    assert base_url == "https://llm.internal.example/v1"
    assert model == "omics-model"
    assert api_key == "generic-key"
