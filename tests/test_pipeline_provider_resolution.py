from __future__ import annotations

import pytest

from omicsclaw.agents.pipeline import ResearchPipeline
from omicsclaw.core.provider_registry import PROVIDER_PRESETS


def _build_pipeline(provider: str = "", model: str = "") -> ResearchPipeline:
    pipeline = object.__new__(ResearchPipeline)
    pipeline.provider = provider
    pipeline.model = model
    return pipeline


def _clear_llm_env(monkeypatch) -> None:
    for name in (
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "OMICSCLAW_MODEL",
        "OC_LLM_PROVIDER",
        "OC_LLM_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_pipeline_auto_detects_openai_provider(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    _clear_llm_env(monkeypatch)
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    llm = ResearchPipeline._get_llm(_build_pipeline())

    assert isinstance(llm, _FakeChatOpenAI)
    assert captured["openai_api_key"] == "openai-key"
    assert captured["model"] == PROVIDER_PRESETS["openai"][1]
    assert "openai_api_base" not in captured


def test_pipeline_openrouter_uses_registry_url_and_key(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    _clear_llm_env(monkeypatch)
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")

    llm = ResearchPipeline._get_llm(_build_pipeline(provider="openrouter"))

    assert isinstance(llm, _FakeChatOpenAI)
    assert captured["openai_api_key"] == "openrouter-key"
    assert captured["openai_api_base"] == PROVIDER_PRESETS["openrouter"][0]
    assert captured["model"] == PROVIDER_PRESETS["openrouter"][1]


def test_pipeline_anthropic_uses_provider_base_override(monkeypatch):
    langchain_anthropic = pytest.importorskip("langchain_anthropic")
    _clear_llm_env(monkeypatch)
    captured: dict[str, object] = {}

    class _FakeChatAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", _FakeChatAnthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.example.test/v1")

    llm = ResearchPipeline._get_llm(_build_pipeline(provider="anthropic"))

    assert isinstance(llm, _FakeChatAnthropic)
    assert captured["anthropic_api_key"] == "anthropic-key"
    assert captured["anthropic_api_url"] == "https://anthropic.example.test/v1"
    assert captured["model"] == PROVIDER_PRESETS["anthropic"][1]


def test_pipeline_custom_provider_keeps_global_base_url_override(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    _clear_llm_env(monkeypatch)
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setenv("LLM_API_KEY", "custom-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example.test/v1")

    llm = ResearchPipeline._get_llm(
        _build_pipeline(provider="custom", model="custom-model")
    )

    assert isinstance(llm, _FakeChatOpenAI)
    assert captured["openai_api_key"] == "custom-key"
    assert captured["openai_api_base"] == "https://gateway.example.test/v1"
    assert captured["model"] == "custom-model"


def test_pipeline_delegates_llm_construction_to_provider_factory(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_factory(**kwargs):
        captured.update(kwargs)
        return "llm-sentinel"

    monkeypatch.setattr(
        "omicsclaw.agents.pipeline.get_langchain_llm",
        _fake_factory,
    )
    monkeypatch.setenv("OMICSCLAW_LLM_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS", "6")
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example.test/v1")

    llm = ResearchPipeline._get_llm(_build_pipeline(provider="custom", model="m1"))

    assert llm == "llm-sentinel"
    assert captured["provider"] == "custom"
    assert captured["model"] == "m1"
    assert captured["base_url"] == "https://gateway.example.test/v1"
    assert captured["temperature"] == 0.3
    assert captured["timeout"].connect == 6.0
    assert captured["timeout"].read == 33.0
    assert captured["anthropic_timeout"] == 33.0
