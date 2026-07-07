"""Anthropic prompt-cache breakpoint injection. OmicsClaw engineers a byte-stable
system+tools prefix but, unlike auto-caching providers (OpenAI/DeepSeek),
Anthropic/OpenRouter-Anthropic will not cache it without an explicit
``cache_control: ephemeral`` breakpoint — so a native Anthropic backend previously got
~0% caching. This transform adds those breakpoints, gated strictly to Anthropic-family
+ non-localhost + not-disabled."""
from __future__ import annotations

from omicsclaw.providers.models import apply_prompt_cache_breakpoints

_EPHEMERAL = {"type": "ephemeral"}


def _sys(text):
    return {"role": "system", "content": text}


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_anthropic_marks_system_and_last_tool_only(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_PROMPT_CACHE_BREAKPOINTS", raising=False)
    messages = [_sys("SYS"), {"role": "user", "content": "hi"}]
    tools = [_tool("a"), _tool("b")]

    out_msgs, out_tools = apply_prompt_cache_breakpoints(
        messages, tools,
        provider="anthropic", model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1/",
    )

    # system string content becomes a content-block list carrying cache_control.
    sys_content = out_msgs[0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[-1]["text"] == "SYS"
    assert sys_content[-1]["cache_control"] == _EPHEMERAL
    # only the LAST tool is marked (one breakpoint), earlier tools untouched.
    assert out_tools[-1]["cache_control"] == _EPHEMERAL
    assert "cache_control" not in out_tools[0]
    # inputs are NOT mutated (transform works on copies).
    assert messages[0]["content"] == "SYS"
    assert "cache_control" not in tools[-1]


def test_openrouter_anthropic_model_is_detected_by_family():
    # provider label 'openrouter' but the model is anthropic/* → still needs a breakpoint.
    out_msgs, out_tools = apply_prompt_cache_breakpoints(
        [_sys("SYS")], [_tool("a")],
        provider="openrouter", model="anthropic/claude-sonnet-4.6",
        base_url="https://openrouter.ai/api/v1",
    )
    assert isinstance(out_msgs[0]["content"], list)
    assert out_tools[-1]["cache_control"] == _EPHEMERAL


def test_non_anthropic_provider_is_identity():
    messages = [_sys("SYS"), {"role": "user", "content": "hi"}]
    tools = [_tool("a")]
    out_msgs, out_tools = apply_prompt_cache_breakpoints(
        messages, tools,
        provider="deepseek", model="deepseek-chat",
        base_url="https://api.deepseek.com",
    )
    assert out_msgs == messages
    assert out_tools == tools


def test_localhost_ccproxy_is_identity():
    # ccproxy/localhost proxies don't accept the same payload shape (mirrors
    # get_default_features' localhost skip).
    messages = [_sys("SYS")]
    tools = [_tool("a")]
    out_msgs, out_tools = apply_prompt_cache_breakpoints(
        messages, tools,
        provider="anthropic", model="claude-sonnet-4-6",
        base_url="http://127.0.0.1:8080/v1",
    )
    assert out_msgs == messages
    assert out_tools == tools


def test_disabled_flag_is_identity():
    # The kill-switch is threaded in as ``enabled=False`` (the env read lives in the
    # send layer; models.py stays pure/no-I/O).
    messages = [_sys("SYS")]
    tools = [_tool("a")]
    out_msgs, out_tools = apply_prompt_cache_breakpoints(
        messages, tools,
        provider="anthropic", model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1/",
        enabled=False,
    )
    assert out_msgs == messages
    assert out_tools == tools


def test_no_tools_and_no_system_message_is_safe():
    # No system message + no tools must not crash and must not fabricate blocks.
    messages = [{"role": "user", "content": "hi"}]
    out_msgs, out_tools = apply_prompt_cache_breakpoints(
        messages, None,
        provider="anthropic", model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1/",
    )
    assert out_tools is None
    assert out_msgs[0]["content"] == "hi"


def test_repeated_application_is_byte_stable(monkeypatch):
    # ADR 0024: applied deterministically every turn, the marked payload must be
    # identical across turns (so the breakpoint itself does not churn the prefix).
    monkeypatch.delenv("OMICSCLAW_PROMPT_CACHE_BREAKPOINTS", raising=False)
    messages = [_sys("SYS"), {"role": "user", "content": "hi"}]
    tools = [_tool("a"), _tool("b")]
    once = apply_prompt_cache_breakpoints(
        messages, tools, provider="anthropic", model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1/",
    )
    twice = apply_prompt_cache_breakpoints(
        messages, tools, provider="anthropic", model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1/",
    )
    assert once == twice


# --------------------------------------------------------------------------- #
# Integration: the query-engine send site applies the transform, gated on the
# provider it was configured with.
# --------------------------------------------------------------------------- #

import asyncio
from types import SimpleNamespace

from omicsclaw.runtime.agent.query_engine import (
    QueryEngineCallbacks,
    QueryEngineConfig,
    QueryEngineContext,
    run_query_engine,
)
from omicsclaw.runtime.tools.registry import ToolRegistry
from omicsclaw.runtime.storage.tool_result import ToolResultStore
from omicsclaw.runtime.storage.transcript import TranscriptStore, sanitize_tool_history


class _RecordingLLM:
    def __init__(self, base_url="https://api.anthropic.com/v1/"):
        self.chat = self
        self.completions = self
        self.base_url = base_url
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        )


def _run(llm, config, tmp_path):
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    transcript_store.append_user_message("chat-A", "hi")
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    runtime = ToolRegistry([]).build_runtime({})
    asyncio.run(
        run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id="chat-A", session_id=None,
                system_prompt="SYS", user_message_content="hi",
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            config=config,
            callbacks=QueryEngineCallbacks(),
        )
    )
    return llm.last_kwargs


def test_send_site_injects_for_anthropic_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("OMICSCLAW_PROMPT_CACHE_BREAKPOINTS", raising=False)
    sent = _run(
        _RecordingLLM(base_url="https://api.anthropic.com/v1/"),
        QueryEngineConfig(model="claude-sonnet-4-6", provider="anthropic"),
        tmp_path,
    )
    system_content = sent["messages"][0]["content"]
    assert isinstance(system_content, list)
    assert system_content[-1]["cache_control"] == _EPHEMERAL


def test_send_site_is_identity_for_deepseek_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("OMICSCLAW_PROMPT_CACHE_BREAKPOINTS", raising=False)
    sent = _run(
        _RecordingLLM(base_url="https://api.deepseek.com"),
        QueryEngineConfig(model="deepseek-chat", provider="deepseek"),
        tmp_path,
    )
    # Auto-caching provider: system content stays a plain string (no breakpoint).
    assert isinstance(sent["messages"][0]["content"], str)
