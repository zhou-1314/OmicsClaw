from __future__ import annotations

import httpx
import pytest

import bot.core as core
from omicsclaw.agents.pipeline import ResearchPipeline
from omicsclaw.core.provider_registry import PROVIDER_PRESETS


def test_build_llm_timeout_uses_defaults(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS", raising=False)

    timeout = core._build_llm_timeout()

    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == core.DEFAULT_LLM_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == core.DEFAULT_LLM_TIMEOUT_SECONDS
    assert timeout.write == core.DEFAULT_LLM_TIMEOUT_SECONDS
    assert timeout.pool == core.DEFAULT_LLM_TIMEOUT_SECONDS


def test_build_llm_timeout_respects_env(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_LLM_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS", "3")

    timeout = core._build_llm_timeout()

    assert timeout.connect == 3.0
    assert timeout.read == 45.0
    assert timeout.write == 45.0
    assert timeout.pool == 45.0


def test_init_passes_timeout_to_async_openai(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(core, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setenv("OMICSCLAW_MEMORY_ENABLED", "false")
    monkeypatch.setenv("OMICSCLAW_LLM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS", "5")

    core.init(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test-model",
        provider="custom",
    )

    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://example.com/v1"
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 5.0
    assert captured["timeout"].read == 30.0


def test_pipeline_openai_path_receives_shared_timeout(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    pipeline = object.__new__(ResearchPipeline)
    pipeline.provider = "deepseek"
    pipeline.model = ""

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setenv("OMICSCLAW_LLM_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS", "4")

    llm = ResearchPipeline._get_llm(pipeline)

    assert isinstance(llm, _FakeChatOpenAI)
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 4.0
    assert captured["timeout"].read == 25.0
    assert captured["openai_api_base"] == PROVIDER_PRESETS["deepseek"][0]


def test_pipeline_anthropic_path_receives_total_timeout(monkeypatch):
    langchain_anthropic = pytest.importorskip("langchain_anthropic")
    captured: dict[str, object] = {}

    class _FakeChatAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    pipeline = object.__new__(ResearchPipeline)
    pipeline.provider = "anthropic"
    pipeline.model = ""

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", _FakeChatAnthropic)
    monkeypatch.setenv("OMICSCLAW_LLM_TIMEOUT_SECONDS", "40")
    monkeypatch.setenv("OMICSCLAW_LLM_CONNECT_TIMEOUT_SECONDS", "7")

    llm = ResearchPipeline._get_llm(pipeline)

    assert isinstance(llm, _FakeChatAnthropic)
    assert captured["timeout"] == 40.0
