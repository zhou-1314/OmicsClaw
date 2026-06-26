"""Regression tests for ProviderChatClient.complete() (P0).

The mini-agent collapses a ``None`` from ``complete()`` into "LLM returned no
content" and retries until the budget trips. These tests pin the behaviours that
used to silently produce ``None``: reasoning-model output in ``reasoning_content``,
a reasoning model that rejects ``temperature`` with HTTP 400, and real HTTP errors.

``complete()`` now drives the OpenAI SDK (the same client family as the main async
chat loop) instead of a hand-written ``requests.post``; the tests mock the SDK
seam (``chat.completions.with_raw_response.create``) and include one end-to-end
case through the real SDK + a mock transport so the raw-response contract is
guarded against SDK changes.
"""

from __future__ import annotations

import json
import types

import httpx
import openai
import pytest

import omicsclaw.providers.runtime as provider_runtime
from omicsclaw.autonomous.code_loop import ProviderChatClient


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeRaw:
    """Stands in for the SDK's raw-response wrapper (``LegacyAPIResponse``)."""

    def __init__(self, payload: dict):
        self.text = json.dumps(payload)


class _FakeCreate:
    def __init__(self, fn):
        self._fn = fn
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._fn(kwargs)


class _FakeClient:
    def __init__(self, fn):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(with_raw_response=_FakeCreate(fn))
        )


def _install(monkeypatch, fn) -> _FakeClient:
    """Make ``ProviderChatClient`` use a fake SDK client whose create() is ``fn``."""
    fake = _FakeClient(fn)
    monkeypatch.setattr(
        ProviderChatClient, "_client_for", lambda self, api_key, base_url: fake
    )
    return fake


def _status_error(cls, status: int, text: str):
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(status, text=text, request=req)
    return cls(message=text, response=resp, body=None)


def _content(text) -> dict:
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def fake_key(monkeypatch):
    """resolve_provider_runtime returns a runtime with a usable api_key."""
    rt = types.SimpleNamespace(api_key="sk-test", base_url="https://api.test/v1", model="m")
    monkeypatch.setattr(provider_runtime, "resolve_provider_runtime", lambda **k: rt)
    return rt


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_reasoning_content_fallback(fake_key, monkeypatch):
    payload = {"choices": [{"message": {"content": "", "reasoning_content": "**Purpose**: ok"}}]}
    _install(monkeypatch, lambda kw: _FakeRaw(payload))
    assert ProviderChatClient().complete("hi") == "**Purpose**: ok"


def test_plain_content_happy_path(fake_key, monkeypatch):
    _install(monkeypatch, lambda kw: _FakeRaw(_content("  **Purpose**: ok  ")))
    assert ProviderChatClient().complete("hi") == "**Purpose**: ok"


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
    _install(monkeypatch, lambda kw: _FakeRaw(payload))
    assert ProviderChatClient().complete("hi") == "**Purpose**: a\n**Code**: b"


def test_tool_calls_only_returns_none(fake_key, monkeypatch):
    payload = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x"}]}}]}
    _install(monkeypatch, lambda kw: _FakeRaw(payload))
    assert ProviderChatClient().complete("hi") is None


# --------------------------------------------------------------------------- #
# temperature retry + HTTP errors
# --------------------------------------------------------------------------- #


def test_temperature_400_retries_without_temperature(fake_key, monkeypatch):
    def fn(kw):
        if "temperature" in kw:
            raise _status_error(openai.BadRequestError, 400, "temperature must be 1 for this model")
        return _FakeRaw(_content("**Purpose**: retried"))

    fake = _install(monkeypatch, fn)
    assert ProviderChatClient().complete("hi", temperature=0.0) == "**Purpose**: retried"
    seen = fake.chat.completions.with_raw_response.calls
    assert len(seen) == 2
    assert "temperature" in seen[0] and "temperature" not in seen[1]


def test_non_temperature_400_does_not_retry(fake_key, monkeypatch):
    def fn(kw):
        raise _status_error(openai.BadRequestError, 400, "missing field messages")

    fake = _install(monkeypatch, fn)
    assert ProviderChatClient().complete("hi") is None
    assert len(fake.chat.completions.with_raw_response.calls) == 1  # no spurious retry


def test_http_401_returns_none(fake_key, monkeypatch):
    _install(monkeypatch, lambda kw: (_ for _ in ()).throw(
        _status_error(openai.AuthenticationError, 401, "invalid api key")
    ))
    assert ProviderChatClient().complete("hi") is None


def test_network_exception_returns_none(fake_key, monkeypatch):
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")

    def fn(kw):
        raise openai.APIConnectionError(message="read timeout", request=req)

    _install(monkeypatch, fn)
    assert ProviderChatClient().complete("hi") is None


def test_no_api_key_returns_none(monkeypatch):
    rt = types.SimpleNamespace(api_key="", base_url="", model="m")
    monkeypatch.setattr(provider_runtime, "resolve_provider_runtime", lambda **k: rt)
    assert ProviderChatClient().complete("hi") is None


def test_configurable_timeout_is_passed_through(fake_key, monkeypatch):
    captured = {}

    def fn(kw):
        captured["timeout"] = kw.get("timeout")
        return _FakeRaw(_content("ok"))

    _install(monkeypatch, fn)
    ProviderChatClient(timeout=180.0).complete("hi")
    assert captured["timeout"] == 180.0


# --------------------------------------------------------------------------- #
# SDK client construction (convergence with the main async client)
# --------------------------------------------------------------------------- #


def test_max_retries_plumbed_to_client():
    """SDK transport retries default OFF (bounded per-step wall clock; the loop
    owns retry/repair). An explicit override still reaches the SDK client."""
    default = ProviderChatClient()._client_for("sk-x", "https://api.test/v1")
    assert default.max_retries == 0
    override = ProviderChatClient(max_retries=2)._client_for("sk-x", "https://api.test/v1")
    assert override.max_retries == 2


def test_real_sdk_raw_response_end_to_end(fake_key, monkeypatch):
    """End-to-end through the real OpenAI SDK + a mock transport.

    Guards the contract that ``with_raw_response.create().text`` carries the
    unparsed body — including ``reasoning_content`` the SDK's typed model drops.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "", "reasoning_content": "**Purpose**: sdk"}}]},
        )

    real_openai_cls = openai.OpenAI

    def factory(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler))
        return real_openai_cls(**kwargs)

    monkeypatch.setattr(openai, "OpenAI", factory)
    assert ProviderChatClient().complete("hi") == "**Purpose**: sdk"
