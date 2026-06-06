"""Phase 3 (Task 3.1) RED tests for the new ``surface_voice_rules`` injector.

The injector replaces the per-surface voice subsections that used to live
inside SOUL.md (Bot Mode / CLI Mode). It emits a small block (≤600 chars)
that adapts the agent's tone to the active surface:

- bot         — no emoji (professional tone), markdown formatting allowed
- interactive — plain text only, UPPERCASE for emphasis
- pipeline    — same plain-text rule as interactive
"""

from __future__ import annotations

import pytest

from omicsclaw.runtime.context.assembler import assemble_prompt_context
from omicsclaw.runtime.context.layers import (
    DEFAULT_CONTEXT_LAYER_INJECTORS,
    ContextAssemblyRequest,
)


def _layer_for(surface: str) -> str:
    asm = assemble_prompt_context(request=ContextAssemblyRequest(surface=surface))
    for layer in asm.layers:
        if layer.name == "surface_voice_rules":
            return layer.content
    return ""


def test_surface_voice_rules_injector_is_registered() -> None:
    names = [inj.name for inj in DEFAULT_CONTEXT_LAYER_INJECTORS]
    assert "surface_voice_rules" in names


def test_surface_voice_rules_for_bot_forbids_emoji() -> None:
    text = _layer_for("bot")
    assert text, "bot voice rules layer empty"
    lower = text.lower()
    # The chat surface (desktop app) must read professionally — no emoji.
    assert "no emoji" in lower
    assert "emoji ok" not in lower, "bot must not be told emoji are OK"
    # Markdown formatting still allowed (desktop renders it).
    assert "markdown formatting allowed" in lower


def test_surface_voice_rules_for_interactive_forbids_emoji_and_markdown_bold() -> None:
    text = _layer_for("interactive")
    assert text, "interactive voice rules layer empty"
    lower = text.lower()
    assert "no emoji" in lower or "plain text" in lower
    # CLI mode prefers UPPERCASE over markdown bold.
    assert "uppercase" in lower or "**bold**" not in text


def test_surface_voice_rules_for_pipeline_uses_plain_text() -> None:
    text = _layer_for("pipeline")
    assert text, "pipeline voice rules layer empty"
    lower = text.lower()
    assert "no emoji" in lower or "plain text" in lower


def test_surface_voice_rules_size_under_600_chars_per_surface() -> None:
    for surface in ("bot", "interactive", "pipeline"):
        text = _layer_for(surface)
        assert 0 < len(text) <= 600, (
            f"surface_voice_rules for {surface}: {len(text)} chars (budget 600)"
        )


def test_surface_voice_rules_bot_forbids_emoji_but_keeps_markdown() -> None:
    """The chat ("bot") surface now forbids emoji like interactive/pipeline
    (desktop replies must read professionally), but unlike those plain-text
    surfaces it still permits markdown formatting. Pins this contract."""
    bot = _layer_for("bot")
    interactive = _layer_for("interactive")
    assert "no emoji" in bot.lower()
    assert "no emoji" in interactive.lower() or "plain text" in interactive.lower()
    # bot keeps markdown; the plain-text surfaces do not.
    assert "markdown formatting allowed" in bot.lower()
    assert "plain text" in interactive.lower()


def test_surface_voice_rules_bot_has_no_signoff_but_keeps_greeting() -> None:
    """The chat ("bot") surface must not instruct a per-message sign-off — a
    trailing ``— OmicsBot`` on every reply reads like a letter, and the desktop
    UI already labels the sender. The opening greeting is intentionally kept.
    Pins the fix so the sign-off can't creep back into the voice rule."""
    bot = _layer_for("bot")
    lower = bot.lower()
    assert "sign off" not in lower, "bot surface must not tell the model to sign off"
    assert "omicsbot" not in lower, "no per-message — OmicsBot signature"
    # The greeting stays (user kept it).
    assert "greet" in lower, "bot surface greeting should be retained"
