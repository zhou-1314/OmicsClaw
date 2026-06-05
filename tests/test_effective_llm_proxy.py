"""Tests for session.effective_llm_proxy() — the proxy surfaced in the
LLM-init startup log so a missing proxy is diagnosable.

Regression context: a remote backend started without sourcing the user's shell
rc had no HTTPS_PROXY in its process env, so LLM calls went direct and failed
with "Connection error" despite a valid key. The startup log now reports the
effective proxy ("(none)" when unset) to make that obvious.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omicsclaw.runtime.agent.session import effective_llm_proxy  # noqa: E402

_PROXY_VARS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"
)


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_none_when_no_proxy_set():
    assert effective_llm_proxy() == "(none)"


def test_reports_plain_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    assert effective_llm_proxy() == "http://127.0.0.1:7890"


def test_masks_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@10.0.0.1:8080")
    out = effective_llm_proxy()
    assert "secret" not in out and "user" not in out
    assert out == "http://***@10.0.0.1:8080"


def test_falls_back_to_all_proxy_for_socks(monkeypatch):
    # SOCKS proxies are typically set via ALL_PROXY; the agent must surface it.
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    assert effective_llm_proxy() == "socks5://127.0.0.1:1080"


def test_https_proxy_takes_precedence(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://a:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://b:2")
    assert effective_llm_proxy() == "http://a:1"
