"""Tests for omicsclaw.providers.chat_completion + resolve_chat_endpoint.

The HTTP layer is mocked via monkeypatch on ``requests.post``; environment
variable resolution is exercised against the real ``providers.registry``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# resolve_chat_endpoint                                                       #
# --------------------------------------------------------------------------- #

def test_resolve_chat_endpoint_falls_back_to_openai_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers.runtime import resolve_chat_endpoint

    for var in ("LLM_PROVIDER", "LLM_BASE_URL", "OMICSCLAW_MODEL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    api_key, base_url, model = resolve_chat_endpoint()
    assert base_url == "https://api.openai.com/v1"
    assert model  # some default model string
    assert api_key == ""  # no key configured


def test_resolve_chat_endpoint_returns_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers.runtime import resolve_chat_endpoint

    monkeypatch.setenv("LLM_API_KEY", "sk-test-123")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")

    api_key, base_url, _model = resolve_chat_endpoint()
    assert api_key == "sk-test-123"
    assert base_url == "https://example.invalid/v1"


# --------------------------------------------------------------------------- #
# call_chat_completion                                                        #
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | str = ""):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> dict[str, Any]:
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")


def test_call_chat_completion_returns_content_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers import chat_completion as cc

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setattr(
        "omicsclaw.providers.chat_completion.requests.post",
        lambda *a, **kw: _FakeResponse(
            200, {"choices": [{"message": {"content": "  hello world  "}}]}
        ),
        raising=False,
    )

    result = cc.call_chat_completion("test prompt")
    assert result == "hello world"


def test_call_chat_completion_returns_none_on_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers import chat_completion as cc

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # If we accidentally still hit requests, fail loud:
    monkeypatch.setattr(
        "omicsclaw.providers.chat_completion.requests.post",
        lambda *a, **kw: pytest.fail("requests.post called despite no api key"),
        raising=False,
    )

    assert cc.call_chat_completion("anything") is None


def test_call_chat_completion_returns_none_on_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers import chat_completion as cc

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setattr(
        "omicsclaw.providers.chat_completion.requests.post",
        lambda *a, **kw: _FakeResponse(500, "server exploded"),
        raising=False,
    )

    assert cc.call_chat_completion("prompt") is None


def test_call_chat_completion_returns_none_on_request_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers import chat_completion as cc

    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("omicsclaw.providers.chat_completion.requests.post", boom, raising=False)

    assert cc.call_chat_completion("prompt") is None


def test_call_chat_completion_forwards_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    from omicsclaw.providers import chat_completion as cc

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    captured: dict[str, Any] = {}

    def capture(url, *, headers, json, timeout):  # noqa: A002  (shadow stdlib `json` ok in test)
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("omicsclaw.providers.chat_completion.requests.post", capture, raising=False)

    cc.call_chat_completion("p", temperature=0.7)
    assert captured["json"]["temperature"] == 0.7
    assert captured["url"].endswith("/chat/completions")


# --------------------------------------------------------------------------- #
# backwards-compat alias                                                      #
# --------------------------------------------------------------------------- #

def test_routing_llm_router_still_exposes_resolve_llm_config_alias() -> None:
    """consensus code paths that imported the leading-underscore name continue
    to work via the explicit alias in routing/llm_router.py."""
    from omicsclaw.routing.llm_router import _resolve_llm_config
    from omicsclaw.providers.runtime import resolve_chat_endpoint

    assert _resolve_llm_config is resolve_chat_endpoint
