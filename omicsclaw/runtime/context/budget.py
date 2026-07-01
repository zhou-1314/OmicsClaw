from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# §9.3 — input-context budget status (token-based; mirrors cellclaw's
# ContextBudgetEvaluator). Pure primitives here; wiring is a separate slice.
# --------------------------------------------------------------------------- #


class ContextBudgetStatus(str, Enum):
    """Input-context budget pressure (cellclaw ContextStatus parity)."""

    OK = "ok"
    WARNING = "warning"
    COMPRESS = "compress"
    CRITICAL = "critical"
    BLOCK = "block"


# Single source of truth for the char↔token bridge, used both by the char-budget
# derivation (engine.resolve_max_prompt_chars) and the budget-status token
# estimate — so the two never drift. A rough global proxy (see audit F1 for the
# CJK/content-type caveats).
CHARS_PER_TOKEN = 3.0


def effective_context_capacity(
    context_window: int,
    *,
    reserved_output: int = 4096,
    safety_margin: int = 2048,
) -> int:
    """Usable input-token budget = window − reserved output − safety margin."""
    return max(0, int(context_window) - int(reserved_output) - int(safety_margin))


_BUDGET_WARNING_PCT = 65
_BUDGET_COMPRESS_PCT = 80
_BUDGET_CRITICAL_PCT = 90
_BUDGET_BLOCK_PCT = 96


def classify_context_budget(
    used_tokens: int, effective_capacity: int
) -> ContextBudgetStatus:
    """Classify input-context pressure by used/effective percentage."""
    if effective_capacity <= 0:
        return ContextBudgetStatus.BLOCK
    pct = used_tokens / effective_capacity * 100
    if pct < _BUDGET_WARNING_PCT:
        return ContextBudgetStatus.OK
    if pct < _BUDGET_COMPRESS_PCT:
        return ContextBudgetStatus.WARNING
    if pct < _BUDGET_CRITICAL_PCT:
        return ContextBudgetStatus.COMPRESS
    if pct < _BUDGET_BLOCK_PCT:
        return ContextBudgetStatus.CRITICAL
    return ContextBudgetStatus.BLOCK


# F4 (ADR 0024 budget accuracy): inline image content blocks otherwise count
# only their ~9-char ``image_url`` type string, so a multimodal turn is nearly
# invisible to the char budget and can silently overflow the model window.
# Charge a fixed per-image budget instead — a rough proxy for typical vision
# token cost (~1300 tokens x ~3 chars/token). NOT the raw base64 length, which
# would over-count catastrophically (a 100 KB image ~= 133 KB of base64).
_IMAGE_BUDGET_CHARS = 4000

_IMAGE_BLOCK_TYPES = frozenset({"image_url", "image", "input_image"})


def _is_image_block(block: dict[str, Any]) -> bool:
    """True if a multimodal content block carries an image payload."""
    if str(block.get("type", "") or "").strip().lower() in _IMAGE_BLOCK_TYPES:
        return True
    return "image_url" in block


def estimate_message_size(message: dict[str, Any]) -> int:
    """Approximate one transcript message's budget footprint."""
    size = len(str(message.get("role", "") or ""))

    content = message.get("content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if _is_image_block(block):
                    size += _IMAGE_BUDGET_CHARS
                # Still count the block's type + any same-block text (mixed
                # image+text blocks, metadata): the image surcharge must not
                # short-circuit past real text.
                size += len(str(block.get("type", "") or ""))
                size += len(str(block.get("text", "") or ""))
            else:
                size += len(str(block))
    else:
        size += len(str(content or ""))

    for tool_call in message.get("tool_calls", []) or []:
        if not isinstance(tool_call, dict):
            size += len(str(tool_call))
            continue
        size += len(str(tool_call.get("id", "") or ""))
        size += len(str(tool_call.get("type", "") or ""))
        function_block = tool_call.get("function", {}) or {}
        if isinstance(function_block, dict):
            size += len(str(function_block.get("name", "") or ""))
            size += len(str(function_block.get("arguments", "") or ""))
        else:
            size += len(str(function_block))

    size += len(str(message.get("tool_call_id", "") or ""))
    size += len(str(message.get("reasoning_content", "") or ""))
    return size


def _group_history_blocks(history: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    blocks: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(history):
        message = history[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            block = [message]
            index += 1
            while index < len(history) and history[index].get("role") == "tool":
                block.append(history[index])
                index += 1
            blocks.append(block)
            continue

        blocks.append([message])
        index += 1
    return blocks


def trim_history_to_budget(
    history: list[dict[str, Any]],
    *,
    max_messages: int = 50,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Return the newest contiguous history suffix that fits the current budget.

    The budget is block-aware: assistant tool-call messages stay grouped with their
    following tool results so truncation does not split bundles in the middle.
    """
    if not history:
        return []
    if max_messages == 0:
        return []

    char_budget = max_chars if max_chars and max_chars > 0 else None
    blocks = _group_history_blocks(history)

    selected_reversed: list[list[dict[str, Any]]] = []
    selected_message_count = 0
    selected_chars = 0

    for block in reversed(blocks):
        block_message_count = len(block)
        block_chars = sum(estimate_message_size(message) for message in block)
        fits_messages = max_messages < 0 or (selected_message_count + block_message_count) <= max_messages
        fits_chars = char_budget is None or (selected_chars + block_chars) <= char_budget

        if selected_reversed:
            if not (fits_messages and fits_chars):
                break
        elif not (fits_messages and fits_chars):
            # Always preserve the newest block so the model still sees the
            # active turn, even when the configured budget is very small.
            selected_reversed.append(block)
            break

        if not selected_reversed or (fits_messages and fits_chars):
            selected_reversed.append(block)
            selected_message_count += block_message_count
            selected_chars += block_chars

    trimmed: list[dict[str, Any]] = []
    for block in reversed(selected_reversed):
        trimmed.extend(block)
    return trimmed


# --------------------------------------------------------------------------- #
# Token budget tracking (was token_budget.py — merged into this module so all
# context/window budgeting lives in one place).
# --------------------------------------------------------------------------- #


TOKEN_BUDGET_COMPLETION_THRESHOLD = 0.9
TOKEN_BUDGET_DIMINISHING_THRESHOLD = 500

_TOKEN_BUDGET_RE = re.compile(
    r"^\+?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kKmMbB]?)\s*$"
)


@dataclass(slots=True)
class TokenBudgetTracker:
    budget_tokens: int
    continuation_count: int = 0
    total_completion_tokens: int = 0
    last_delta_tokens: int = 0
    last_checked_tokens: int = 0
    started_at: float = 0.0


@dataclass(frozen=True, slots=True)
class TokenBudgetContinueDecision:
    action: str
    nudge_message: str
    continuation_count: int
    pct: int
    turn_tokens: int
    budget: int


@dataclass(frozen=True, slots=True)
class TokenBudgetStopDecision:
    action: str
    completion_event: dict[str, int | float | bool] | None


def normalize_token_budget(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None

    text = str(value).strip()
    if not text:
        return None

    match = _TOKEN_BUDGET_RE.match(text)
    if not match:
        return None

    number = float(match.group("value"))
    unit = match.group("unit").lower()
    multiplier = {
        "": 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
    }[unit]
    budget = int(number * multiplier)
    return budget if budget > 0 else None


def create_token_budget_tracker(budget_tokens: int | str | None) -> TokenBudgetTracker | None:
    normalized = normalize_token_budget(budget_tokens)
    if normalized is None:
        return None
    return TokenBudgetTracker(
        budget_tokens=normalized,
        started_at=time.time(),
    )


def record_completion_tokens(
    tracker: TokenBudgetTracker | None,
    completion_tokens: int,
) -> None:
    if tracker is None or completion_tokens <= 0:
        return
    tracker.total_completion_tokens += completion_tokens


def build_budget_continuation_message(
    *,
    pct: int,
    turn_tokens: int,
    budget: int,
) -> str:
    return (
        "Continue working on the same request. "
        f"The current turn has used about {turn_tokens}/{budget} output tokens ({pct}%). "
        "Do not restart from the top or restate completed work. "
        "Continue from the next unfinished step and stop only when the task is actually complete "
        "or progress has clearly plateaued."
    )


def check_token_budget(
    tracker: TokenBudgetTracker | None,
) -> TokenBudgetContinueDecision | TokenBudgetStopDecision:
    if tracker is None or tracker.budget_tokens <= 0:
        return TokenBudgetStopDecision(action="stop", completion_event=None)

    turn_tokens = tracker.total_completion_tokens
    pct = int(round((turn_tokens / tracker.budget_tokens) * 100))
    delta_since_last_check = turn_tokens - tracker.last_checked_tokens

    is_diminishing = (
        tracker.continuation_count >= 3
        and delta_since_last_check < TOKEN_BUDGET_DIMINISHING_THRESHOLD
        and tracker.last_delta_tokens < TOKEN_BUDGET_DIMINISHING_THRESHOLD
    )

    if not is_diminishing and turn_tokens < tracker.budget_tokens * TOKEN_BUDGET_COMPLETION_THRESHOLD:
        tracker.continuation_count += 1
        tracker.last_delta_tokens = delta_since_last_check
        tracker.last_checked_tokens = turn_tokens
        return TokenBudgetContinueDecision(
            action="continue",
            nudge_message=build_budget_continuation_message(
                pct=pct,
                turn_tokens=turn_tokens,
                budget=tracker.budget_tokens,
            ),
            continuation_count=tracker.continuation_count,
            pct=pct,
            turn_tokens=turn_tokens,
            budget=tracker.budget_tokens,
        )

    if is_diminishing or tracker.continuation_count > 0:
        return TokenBudgetStopDecision(
            action="stop",
            completion_event={
                "continuation_count": tracker.continuation_count,
                "pct": pct,
                "turn_tokens": turn_tokens,
                "budget": tracker.budget_tokens,
                "diminishing_returns": is_diminishing,
                "duration_ms": int((time.time() - tracker.started_at) * 1000),
            },
        )

    return TokenBudgetStopDecision(action="stop", completion_event=None)
