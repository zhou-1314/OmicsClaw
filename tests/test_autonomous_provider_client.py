"""Regression tests for ProviderChatClient.complete() (P0).

The mini-agent collapses a ``None`` from ``complete()`` into "LLM returned no
content" and retries until the budget trips. These tests pin the behaviours that
used to silently produce ``None``: reasoning-model output in ``reasoning_content``,
a reasoning model that rejects ``temperature`` with HTTP 400, and real HTTP errors.
"""

from __future__ import annotations

import types

import pytest
import requests

import omicsclaw.providers.runtime as provider_runtime
from omicsclaw.autonomous.code_loop import ProviderChatClient


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _content(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def fake_key(monkeypatch):
    """resolve_provider_runtime returns a runtime with a usable api_key."""
    rt = types.SimpleNamespace(api_key="sk-test", base_url="https://api.test/v1", model="m")
    monkeypatch.setattr(provider_runtime, "resolve_provider_runtime", lambda **k: rt)
    return rt


def test_reasoning_content_fallback(fake_key, monkeypatch):
    payload = {"choices": [{"message": {"content": "", "reasoning_content": "**Purpose**: ok"}}]}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, payload))
    assert ProviderChatClient().complete("hi") == "**Purpose**: ok"


def test_temperature_400_retries_without_temperature(fake_key, monkeypatch):
    seen = []

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        seen.append(body)
        if "temperature" in body:
            return _Resp(400, {"error": {"message": "x"}}, text="temperature must be 1 for this model")
        return _Resp(200, _content("**Purpose**: retried"))

    monkeypatch.setattr(requests, "post", fake_post)
    assert ProviderChatClient().complete("hi", temperature=0.0) == "**Purpose**: retried"
    assert len(seen) == 2
    assert "temperature" in seen[0] and "temperature" not in seen[1]


def test_non_temperature_400_does_not_retry(fake_key, monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(1)
        return _Resp(400, {"error": {"message": "bad request"}}, text="missing field messages")

    monkeypatch.setattr(requests, "post", fake_post)
    assert ProviderChatClient().complete("hi") is None
    assert len(calls) == 1  # no spurious retry for unrelated 400s


def test_http_401_returns_none(fake_key, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(401, text="invalid api key"))
    assert ProviderChatClient().complete("hi") is None


def test_network_exception_returns_none(fake_key, monkeypatch):
    def boom(*a, **k):
        raise requests.RequestException("read timeout")

    monkeypatch.setattr(requests, "post", boom)
    assert ProviderChatClient().complete("hi") is None


def test_no_api_key_returns_none(monkeypatch):
    rt = types.SimpleNamespace(api_key="", base_url="", model="m")
    monkeypatch.setattr(provider_runtime, "resolve_provider_runtime", lambda **k: rt)
    assert ProviderChatClient().complete("hi") is None


def test_plain_content_happy_path(fake_key, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, _content("  **Purpose**: ok  ")))
    assert ProviderChatClient().complete("hi") == "**Purpose**: ok"


def test_configurable_timeout_is_passed_through(fake_key, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Resp(200, _content("ok"))

    monkeypatch.setattr(requests, "post", fake_post)
    ProviderChatClient(timeout=180.0).complete("hi")
    assert captured["timeout"] == 180.0


def test_list_content_is_joined(fake_key, monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "**Purpose**: a"},
                        {"type": "text", "text": "**Code**: b"},
                    ]
                }
            }
        ]
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, payload))
    assert ProviderChatClient().complete("hi") == "**Purpose**: a\n**Code**: b"


def test_tool_calls_only_returns_none(fake_key, monkeypatch):
    payload = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x"}]}}]}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, payload))
    assert ProviderChatClient().complete("hi") is None
