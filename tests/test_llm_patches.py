"""Tests for omicsclaw.providers.patches — runtime helpers."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx

from omicsclaw.providers.patches import (
    apply_deepseek_reasoning_passback,
    discover_ollama_models,
    discover_ollama_models_async,
    model_supports_tools_ollama,
)


# ---------------------------------------------------------------------------
# DeepSeek passback
# ---------------------------------------------------------------------------


class TestDeepseekPassback:
    def test_injects_empty_string_when_assistant_lacks_reasoning_content(self):
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ]
        out = apply_deepseek_reasoning_passback(messages)
        assert out[0]["role"] == "system"
        assert "reasoning_content" not in out[0]
        assert out[1]["role"] == "user"
        assert "reasoning_content" not in out[1]
        assert out[2]["role"] == "assistant"
        assert out[2]["reasoning_content"] == ""
        assert out[3]["role"] == "user"
        assert "reasoning_content" not in out[3]

    def test_preserves_existing_reasoning_content(self):
        messages = [
            {
                "role": "assistant",
                "content": "result",
                "reasoning_content": "I thought about it",
            }
        ]
        out = apply_deepseek_reasoning_passback(messages)
        assert out[0]["reasoning_content"] == "I thought about it"

    def test_skips_user_and_system_roles(self):
        messages = [
            {"role": "system", "content": "hi"},
            {"role": "user", "content": "ping"},
        ]
        out = apply_deepseek_reasoning_passback(messages)
        for m in out:
            assert "reasoning_content" not in m

    def test_idempotent(self):
        messages = [{"role": "assistant", "content": "x"}]
        first = apply_deepseek_reasoning_passback(messages)
        second = apply_deepseek_reasoning_passback(first)
        assert second == first

    def test_returns_new_list_does_not_mutate_caller_dicts(self):
        messages = [{"role": "assistant", "content": "x"}]
        out = apply_deepseek_reasoning_passback(messages)
        assert out is not messages
        # Original dict should not be mutated
        assert "reasoning_content" not in messages[0]

    def test_handles_empty_list(self):
        assert apply_deepseek_reasoning_passback([]) == []

    def test_handles_non_dict_items_passthrough(self):
        # Unknown items survive untouched
        weird = ["not-a-dict", {"role": "assistant", "content": "ok"}]
        out = apply_deepseek_reasoning_passback(weird)  # type: ignore[arg-type]
        assert out[0] == "not-a-dict"
        assert out[1]["reasoning_content"] == ""


# ---------------------------------------------------------------------------
# Ollama discovery (sync)
# ---------------------------------------------------------------------------


class TestDiscoverOllamaModelsSync:
    def test_returns_models_on_200(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {
            "models": [{"name": "qwen2.5:7b"}, {"name": "llama3.3:70b"}]
        }
        with patch("httpx.get", return_value=fake_resp):
            assert discover_ollama_models("http://127.0.0.1:11434") == [
                "qwen2.5:7b",
                "llama3.3:70b",
            ]

    def test_returns_empty_on_404(self):
        fake_resp = MagicMock(status_code=404)
        with patch("httpx.get", return_value=fake_resp):
            assert discover_ollama_models("http://127.0.0.1:11434") == []

    def test_returns_empty_on_connection_error(self):
        with patch("httpx.get", side_effect=httpx.ConnectError("nope")):
            assert discover_ollama_models("http://127.0.0.1:11434") == []

    def test_returns_empty_on_malformed_json(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.side_effect = ValueError("bad json")
        with patch("httpx.get", return_value=fake_resp):
            assert discover_ollama_models("http://127.0.0.1:11434") == []

    def test_no_url_returns_empty(self):
        assert discover_ollama_models("") == []
        assert discover_ollama_models(None) == []  # type: ignore[arg-type]

    def test_strips_trailing_slash(self):
        captured = {}

        def fake_get(url, *args, **kwargs):
            captured["url"] = url
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"models": []}
            return resp

        with patch("httpx.get", side_effect=fake_get):
            discover_ollama_models("http://127.0.0.1:11434/")
        assert captured["url"] == "http://127.0.0.1:11434/api/tags"


# ---------------------------------------------------------------------------
# Ollama discovery (async)
# ---------------------------------------------------------------------------


class TestDiscoverOllamaModelsAsync:
    def test_no_url_returns_empty(self):
        assert asyncio.run(discover_ollama_models_async("")) == []

    def test_returns_empty_on_exception(self):
        async def fake_get(*args, **kwargs):
            raise httpx.ConnectError("nope")

        client = MagicMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        client.get = fake_get

        with patch("httpx.AsyncClient", return_value=client):
            result = asyncio.run(
                discover_ollama_models_async("http://127.0.0.1:11434")
            )
        assert result == []

    def test_returns_models_on_200(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {
            "models": [{"name": "qwen2.5:7b"}, {"name": "llama3.3:70b"}]
        }

        async def fake_get(*args, **kwargs):
            return fake_resp

        client = MagicMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        client.get = fake_get

        with patch("httpx.AsyncClient", return_value=client):
            result = asyncio.run(
                discover_ollama_models_async("http://127.0.0.1:11434")
            )
        assert result == ["qwen2.5:7b", "llama3.3:70b"]


# ---------------------------------------------------------------------------
# Ollama tool-capability classification
# ---------------------------------------------------------------------------


class TestModelSupportsToolsOllama:
    def test_qwen2_5_family_capable(self):
        assert model_supports_tools_ollama("qwen2.5:7b") is True
        assert model_supports_tools_ollama("qwen2.5:14b") is True
        assert model_supports_tools_ollama("qwen2.5:32b") is True
        assert model_supports_tools_ollama("qwen2.5-coder:7b") is True

    def test_qwen3_capable(self):
        assert model_supports_tools_ollama("qwen3:8b") is True
        assert model_supports_tools_ollama("qwen3-coder:latest") is True

    def test_qwen2_base_incapable(self):
        # qwen2 (without .5) lacks tool support; do not confuse with qwen2.5.
        assert model_supports_tools_ollama("qwen2:7b") is False

    def test_deepseek_r1_incapable(self):
        # The issue #208 reproducer.
        assert model_supports_tools_ollama("deepseek-r1:14b") is False
        assert model_supports_tools_ollama("deepseek-r1:7b") is False
        assert model_supports_tools_ollama("deepseek-r1:32b") is False

    def test_deepseek_r1_with_registry_prefix(self):
        # Matches the error string Ollama actually returns to the user.
        assert (
            model_supports_tools_ollama("registry.ollama.ai/library/deepseek-r1:14b")
            is False
        )

    def test_gemma_family_incapable(self):
        assert model_supports_tools_ollama("gemma3:4b") is False
        assert model_supports_tools_ollama("gemma3:12b") is False
        assert model_supports_tools_ollama("gemma2:9b") is False

    def test_gemma4_capable(self):
        # Gemma 4 (2026-04) added native function calling — must not be
        # confused with the still-incapable gemma2 / gemma3 families.
        assert model_supports_tools_ollama("gemma4:e4b") is True
        assert model_supports_tools_ollama("gemma4:e2b") is True
        assert model_supports_tools_ollama("gemma4:26b") is True
        assert model_supports_tools_ollama("gemma4:31b") is True
        assert model_supports_tools_ollama("gemma4:latest") is True

    def test_llama3_minor_versions_capable(self):
        assert model_supports_tools_ollama("llama3.1:8b") is True
        assert model_supports_tools_ollama("llama3.2:3b") is True
        assert model_supports_tools_ollama("llama3.3:70b") is True

    def test_llama3_base_incapable(self):
        # llama3 (no minor) is a base text model without tool support.
        assert model_supports_tools_ollama("llama3:8b") is False

    def test_mistral_family_capable(self):
        assert model_supports_tools_ollama("mistral:latest") is True
        assert model_supports_tools_ollama("mistral-nemo:12b") is True
        assert model_supports_tools_ollama("mixtral:8x7b") is True

    def test_embedding_models_incapable(self):
        assert model_supports_tools_ollama("nomic-embed-text:latest") is False
        assert model_supports_tools_ollama("mxbai-embed-large:latest") is False

    def test_unknown_model_returns_none(self):
        assert model_supports_tools_ollama("brand-new-model:7b") is None
        assert model_supports_tools_ollama("some-untracked-thing") is None

    def test_handles_invalid_input(self):
        assert model_supports_tools_ollama("") is None
        assert model_supports_tools_ollama(None) is None  # type: ignore[arg-type]
        assert model_supports_tools_ollama(":no-name") is None
