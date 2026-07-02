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
    resolve_max_prompt_chars,
)
import omicsclaw.engine.loop as _engine_loop


def test_resolve_max_prompt_chars_scales_with_window(monkeypatch):
    """ADR 0024 / cap-policy: collapse budget = min(deliberate DEFAULT_MAX_PROMPT_CHARS
    budget, window*1.5); it is window-relative below the cap (so small windows shrink
    AND mid-size windows use their window), capped for large windows, falls back to
    the default for unknown windows, and honors the OMICSCLAW_MAX_PROMPT_CHARS override."""
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_CHARS", raising=False)

    windows = {
        "big": 1_000_000,
        "small": 16_000,
        "small_real": 131_072,  # smallest registered window
        "unknown": None,
    }
    monkeypatch.setattr(_engine_loop, "get_context_window", lambda m: windows.get(m))

    # Large window: capped at the deliberate budget (bounded cost/latency).
    assert resolve_max_prompt_chars("big") == 256000
    # Small synthetic window: shrinks well below the cap for safety.
    assert resolve_max_prompt_chars("small") == min(256000, int(16_000 * 3.0 * 0.5))
    assert resolve_max_prompt_chars("small") < 256000
    # A REAL small-window model (131072 tok) is below the 256000 cap, so it now gets
    # the window-relative budget (the branch the old 96000 cap left dead), not the cap.
    assert resolve_max_prompt_chars("small_real") == min(256000, int(131_072 * 3.0 * 0.5))
    assert resolve_max_prompt_chars("small_real") == 196608
    # Unknown window (e.g. Ollama, returns None): conservative default.
    assert resolve_max_prompt_chars("unknown") == 256000

    # Explicit override wins regardless of window.
    monkeypatch.setenv("OMICSCLAW_MAX_PROMPT_CHARS", "12345")
    assert resolve_max_prompt_chars("big") == 12345
    assert resolve_max_prompt_chars("unknown") == 12345


def test_build_compaction_config_sets_budget_relative_targets(monkeypatch):
    # §9.3 slice 3: the engine wires budget-relative compress-to-target ratios so
    # the collapse/auto preserve budgets scale with the model's char budget
    # instead of the fixed magic constants. The ratios must stay below their
    # triggers (byte-stability) and stack collapse > auto (auto is more aggressive).
    monkeypatch.delenv("OMICSCLAW_MAX_PROMPT_CHARS", raising=False)
    monkeypatch.setattr(_engine_loop, "get_context_window", lambda m: 1_000_000)

    cfg = _engine_loop._build_compaction_config("big")

    assert cfg.max_prompt_chars == 256000
    assert cfg.context_window_tokens == 1_000_000
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
