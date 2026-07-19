"""Smoke tests for ``omicsclaw.engine.run_engine_loop``.

Full end-to-end coverage lives in ``tests/bot/test_agent_loop.py``
(via the bot wirer landed in 6c). The tests here focus on the
contract the engine itself owns: the LLM-not-configured early
return, and the helper string functions used to compose the
system prompt.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from omicsclaw.engine import (
    EngineDependencies,
    LLM_NOT_CONFIGURED_MESSAGE,
    run_engine_loop,
)
from omicsclaw.engine.loop import (
    _maybe_append_caller_addition,
    _maybe_append_mode_hint,
    _maybe_append_stage_fragment,
    resolve_max_prompt_tokens,
)
import omicsclaw.engine.loop as _engine_loop


def test_resolve_max_prompt_tokens_scales_with_window(monkeypatch):
    """ADR 0039: token budget = min(TOKEN_CAP=85_000, floor((window-8192)*0.5)).
    Window-relative below the cap (small windows shrink; mid-size use their window),
    capped for large windows (latency backstop), default for unknown windows,
    honors OMICSCLAW_MAX_PROMPT_TOKENS, and converts the deprecated _CHARS var."""
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_TOKENS", raising=False)
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_CHARS", raising=False)

    windows = {
        "big": 1_000_000,
        "small": 16_000,
        "small_real": 131_072,  # smallest registered window
        "unknown": None,
    }
    monkeypatch.setattr(_engine_loop, "get_context_window", lambda m: windows.get(m))

    # Large window: capped at the token cap (bounded cold/re-warm latency).
    assert resolve_max_prompt_tokens("big") == 85_000
    # Small synthetic window: shrinks below the cap.
    assert resolve_max_prompt_tokens("small") == min(85_000, (16_000 - 8192) // 2)
    assert resolve_max_prompt_tokens("small") < 85_000
    # Real small-window model (131072 tok) below the cap → window-relative budget.
    assert resolve_max_prompt_tokens("small_real") == (131_072 - 8192) // 2
    assert resolve_max_prompt_tokens("small_real") == 61_440
    # Unknown window (Ollama → None): token-cap fallback.
    assert resolve_max_prompt_tokens("unknown") == 85_000

    # Explicit token override wins regardless of window.
    monkeypatch.setenv("OMICSCLAW_MAX_PROMPT_TOKENS", "12345")
    assert resolve_max_prompt_tokens("big") == 12345
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_TOKENS", raising=False)

    # Deprecated char override is honored for one release, converted chars→tokens (÷4).
    monkeypatch.setenv("OMICSCLAW_MAX_PROMPT_CHARS", "40000")
    assert resolve_max_prompt_tokens("big") == 10_000  # 40000 // 4
    # A tiny deprecated char value must never convert to a 0-token budget (clamp≥1).
    monkeypatch.setenv("OMICSCLAW_MAX_PROMPT_CHARS", "3")
    assert resolve_max_prompt_tokens("big") == 1
    # When both are set, the token env wins.
    monkeypatch.setenv("OMICSCLAW_MAX_PROMPT_TOKENS", "9000")
    assert resolve_max_prompt_tokens("big") == 9000


def test_resolve_max_prompt_tokens_small_window_never_zero(monkeypatch):
    """G / ADR 0039: a known window at/near reserved_output (8192) must NOT yield a 0
    budget. A 0 ``max_prompt_tokens`` silently disables proactive collapse — the sole
    overflow handler — because the collapse/auto thresholds become None. Fall back to a
    fraction of the window so proactive collapse still fires on tiny/edge windows."""
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_TOKENS", raising=False)
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_CHARS", raising=False)
    windows = {"w8192": 8192, "w8193": 8193, "w4096": 4096, "w16000": 16_000}
    monkeypatch.setattr(_engine_loop, "get_context_window", lambda m: windows.get(m))

    # Windows where floor((window - 8192) * 0.5) collapses to 0 must stay positive.
    assert resolve_max_prompt_tokens("w8192") > 0
    assert resolve_max_prompt_tokens("w8193") > 0
    assert resolve_max_prompt_tokens("w4096") > 0
    # A window comfortably above reserved_output keeps the standard window-relative
    # budget (no behavior change for normal windows).
    assert resolve_max_prompt_tokens("w16000") == (16_000 - 8192) // 2


def test_collapse_llm_summary_env_toggle(monkeypatch):
    # ADR 0039 D5: default-ON; OMICSCLAW_COLLAPSE_LLM_SUMMARY=0 disables it.
    from omicsclaw.engine.loop import _collapse_llm_summary_enabled

    monkeypatch.delenv("OMICSCLAW_COLLAPSE_LLM_SUMMARY", raising=False)
    assert _collapse_llm_summary_enabled() is True  # default-ON
    monkeypatch.setenv("OMICSCLAW_COLLAPSE_LLM_SUMMARY", "0")
    assert _collapse_llm_summary_enabled() is False
    monkeypatch.setenv("OMICSCLAW_COLLAPSE_LLM_SUMMARY", "1")
    assert _collapse_llm_summary_enabled() is True


def test_build_compaction_config_sets_budget_relative_targets(monkeypatch):
    # §9.3 slice 3 + ADR 0039: the engine wires budget-relative compress-to-target
    # ratios so the collapse/auto preserve budgets scale with the model's TOKEN
    # budget instead of fixed magic constants. The ratios must stay below their
    # triggers (byte-stability) and stack collapse > auto (auto is more aggressive).
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_TOKENS", raising=False)
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_CHARS", raising=False)
    monkeypatch.setattr(_engine_loop, "get_context_window", lambda m: 1_000_000)

    cfg = _engine_loop._build_compaction_config("big")

    # ADR 0039: large window → capped at the token cap (85_000).
    assert cfg.max_prompt_tokens == 85_000
    assert cfg.collapse_target_ratio is not None
    assert cfg.auto_compact_target_ratio is not None
    # Targets sit below their triggers so the re-warmed next turn cannot re-collapse.
    assert cfg.collapse_target_ratio < cfg.collapse_trigger_ratio
    assert cfg.auto_compact_target_ratio < cfg.auto_compact_trigger_ratio
    # Auto compaction preserves less than collapse (strictly more aggressive).
    assert cfg.auto_compact_target_ratio < cfg.collapse_target_ratio


def _make_deps(**overrides) -> EngineDependencies:
    """Build a full EngineDependencies with minimal sentinel values."""
    field_names = {f.name for f in dataclasses.fields(EngineDependencies)}
    defaults = {name: None for name in field_names}
    defaults["omicsclaw_model"] = "test-model"
    defaults["llm_provider_name"] = "test-provider"
    defaults["omicsclaw_dir"] = "/tmp/oc-test"
    defaults["max_history"] = 80
    defaults["max_history_chars"] = None
    defaults["max_conversations"] = 200
    defaults["skill_aliases"] = ()
    defaults["deep_learning_methods"] = frozenset()
    defaults.update(overrides)
    return EngineDependencies(**defaults)


def test_returns_setup_prompt_when_llm_is_none() -> None:
    """If omicsclaw.runtime.agent.state.llm is None at request time, the engine returns
    a setup-instructions message instead of raising. This is the
    contract the bot has relied on since core.py was carved up."""
    deps = _make_deps(llm=None)

    result = asyncio.run(
        run_engine_loop(
            deps=deps,
            chat_id="chat-1",
            user_content="hello",
        )
    )

    assert result == LLM_NOT_CONFIGURED_MESSAGE
    assert "LLM is not configured" in result
    assert "LLM_API_KEY" in result


def test_engine_assembles_stored_content_with_the_same_message_context(monkeypatch):
    rendered_content = [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,EPHEMERAL"},
        }
    ]
    durable_content = [
        {"type": "attachment_ref", "attachment_id": "attachment-1"}
    ]
    message_context = "MESSAGE CONTEXT"
    adapter = object()
    captured = {}

    async def fake_assemble_chat_context(**_kwargs):
        return SimpleNamespace(
            session_id="session-1",
            system_prompt="SYSTEM",
            user_message_content=[
                {"type": "text", "text": message_context},
                *rendered_content,
            ],
            prompt_context=SimpleNamespace(
                request=object(),
                message_context=message_context,
            ),
        )

    async def fake_run_query_engine(**kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(
        _engine_loop,
        "_assemble_chat_context",
        fake_assemble_chat_context,
    )
    monkeypatch.setattr(_engine_loop, "run_query_engine", fake_run_query_engine)
    monkeypatch.setattr(
        _engine_loop,
        "build_default_lifecycle_hook_runtime",
        lambda _root: None,
    )

    transcript_store = SimpleNamespace(
        max_history=0,
        max_history_chars=None,
        max_conversations=0,
        sanitizer=lambda messages, warn=True: messages,
        get_history=lambda _chat_id: [],
    )
    tool_registry = SimpleNamespace(
        to_openai_tools_for_request=lambda *_args, **_kwargs: []
    )
    deps = _make_deps(
        llm=object(),
        transcript_store=transcript_store,
        tool_result_store=object(),
        tool_runtime=object(),
        tool_registry=tool_registry,
        callbacks_builder=lambda **_kwargs: object(),
    )

    result = asyncio.run(
        run_engine_loop(
            deps=deps,
            chat_id="chat-1",
            user_content=rendered_content,
            stored_user_content=durable_content,
            content_adapter=adapter,
            user_turn_context="VOLATILE CONTEXT",
        )
    )

    assert result == "done"
    context = captured["context"]
    assert context.user_message_content == [
        {"type": "text", "text": "VOLATILE CONTEXT"},
        {"type": "text", "text": message_context},
        *rendered_content,
    ]
    assert context.stored_user_content == [
        {"type": "text", "text": "VOLATILE CONTEXT"},
        {"type": "text", "text": message_context},
        *durable_content,
    ]
    assert context.content_adapter is adapter


class TestMaybeAppendCallerAddition:
    def test_no_op_for_empty(self) -> None:
        assert _maybe_append_caller_addition("base", "") == "base"

    def test_strips_added_section(self) -> None:
        assert (
            _maybe_append_caller_addition("base", "  extra  ") == "base\n\nextra"
        )

    def test_strips_trailing_whitespace_on_base(self) -> None:
        assert (
            _maybe_append_caller_addition("base   \n\n", "extra")
            == "base\n\nextra"
        )


class TestMaybeAppendModeHint:
    def test_unknown_mode_is_no_op(self) -> None:
        assert _maybe_append_mode_hint("base", "wat") == "base"

    def test_ask_mode_is_no_op(self) -> None:
        # "ask" is the implicit default — emitting a mode hint for it
        # would just add noise to every system prompt.
        assert _maybe_append_mode_hint("base", "ask") == "base"

    def test_empty_mode_is_no_op(self) -> None:
        assert _maybe_append_mode_hint("base", "") == "base"

    def test_code_mode_appends_section(self) -> None:
        result = _maybe_append_mode_hint("base", "code")
        assert "## Mode" in result
        assert "code mode" in result
        assert result.startswith("base")

    def test_plan_mode_appends_section(self) -> None:
        result = _maybe_append_mode_hint("base", "plan")
        assert "## Mode" in result
        assert "plan mode" in result


class TestMaybeAppendStageFragment:
    # Bench (ADR 0020): stage stance fragment is additive; empty/unknown = no-op.
    def test_empty_stage_is_no_op(self) -> None:
        assert _maybe_append_stage_fragment("base", "") == "base"

    def test_unknown_stage_is_no_op(self) -> None:
        assert _maybe_append_stage_fragment("base", "bogus") == "base"

    def test_read_stage_appends_section(self) -> None:
        result = _maybe_append_stage_fragment("base", "read")
        assert "## Stage" in result
        assert "Read" in result
        assert result.startswith("base")

    def test_analyze_stage_appends_section(self) -> None:
        result = _maybe_append_stage_fragment("base", "analyze")
        assert "## Stage" in result
        assert "Analyze" in result


def test_max_tool_iterations_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The constant is read from env at import time. Reimporting
    the module after setting the env var should yield the new value
    — proves the engine isn't ignoring the user's override."""
    import importlib

    monkeypatch.setenv("OMICSCLAW_MAX_TOOL_ITERATIONS", "42")
    import omicsclaw.engine.loop as loop_module

    reloaded = importlib.reload(loop_module)
    assert reloaded.MAX_TOOL_ITERATIONS == 42

    # Restore the default so other tests in this session see the
    # original module-level value (importlib.reload mutates the
    # actual module object, so we reload again with the env unset).
    monkeypatch.delenv("OMICSCLAW_MAX_TOOL_ITERATIONS", raising=False)
    importlib.reload(loop_module)
