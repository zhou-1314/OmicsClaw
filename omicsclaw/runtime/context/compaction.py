from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from ..context.budget import (
    ContextBudgetStatus,
    estimate_message_size,
    estimate_message_tokens,
    estimate_text_tokens,
    local_budget_status,
    trim_history_to_budget,
)
from ..storage.tool_result import ToolResultRecord, ToolResultStore
from ..storage.transcript import (
    TranscriptReplaySummary,
    build_transcript_summary,
    sanitize_tool_history,
)

STAGE_SNIP_COMPACT = "snip_compact"
STAGE_MICRO_COMPACT = "micro_compact"
STAGE_CONTEXT_COLLAPSE = "context_collapse"
STAGE_AUTO_COMPACT = "auto_compact"
STAGE_REACTIVE_COMPACT = "reactive_compact"

# Marker that wraps the body of a manual /compact summary message stored in
# the transcript. The next /compact invocation uses this to skip
# re-summarising the previous summary (CodePilot bug #7 — boundary tracking).
COMPACTION_SUMMARY_OPEN = "<compaction-summary>"
COMPACTION_SUMMARY_CLOSE = "</compaction-summary>"


def wrap_compaction_summary(body: str) -> str:
    """Wrap a summary body so subsequent compactions can detect and skip it."""
    return f"{COMPACTION_SUMMARY_OPEN}\n{body.strip()}\n{COMPACTION_SUMMARY_CLOSE}"


def unwrap_compaction_summary(content: str) -> str:
    """Return the inner body of a wrapped summary, or pass through unchanged."""
    text = (content or "").strip()
    if not text.startswith(COMPACTION_SUMMARY_OPEN):
        return content
    inner = text[len(COMPACTION_SUMMARY_OPEN) :]
    end = inner.rfind(COMPACTION_SUMMARY_CLOSE)
    if end == -1:
        return content
    return inner[:end].strip()


def is_compaction_summary_message(message: dict[str, Any]) -> bool:
    """True if ``message`` is a manual-/compact summary header."""
    if not isinstance(message, dict):
        return False
    if message.get("role") != "system":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    return content.lstrip().startswith(COMPACTION_SUMMARY_OPEN)


# ADR 0039 — token-native context-collapse budget. A latency backstop (not a cost
# policy): prefix caching makes accumulated history cheap to bill but does not bound
# cold/re-warm/miss latency, so the working prompt is capped ~85k tokens below a
# large model's window. See engine.resolve_max_prompt_tokens.
DEFAULT_MAX_PROMPT_TOKENS = 85_000


@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:
    enabled: bool = True
    # ADR 0039 — the context-collapse budget is now in TOKENS (was max_prompt_chars).
    max_prompt_tokens: int | None = DEFAULT_MAX_PROMPT_TOKENS
    snip_message_chars: int = 2400  # snip truncates message *strings* — stays char-based
    protected_tail_messages: int = 4
    micro_keep_recent_tool_messages: int = 1
    collapse_trigger_ratio: float = 0.82
    auto_compact_trigger_ratio: float = 0.92
    collapse_preserve_messages: int = 16
    collapse_preserve_tokens: int | None = 3000
    auto_compact_preserve_messages: int = 8
    auto_compact_preserve_tokens: int | None = 1500
    reactive_preserve_messages: int = 6
    reactive_preserve_tokens: int | None = 1000
    # §9.3 slice 3 — compress-to-target. When set (not None), the collapse/auto
    # stage converges the TOTAL prompt (system incl. its summary + preserved tail)
    # to this fraction of max_prompt_tokens — the char budget drives (the small
    # message-count cap is lifted; the fixed preserve_tokens above applies only in
    # legacy no-target mode) — so the target tracks the model's real char budget
    # rather than a magic number. Keep each ratio safely below its trigger ratio so
    # the re-warmed next turn cannot re-collapse (F2 one-compaction = one-rewarm).
    # None on both -> current behavior (backward-compatible). reactive is left
    # aggressive on purpose (emergency path), so it has no target ratio.
    collapse_target_ratio: float | None = None
    auto_compact_target_ratio: float | None = None
    max_highlights_per_role: int = 3
    max_compacted_refs: int = 3
    max_plan_refs: int = 2
    max_advisory_refs: int = 3
    # F6 — LLM-condensed collapse summary (opt-in, ships dark). When enabled AND a
    # collapse/auto-compact stage fires (target-active branch only) over an omitted
    # set of at least ``llm_summary_min_omitted`` messages, the deterministic
    # template summary is upgraded to an LLM "episode" summary, validated (tier 3:
    # non-empty and NO LONGER than the template, so the byte-stable re-hoist stays
    # within the same budget the template already satisfied — F2 one-compaction =
    # one-rewarm), and falls back to the template on any timeout/error/validation
    # failure. Structurally unreachable from snip/micro (every-turn hot path),
    # reactive-413, and /compact (they never enter _collapse_with_target).
    # ADR 0039 D5: default-ON — the LLM collapse summary is the normal output when
    # an llm is threaded (safe: token cap keeps F2, and the B1 content-fidelity gate
    # preserves paths/errors). OMICSCLAW_COLLAPSE_LLM_SUMMARY=0 disables it.
    collapse_llm_summary_enabled: bool = True
    llm_summary_min_omitted: int = 8
    llm_summary_timeout_s: float = 20.0
    llm_summary_max_tokens: int = 1024
    # C2 — cap on the accumulated ``## Persisted Compacted Context`` summary. When
    # set, the oldest ``### <label>`` blocks are elided so the summary cannot grow
    # unboundedly across collapses and freeze a large share of the budget. None keeps
    # the legacy unbounded behavior (byte-identical); the engine sets it from a
    # fraction of ``max_prompt_tokens``.
    max_persisted_summary_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedModelMessages:
    system_prompt: str
    messages: list[dict[str, Any]]
    estimated_tokens: int
    applied_stages: tuple[str, ...] = ()
    persisted_summary: str = ""
    budget_status: "ContextBudgetStatus | None" = None
    local_budget_status: "ContextBudgetStatus | None" = None
    # D1(a): the actionable status computed from the PRE-compaction token estimate
    # (the pressure that triggered this turn's collapse/auto/reactive), so the surface
    # can report how full the context was — not the deflated post-compaction value the
    # collapse just drove down to ~target. None when nothing raised pressure this turn.
    pre_compaction_budget_status: "ContextBudgetStatus | None" = None


@dataclass(frozen=True, slots=True)
class _CollapseResult:
    messages: list[dict[str, Any]]
    summary: str
    omitted_count: int
    # F6 — the omitted (dropped-with-summary) messages, so a caller can feed them
    # to an LLM episode summarizer. Populated by _collapse_history; empty when
    # nothing was omitted. Never itself persisted or sent to the model.
    omitted_history: list[dict[str, Any]] = field(default_factory=list)


def estimate_prompt_chars(system_prompt: str, messages: list[dict[str, Any]]) -> int:
    return len(str(system_prompt or "")) + sum(
        estimate_message_size(message) for message in messages
    )


def estimate_prompt_tokens(
    system_prompt: str, messages: list[dict[str, Any]], *, model: str | None = None
) -> int:
    """Token analogue of :func:`estimate_prompt_chars` (ADR 0039)."""
    return estimate_text_tokens(system_prompt, model=model) + sum(
        estimate_message_tokens(message, model=model) for message in messages
    )


def _prompt_budget_status(
    estimated_tokens: int, config: "ContextCompactionConfig"
) -> "ContextBudgetStatus | None":
    """The single actionable budget status: token usage vs ``max_prompt_tokens``.

    ADR 0039 / S3: replaces the retired window-relative status (which was
    ~always OK on large-window models). ``None`` when no budget is configured.
    """
    return local_budget_status(estimated_tokens, config.max_prompt_tokens)


def _flatten_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _truncate_text(text: str, *, max_chars: int, label: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    omitted = len(text) - max_chars
    marker = f"\n[{label}: omitted {omitted} chars]"
    if len(marker) >= max_chars:
        return marker[-max_chars:]

    available = max_chars - len(marker)
    if available <= 24:
        return text[:available] + marker

    separator = "\n...\n"
    if available <= len(separator) + 16:
        return text[:available].rstrip() + marker

    body_budget = available - len(separator)
    head_budget = max(8, int(body_budget * 0.7))
    tail_budget = max(0, body_budget - head_budget)
    head = text[:head_budget].rstrip()
    tail = text[-tail_budget:].lstrip() if tail_budget else ""
    compacted = head
    if tail:
        compacted = f"{head}{separator}{tail}"
    return f"{compacted}{marker}"


def _append_system_summary(system_prompt: str, heading: str, summary: str) -> str:
    if not summary.strip():
        return system_prompt
    return (f"{system_prompt.rstrip()}\n\n{heading}\n\n{summary.strip()}").strip()


# C2: the persisted collapse summary accumulates one ``### <label>`` block per
# collapse and carries ``previous_summary`` forward verbatim, so over a long
# multi-collapse session it grows ~linearly and eventually freezes a large share of
# the token budget (roughly ``collapse_target_ratio`` of it). ``max_tokens`` bounds
# it by keeping the NEWEST blocks that fit and eliding the oldest — a lossy but
# deterministic and idempotent trim (byte-stable re-hoist = one-collapse=one-rewarm).
_SUMMARY_ELISION_MARKER = (
    "[Older compacted context was condensed to keep the prompt within budget.]"
)
_SUMMARY_BLOCK_SEP = "\n\n---\n\n"


def _split_summary_blocks(summary: str) -> list[str]:
    return [block.strip() for block in summary.split(_SUMMARY_BLOCK_SEP) if block.strip()]


def _bound_summary_blocks(blocks: list[str], max_tokens: int) -> list[str]:
    """Keep the newest blocks that fit ``max_tokens`` (always keep the newest one),
    drop the oldest, and prepend an elision marker whenever anything is/was trimmed.

    Idempotent: a stale marker is stripped and re-derived, and the marker persists
    once present, so re-bounding an already-bounded list returns the same list —
    the byte-stable re-hoist the one-collapse=one-rewarm invariant depends on.
    """
    real_blocks = [block for block in blocks if block != _SUMMARY_ELISION_MARKER]
    had_marker = len(real_blocks) != len(blocks)

    kept_reversed: list[str] = []
    total = 0
    for block in reversed(real_blocks):
        tokens = estimate_text_tokens(block)
        if kept_reversed and total + tokens > max_tokens:
            break
        kept_reversed.append(block)
        total += tokens
    kept = list(reversed(kept_reversed))

    if had_marker or len(kept) < len(real_blocks):
        kept = [_SUMMARY_ELISION_MARKER, *kept]
    return kept


def _combine_persisted_summaries(
    previous_summary: str,
    sections: list[tuple[str, str]],
    *,
    max_tokens: int | None = None,
) -> str:
    parts: list[str] = []
    if previous_summary.strip():
        parts.append(previous_summary.strip())
    for heading, summary in sections:
        if summary.strip():
            parts.append(f"### {heading}\n\n{summary.strip()}")
    if max_tokens is not None and max_tokens > 0:
        blocks: list[str] = []
        for part in parts:
            blocks.extend(_split_summary_blocks(part))
        parts = _bound_summary_blocks(blocks, max_tokens)
    return _SUMMARY_BLOCK_SEP.join(parts).strip()


def _threshold_tokens(max_prompt_tokens: int | None, ratio: float) -> int | None:
    if max_prompt_tokens is None or max_prompt_tokens <= 0:
        return None
    bounded_ratio = min(1.0, max(0.0, ratio))
    return max(1, int(max_prompt_tokens * bounded_ratio))


def _preserve_token_target(
    preserve_tokens: int | None,
    target_ratio: float | None,
    max_prompt_tokens: int | None,
    system_tokens: int,
) -> int | None:
    """§9.3 slice 3: preserved-tail TOKEN budget for a compaction stage (ADR 0039).

    Legacy (no target ratio / no token budget): the fixed ``preserve_tokens``.

    Compress-to-target: the budget that makes the resulting TOTAL prompt
    (system + tail) converge to ``target_ratio * max_prompt_tokens``, by subtracting
    the ``system_tokens`` overhead — so the target is a true *total*-prompt target,
    invariant to system-prompt size, and a large budget keeps proportionally more
    recent context than the fixed preserve constants. The fixed ``preserve_tokens``
    is *not* a floor here: under a target the target drives purely, so a large system
    (or, via the two-pass caller, a large summary) cannot push the re-warmed next
    turn back over the collapse trigger (robust F2). Floored at 1 — never None/0 —
    because the message-count cap is lifted under a target, so a None token budget
    would keep the whole history and overflow.
    """
    if target_ratio is None or max_prompt_tokens is None or max_prompt_tokens <= 0:
        return preserve_tokens
    total_target = int(max_prompt_tokens * min(1.0, max(0.0, target_ratio)))
    return max(1, total_target - max(0, int(system_tokens)))


def _preserve_message_target(
    preserve_messages: int, target_ratio: float | None
) -> int:
    """§9.3 slice 3: message-count cap for a compaction stage.

    When compress-to-target is active the char budget (:func:`_preserve_token_target`)
    is the sole driver, so the fixed message-count cap is lifted (``-1`` =
    unbounded) and the tail fills up to the char target — otherwise the small
    default cap (16/8) would bind first and keep only a sliver of a large budget.
    Without a target ratio the configured message cap applies (backward-compatible).
    """
    return -1 if target_ratio is not None else preserve_messages


_EPISODE_SUMMARY_SYSTEM_PROMPT = (
    "You are compacting an ongoing AI agent conversation to keep it within its "
    "context budget. Summarize the omitted transcript below into a concise, "
    "faithful episode summary. Preserve: the user's goals and constraints, key "
    "decisions and their rationale, tool results and any file/artifact paths, and "
    "durable workspace state. Do NOT invent facts and do NOT add pleasantries. "
    "CRITICAL: never reproduce tool-call syntax, function-call JSON, or any text "
    "that imitates invoking a tool — refer to past tool use only as plain past-tense "
    "narration (e.g. \"read config.py\", not a tool call), so the summary cannot "
    "induce the model to mimic tool calls on later turns. Write dense prose or "
    "bullet points; be brief."
)


def _render_omitted_for_summary(omitted_history: list[dict[str, Any]]) -> str:
    """Render the omitted messages as plain text for the LLM summarizer (F6).

    Feeds the RAW omitted content (not previews) so the model can condense with
    full fidelity; the model's job is the compression.
    """
    parts: list[str] = []
    for message in omitted_history:
        role = str(message.get("role", "") or "")
        content = _flatten_message_content(message.get("content", ""))
        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls:
            names = []
            for tool_call in tool_calls or []:
                if not isinstance(tool_call, dict):
                    continue
                function_block = tool_call.get("function")
                if isinstance(function_block, dict) and function_block.get("name"):
                    names.append(str(function_block.get("name")))
            if names:
                marker = f"[called tools: {', '.join(names)}]"
                content = f"{content}\n{marker}".strip() if content else marker
        if content:
            parts.append(f"{role.upper()}: {content}")
    return "\n\n".join(parts)


def _should_refine_episode(
    llm: Any,
    config: "ContextCompactionConfig",
    omitted_history: list[dict[str, Any]],
) -> bool:
    """F6 tier-1 gate: only refine with an LLM when opted in and the omitted set is
    large enough to be worth a round-trip. (Reached only on the target-active
    collapse/auto path, so /compact + reactive are excluded structurally.)"""
    return (
        llm is not None
        and config.collapse_llm_summary_enabled
        and len(omitted_history) >= max(1, config.llm_summary_min_omitted)
    )


_TOOL_CALL_SIGNATURE = re.compile(
    r'"(?:tool_calls|tool_call_id|function|arguments)"\s*:'
    r"|<\s*(?:tool_call|tool_use|function_calls|invoke)\b",
    re.IGNORECASE,
)


def _looks_like_tool_invocation(text: str) -> bool:
    """True if the summary imitates a tool/function call (F6 anti-mimicry).

    The deterministic path renders prior tool calls as inert XML metadata; a free
    LLM summary must not reintroduce function-call JSON or tool-call tags into the
    persisted context, which would prompt the model to mimic tool calls as plain
    text on later turns (see _message_preview). A whole-body JSON object/array is
    never a legitimate prose episode summary, and the structural markers below are
    strong tool-call signals unlikely in faithful narration. Rejected → template.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return True
        except Exception:
            pass
    return bool(_TOOL_CALL_SIGNATURE.search(stripped))


def _extract_summary_text(response: Any) -> str:
    """Pull the assistant content from a non-streaming chat completion, tolerating
    both SDK objects (``.choices[0].message.content``) and plain dicts."""
    try:
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if not choices:
            return ""
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None and isinstance(first, dict):
            message = first.get("message")
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return str(content or "")
    except Exception:
        return ""


# B1 (ADR 0039 D5) content-fidelity gate: tokens the LLM summary must not drop if
# they were present in the DETERMINISTIC TEMPLATE for the same episode (ADR 0039
# pins the required set to the template, not the raw omitted content). Required
# tokens are file paths and error/pending markers. Match ABSOLUTE POSIX paths,
# RELATIVE multi-segment paths with a file extension, and Windows drive paths — a
# ``full_result_path`` value can be any of these; matching only absolute paths
# would silently drop a relative/Windows blob ref. The internal field NAME
# ``full_result_path`` is intentionally NOT a required marker: the template renders
# the path VALUE (``-> `/path```), and a faithful prose summary echoes the value,
# not the structural field label.
#
# Over-match guards (the matcher must not force a summary to reproduce non-paths,
# which would revive the inert-summary failure mode):
#   - Windows branch anchors on a WORD BOUNDARY before a single drive letter
#     (``\bC:``), so a URL scheme ``https://`` is not read as a drive path
#     ``s:/...`` (there is no boundary before the ``s`` in ``https``, and real
#     URI schemes are >=2 chars). It accepts both ``\`` and ``/`` separators so a
#     forward-slash Windows path (``C:/a/b.txt``) is still required.
#   - The POSIX branches carry a negative lookbehind ``(?<![:/\w.])`` so a path
#     inside a URL (``…//host/p``) is not matched.
#   - The relative branch requires an ALPHABETIC-initial file extension, so numeric
#     ratios / dates (``3/4.5``, ``2024/01/15``) are not treated as paths.
_FIDELITY_PATH_RE = re.compile(
    r"\b[A-Za-z]:[\\/][\w.\-\\/]+"                      # Windows drive path (C:\a\b.txt or C:/a/b.txt)
    r"|(?<![:/\w.])(?:/[\w.\-]+){2,}"                   # absolute POSIX path (>=2 segments)
    r"|(?<![:/\w.])(?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z]\w*" # relative path w/ alpha-initial extension
)
_FIDELITY_MARKER_RE = re.compile(
    r"\b(?:Error|Exception|Traceback|Failed|TODO|FIXME|pending)\b"
)


def _fidelity_tokens(text: str) -> set[str]:
    """Extract the fidelity-required tokens from ``text``: normalized file-path
    tokens (trailing sentence punctuation stripped) plus error/pending markers."""
    tokens = {path.rstrip(".,;:)\"'") for path in _FIDELITY_PATH_RE.findall(text)}
    tokens.update(_FIDELITY_MARKER_RE.findall(text))
    return tokens


def _drops_required_tokens(rendered_omitted: str, summary: str) -> bool:
    """True if ``summary`` drops a fidelity-required token (file path, error, or
    pending-work marker) present in ``rendered_omitted``.

    Compares EXTRACTED tokens, not raw substrings, so ``/a/b.bak`` does not satisfy
    required ``/a/b`` and trailing sentence punctuation does not cause false rejects.
    No-retry gate: the caller falls back to the deterministic template.
    """
    required = _fidelity_tokens(rendered_omitted)
    if not required:
        return False
    present = _fidelity_tokens(summary)
    return any(token not in present for token in required)


async def _summarize_episode_llm(
    *,
    llm: Any,
    llm_model: str | None,
    omitted_history: list[dict[str, Any]],
    template_fallback: str,
    config: "ContextCompactionConfig",
    max_summary_chars: int,
) -> str:
    """F6 tier-2/3: one bounded LLM call to condense ``omitted_history`` into an
    episode summary, else the deterministic ``template_fallback``.

    All-or-nothing: any timeout, client error, empty output, or a summary longer
    than ``max_summary_chars`` (the template's length — the F2 length cap) returns
    the template unchanged. Never raises except on cancellation.
    """
    if max_summary_chars <= 0:
        return template_fallback
    try:
        rendered = _render_omitted_for_summary(omitted_history)
        if not rendered.strip():
            return template_fallback
        response = await asyncio.wait_for(
            llm.chat.completions.create(
                model=llm_model,
                max_tokens=config.llm_summary_max_tokens,
                messages=[
                    {"role": "system", "content": _EPISODE_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": rendered},
                ],
                stream=False,
            ),
            timeout=max(0.1, config.llm_summary_timeout_s),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger(__name__).warning(
            "F6 LLM episode summary failed; falling back to the deterministic "
            "template summary.",
            exc_info=True,
        )
        return template_fallback

    summary = _extract_summary_text(response).strip()
    # Tier-3 validation. The length cap (<= the template) is load-bearing for F2:
    # a summary no larger than the template keeps the re-hoisted system prompt no
    # larger than the deterministic path already produced, so the next turn cannot
    # newly cross the collapse trigger (one compaction = one re-warm). ADR 0039: the
    # trigger is now TOKEN-based, so the TOKEN count is the PRIMARY cap — a summary
    # can be fewer code points yet tokenize LARGER than the template and re-cross the
    # token trigger, breaking F2. Also bound code points AND UTF-8 bytes (defense in
    # depth: a denser-encoded summary must not inflate the real provider payload and
    # risk a 413 → deterministic reactive compact → a second re-warm).
    if not summary:
        return template_fallback
    if estimate_text_tokens(summary) > estimate_text_tokens(template_fallback):
        return template_fallback
    if len(summary) > max_summary_chars:
        return template_fallback
    if len(summary.encode("utf-8")) > len(template_fallback.encode("utf-8")):
        return template_fallback
    if _looks_like_tool_invocation(summary):
        return template_fallback
    # B1 content-fidelity gate (no retry): if the summary drops a required file
    # path / error marker present in the DETERMINISTIC TEMPLATE for this episode,
    # use the template instead. Keying off ``template_fallback`` (not ``rendered``,
    # the raw omitted content) is what ADR 0039 D5/B1 pins: the guarantee is
    # "no worse than the template", so a faithful summary that preserves every
    # path the template kept is accepted rather than paid-for-then-discarded.
    if _drops_required_tokens(template_fallback, summary):
        return template_fallback
    return summary


async def _collapse_with_target(
    messages: list[dict[str, Any]],
    *,
    preserve_messages: int,
    preserve_tokens: int | None,
    target_ratio: float | None,
    max_prompt_tokens: int | None,
    section_label: str,
    render_system: Any,
    metadata: dict[str, Any] | None,
    workspace: str | None,
    config: "ContextCompactionConfig",
    llm: Any = None,
    llm_model: str | None = None,
) -> "_CollapseResult":
    """§9.3 slice 3: collapse converging the FINAL total to the target.

    ``render_system(extra)`` returns the system prompt given extra pending
    ``(label, summary)`` sections, so the tail budget can be sized against the
    real rendered system. Legacy (``target_ratio`` is None): a single pass with the
    fixed preserve budget.

    Target-active (three steps, all guarding F2 one-compaction = one-rewarm — the
    naive single pass leaves ``target + summary`` and re-collapses next turn):
      1. size the tail against the pre-summary system;
      2. re-collapse against the ACTUAL pass-1 summary so the extra-omitted
         messages are themselves summarized (fidelity);
      3. a final hard-trim against the ACTUAL pass-2 summary — which can still be
         larger than pass-1's (more verbatim, length-unbounded tool-result paths
         surface from the larger omitted set) — bringing the final total within
         target so the re-warmed next turn stays under the trigger. The trim drops
         at most a few oldest preserved messages not captured in the summary, and
         only when the summary actually grew (so the summary's "N older messages"
         count can then understate by that few — a benign, deterministic hint).

    The trim cannot reduce *irreducible* content: if the rendered system alone
    exceeds target, or the newest preserved block alone exceeds the remaining
    budget (``trim_history_to_budget`` always keeps the newest block), the total
    can still exceed target. That is an inherent limit, unchanged from legacy and
    backstopped by reactive compaction on a real context-length error.
    """
    if target_ratio is None:
        return _collapse_history(
            messages,
            preserve_messages=preserve_messages,
            preserve_tokens=preserve_tokens,
            metadata=metadata,
            workspace=workspace,
            config=config,
        )

    def _collapse(pending: tuple[tuple[str, str], ...]) -> "_CollapseResult":
        budget = _preserve_token_target(
            preserve_tokens, target_ratio, max_prompt_tokens,
            system_tokens=estimate_text_tokens(render_system(pending)),
        )
        return _collapse_history(
            messages,
            preserve_messages=preserve_messages,
            preserve_tokens=budget,
            metadata=metadata,
            workspace=workspace,
            config=config,
        )

    result = _collapse(())
    if result.omitted_count == 0:
        return result
    result = _collapse(((section_label, result.summary),))
    if result.omitted_count == 0:
        return result

    final_budget = _preserve_token_target(
        preserve_tokens, target_ratio, max_prompt_tokens,
        system_tokens=estimate_text_tokens(render_system(((section_label, result.summary),))),
    )
    trimmed = trim_history_to_budget(
        result.messages,
        max_messages=preserve_messages,
        max_chars=final_budget,
        size_fn=estimate_message_tokens,
    )
    dropped = len(result.messages) - len(trimmed)
    if dropped > 0:
        # trim keeps the newest contiguous suffix, so the dropped messages are the
        # oldest `dropped` of result.messages. Fold them into omitted_history so the
        # F6 LLM episode summary sees the FULL omitted set — otherwise omitted_count
        # would claim they were compacted while neither the template nor the LLM
        # ever saw them (a fidelity gap for a "faithful summary of omitted history").
        result = _CollapseResult(
            messages=trimmed,
            summary=result.summary,
            omitted_count=result.omitted_count + dropped,
            omitted_history=[*result.omitted_history, *result.messages[:dropped]],
        )

    # F6: optionally upgrade the deterministic template summary to an LLM episode
    # summary, capped at the template's length. Because the chosen summary is never
    # longer than the template, the re-hoisted next-turn system prompt is no larger
    # than the deterministic path already produced (which the byte-stable tests
    # pin under the trigger) — so F2 (one compaction = one re-warm) holds without
    # re-sizing the preserved tail. Falls back to the template on any failure.
    if _should_refine_episode(llm, config, result.omitted_history):
        chosen = await _summarize_episode_llm(
            llm=llm,
            llm_model=llm_model,
            omitted_history=result.omitted_history,
            template_fallback=result.summary,
            config=config,
            max_summary_chars=len(result.summary),
        )
        if chosen != result.summary:
            result = replace(result, summary=chosen)
    return result


def _apply_snip_compaction(
    messages: list[dict[str, Any]],
    *,
    config: ContextCompactionConfig,
) -> tuple[list[dict[str, Any]], bool]:
    if not messages:
        return messages, False

    changed = False
    protected_from = max(0, len(messages) - max(0, config.protected_tail_messages))
    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index >= protected_from:
            compacted.append(copy.deepcopy(message))
            continue

        updated = copy.deepcopy(message)
        content = updated.get("content")
        if isinstance(content, str) and len(content) > config.snip_message_chars > 0:
            updated["content"] = _truncate_text(
                content,
                max_chars=config.snip_message_chars,
                label="snip compacted older message",
            )
            changed = True

        # F11: snip does NOT rewrite tool_call function.arguments. Truncating a
        # JSON args string yields invalid args that are re-sent to the model every
        # turn (and, when a collapse stage co-fires, persisted via replace_history)
        # — an in-place corruption of append-only history. Oversized tool calls are
        # folded (dropped-with-summary) by the collapse stage, the correct place;
        # tool RESULT content is still compressed by snip (above) and micro.
        compacted.append(updated)

    return compacted, changed


def _build_micro_tool_reference(record: ToolResultRecord) -> str:
    return (
        "[tool result micro-compacted]\n"
        f"tool: {record.tool_name}\n"
        f"bytes: {record.output_bytes}\n"
        f"policy: {record.result_policy}\n"
        f"full_result_path: {record.storage_path}\n"
        "note: reload the referenced file if earlier tool details are needed."
    )


def _apply_micro_compaction(
    messages: list[dict[str, Any]],
    *,
    chat_id: int | str,
    tool_result_store: ToolResultStore,
    config: ContextCompactionConfig,
) -> tuple[list[dict[str, Any]], bool]:
    records_by_call_id = {
        record.tool_call_id: record
        for record in tool_result_store.get_records(chat_id)
        if record.storage_path
    }
    if not records_by_call_id:
        return [copy.deepcopy(message) for message in messages], False

    tool_indexes = [
        index for index, message in enumerate(messages) if message.get("role") == "tool"
    ]
    keep_recent = max(0, config.micro_keep_recent_tool_messages)
    protected_indexes = set(tool_indexes[-keep_recent:]) if keep_recent else set()

    changed = False
    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        updated = copy.deepcopy(message)
        if index not in protected_indexes and updated.get("role") == "tool":
            record = records_by_call_id.get(str(updated.get("tool_call_id", "") or ""))
            if record is not None:
                compact_content = _build_micro_tool_reference(record)
                current_content = str(updated.get("content", "") or "")
                if (
                    len(compact_content) < len(current_content)
                    or "[tool result compacted]" in current_content
                    or "[snip compacted older message" in current_content
                ):
                    updated["content"] = compact_content
                    changed = True
        compacted.append(updated)

    return compacted, changed


def _message_preview(message: dict[str, Any], *, max_chars: int = 180) -> str:
    role = str(message.get("role", "") or "")
    if role == "assistant" and message.get("tool_calls"):
        tool_names = []
        for tool_call in message.get("tool_calls", []) or []:
            if not isinstance(tool_call, dict):
                continue
            function_block = tool_call.get("function")
            if isinstance(function_block, dict) and function_block.get("name"):
                tool_names.append(str(function_block.get("name")))
        if tool_names:
            # XML self-closing tag — Claude/DeepSeek treat structural tags
            # as metadata and don't reproduce them as text. Bare prose form
            # ("Read, Edit") triggers few-shot mimicry: the model writes
            # plain-text tool descriptions on the next turn instead of
            # invoking real tool_calls.
            joined_names = (
                _truncate_text(
                    ",".join(tool_names),
                    max_chars=max(16, max_chars - 32),
                    label="tool-call summary",
                )
                .replace("\n", " ")
                .strip()
            )
            return f'<prior-tool-calls names="{joined_names}"/>'

    content = _flatten_message_content(message.get("content", ""))
    if not content:
        return ""
    preview = " ".join(content.split())
    return (
        _truncate_text(
            preview,
            max_chars=max_chars,
            label="collapsed summary",
        )
        .replace("\n", " ")
        .strip()
    )


def _collect_role_highlights(
    messages: list[dict[str, Any]],
    *,
    role: str,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []

    seen: set[str] = set()
    highlights: list[str] = []
    for message in reversed(messages):
        if message.get("role") != role:
            continue
        preview = _message_preview(message)
        if not preview or preview in seen:
            continue
        highlights.append(preview)
        seen.add(preview)
        if len(highlights) >= limit:
            break
    highlights.reverse()
    return highlights


def _build_collapse_summary(
    omitted_history: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None,
    workspace: str | None,
    config: ContextCompactionConfig,
) -> str:
    lines = [
        (
            f"- {len(omitted_history)} earlier message(s) were compacted to keep the "
            "active prompt within budget."
        ),
        "- Preserve the prior user intent, tool references, and durable workspace state when continuing.",
    ]

    user_highlights = _collect_role_highlights(
        omitted_history,
        role="user",
        limit=config.max_highlights_per_role,
    )
    if user_highlights:
        lines.extend(("", "### Omitted User Goals"))
        lines.extend(f"- {item}" for item in user_highlights)

    assistant_highlights = _collect_role_highlights(
        omitted_history,
        role="assistant",
        limit=config.max_highlights_per_role,
    )
    if assistant_highlights:
        lines.extend(("", "### Omitted Assistant State"))
        lines.extend(f"- {item}" for item in assistant_highlights)

    structured_summary = build_transcript_summary(
        omitted_history,
        metadata=metadata,
        workspace=workspace,
    )
    replay_block = TranscriptReplaySummary(
        omitted_message_count=len(omitted_history),
        compacted_tool_results=structured_summary.compacted_tool_results[
            : config.max_compacted_refs
        ],
        plan_references=structured_summary.plan_references[: config.max_plan_refs],
        advisory_events=structured_summary.advisory_events[: config.max_advisory_refs],
    ).to_prompt_block()
    if replay_block:
        lines.extend(("", replay_block))

    return "\n".join(lines).strip()


def compact_history(
    messages: list[dict[str, Any]],
    *,
    preserve_messages: int,
    preserve_tokens: int | None,
    config: ContextCompactionConfig,
    metadata: dict[str, Any] | None = None,
    workspace: str | None = None,
) -> "_CollapseResult":
    """Public entry point for on-demand transcript compaction.

    Returns a result with:
      * ``messages`` — the trimmed tail (preserved messages, sanitized).
      * ``summary`` — a deterministic, template-built summary of what was
        omitted (empty when nothing was compacted).
      * ``omitted_count`` — number of messages dropped from the head.

    No LLM call. Intended for slash-command surfaces (e.g. ``/compact``).
    """
    return _collapse_history(
        messages,
        preserve_messages=preserve_messages,
        preserve_tokens=preserve_tokens,
        metadata=metadata,
        workspace=workspace,
        config=config,
    )


def _collapse_history(
    messages: list[dict[str, Any]],
    *,
    preserve_messages: int,
    preserve_tokens: int | None,
    metadata: dict[str, Any] | None,
    workspace: str | None,
    config: ContextCompactionConfig,
) -> _CollapseResult:
    sanitized = sanitize_tool_history(copy.deepcopy(messages), warn=False)
    trimmed = trim_history_to_budget(
        sanitized,
        max_messages=preserve_messages,
        max_chars=preserve_tokens,
        size_fn=estimate_message_tokens,
    )
    omitted_count = max(0, len(sanitized) - len(trimmed))
    if omitted_count <= 0:
        return _CollapseResult(messages=sanitized, summary="", omitted_count=0)

    omitted_history = sanitized[:omitted_count]
    summary = _build_collapse_summary(
        omitted_history,
        metadata=metadata,
        workspace=workspace,
        config=config,
    )
    return _CollapseResult(
        messages=trimmed,
        summary=summary,
        omitted_count=omitted_count,
        omitted_history=omitted_history,
    )


async def prepare_model_messages(
    *,
    system_prompt: str,
    history: list[dict[str, Any]],
    chat_id: int | str,
    tool_result_store: ToolResultStore,
    config: ContextCompactionConfig,
    metadata: dict[str, Any] | None = None,
    workspace: str | None = None,
    force_reactive_compact: bool = False,
    llm: Any = None,
    llm_model: str | None = None,
) -> PreparedModelMessages:
    messages = [copy.deepcopy(message) for message in history]
    base_prompt = str(system_prompt or "")
    if not config.enabled:
        est = estimate_prompt_tokens(base_prompt, messages)
        return PreparedModelMessages(
            system_prompt=base_prompt,
            messages=messages,
            estimated_tokens=est,
            budget_status=_prompt_budget_status(est, config),
            local_budget_status=local_budget_status(est, config.max_prompt_tokens),
        )

    previous_summary = ""
    if messages and is_compaction_summary_message(messages[0]):
        previous_summary = unwrap_compaction_summary(
            str(messages[0].get("content", "") or "")
        )
        messages = messages[1:]

    applied_stages: list[str] = []
    summary_sections: list[tuple[str, str]] = []

    # F2: render every compaction summary — the hoisted prior one AND any new
    # stage summaries this turn — under a SINGLE canonical ``## Persisted
    # Compacted Context`` block, byte-identical to how the next turn will hoist
    # it. This guarantees one compaction => one prefix re-warm (ADR 0024) rather
    # than a second re-warm when the heading shape shifts on the following turn.
    def _render_system(extra: tuple[tuple[str, str], ...] = ()) -> str:
        combined = _combine_persisted_summaries(
            previous_summary,
            [*summary_sections, *extra],
            max_tokens=config.max_persisted_summary_tokens,
        )
        return _append_system_summary(
            base_prompt, "## Persisted Compacted Context", combined
        )

    messages, snip_changed = _apply_snip_compaction(messages, config=config)
    if snip_changed:
        applied_stages.append(STAGE_SNIP_COMPACT)

    messages, micro_changed = _apply_micro_compaction(
        messages,
        chat_id=chat_id,
        tool_result_store=tool_result_store,
        config=config,
    )
    if micro_changed:
        applied_stages.append(STAGE_MICRO_COMPACT)

    if force_reactive_compact:
        # D1(a): the pre-reactive-collapse pressure, classified vs the local
        # max_prompt_tokens budget (after this turn's snip/micro). A reactive compact
        # is a 413 backstop, so this reliably lands CRITICAL/BLOCK — the wire should
        # report that, not the deflated post-reactive total.
        pre_compaction_tokens = estimate_prompt_tokens(_render_system(), messages)
        reactive_result = _collapse_history(
            messages,
            preserve_messages=config.reactive_preserve_messages,
            preserve_tokens=config.reactive_preserve_tokens,
            metadata=metadata,
            workspace=workspace,
            config=config,
        )
        if reactive_result.omitted_count > 0:
            messages = reactive_result.messages
            applied_stages.append(STAGE_REACTIVE_COMPACT)
            summary_sections.append(
                ("Reactive Compact Context", reactive_result.summary)
            )
        prompt = _render_system()
        est = estimate_prompt_tokens(prompt, messages)
        return PreparedModelMessages(
            system_prompt=prompt,
            messages=messages,
            estimated_tokens=est,
            applied_stages=tuple(applied_stages),
            persisted_summary=_combine_persisted_summaries(
                previous_summary,
                summary_sections,
                max_tokens=config.max_persisted_summary_tokens,
            ),
            budget_status=_prompt_budget_status(est, config),
            local_budget_status=local_budget_status(est, config.max_prompt_tokens),
            pre_compaction_budget_status=_prompt_budget_status(
                pre_compaction_tokens, config
            ),
        )

    collapse_threshold = _threshold_tokens(
        config.max_prompt_tokens,
        config.collapse_trigger_ratio,
    )
    auto_threshold = _threshold_tokens(
        config.max_prompt_tokens,
        config.auto_compact_trigger_ratio,
    )

    current_tokens = estimate_prompt_tokens(_render_system(), messages)
    # D1(a): the pre-collapse pressure — the value that is compared against the
    # collapse trigger — so the wire status reports it rather than the post-collapse
    # total the stages below drive down to ~target.
    pre_compaction_tokens = current_tokens
    if collapse_threshold is not None and current_tokens > collapse_threshold:
        collapse_result = await _collapse_with_target(
            messages,
            preserve_messages=_preserve_message_target(
                config.collapse_preserve_messages, config.collapse_target_ratio
            ),
            preserve_tokens=config.collapse_preserve_tokens,
            target_ratio=config.collapse_target_ratio,
            max_prompt_tokens=config.max_prompt_tokens,
            section_label="Context Collapse",
            render_system=_render_system,
            metadata=metadata,
            workspace=workspace,
            config=config,
            llm=llm,
            llm_model=llm_model,
        )
        if collapse_result.omitted_count > 0:
            messages = collapse_result.messages
            applied_stages.append(STAGE_CONTEXT_COLLAPSE)
            summary_sections.append(("Context Collapse", collapse_result.summary))
            current_tokens = estimate_prompt_tokens(_render_system(), messages)

    if auto_threshold is not None and current_tokens > auto_threshold:
        auto_result = await _collapse_with_target(
            messages,
            preserve_messages=_preserve_message_target(
                config.auto_compact_preserve_messages, config.auto_compact_target_ratio
            ),
            preserve_tokens=config.auto_compact_preserve_tokens,
            target_ratio=config.auto_compact_target_ratio,
            max_prompt_tokens=config.max_prompt_tokens,
            section_label="Auto Compacted Context",
            render_system=_render_system,
            metadata=metadata,
            workspace=workspace,
            config=config,
            llm=llm,
            llm_model=llm_model,
        )
        if auto_result.omitted_count > 0:
            messages = auto_result.messages
            applied_stages.append(STAGE_AUTO_COMPACT)
            summary_sections.append(("Auto Compacted Context", auto_result.summary))

    prompt = _render_system()
    est = estimate_prompt_tokens(prompt, messages)
    return PreparedModelMessages(
        system_prompt=prompt,
        messages=messages,
        estimated_tokens=est,
        applied_stages=tuple(applied_stages),
        persisted_summary=_combine_persisted_summaries(
            previous_summary,
            summary_sections,
            max_tokens=config.max_persisted_summary_tokens,
        ),
        # S3: the inert window-relative status is retired; both the legacy
        # ``budgetStatus`` wire key and ``localBudgetStatus`` now carry the single
        # actionable token status (App badge becomes useful; wire shape preserved).
        budget_status=_prompt_budget_status(est, config),
        local_budget_status=_prompt_budget_status(est, config),
        pre_compaction_budget_status=_prompt_budget_status(pre_compaction_tokens, config),
    )


@dataclass(frozen=True, slots=True)
class CompactionEvent:
    """One compaction occurrence — emitted to the chat surface as a toast."""

    messages_compressed: int
    tokens_saved_estimate: int
    applied_stages: tuple[str, ...]
    # B3 (§9.3) — surface the already-computed context-budget pressure so a
    # surface can render a "context X% full" badge. local_budget_status (vs
    # max_prompt_tokens) is the actionable one; budget_status (window-relative) is
    # ~always OK on large-window models (B1). Optional/None for back-compat with
    # existing 3-arg constructions and dataclasses.asdict consumers.
    budget_status: "ContextBudgetStatus | str | None" = None
    local_budget_status: "ContextBudgetStatus | str | None" = None


def _budget_status_str(status: "ContextBudgetStatus | str | None") -> str | None:
    """Normalise a budget status to its plain string value, or None.

    Handles a live ``ContextBudgetStatus`` enum (in-process) and a plain string
    (after crossing the dispatcher's ``dataclasses.asdict`` / JSON boundary), so
    the SSE JSON contract is a stable string independent of the str-Enum impl.
    """
    if status is None:
        return None
    if isinstance(status, ContextBudgetStatus):
        return status.value
    text = str(status).strip()
    return text or None


def build_compaction_status_payload(event: CompactionEvent) -> dict[str, Any]:
    """Build the CodePilot-shape SSE 'status' payload for a compaction event.

    Returned dict is the JSON object that goes inside the SSE
    ``data`` field; the caller json.dumps it.
    """
    if event.messages_compressed <= 0:
        if event.tokens_saved_estimate > 0:
            msg = (
                "Context compressed: prompt context trimmed, "
                f"~{event.tokens_saved_estimate:,} tokens saved"
            )
        else:
            msg = "Context compressed: prompt context trimmed"
    elif event.tokens_saved_estimate > 0:
        msg = (
            f"Context compressed: {event.messages_compressed} older "
            f"messages summarized, ~{event.tokens_saved_estimate:,} tokens saved"
        )
    else:
        msg = (
            f"Context compressed: {event.messages_compressed} older "
            "messages summarized"
        )
    payload: dict[str, Any] = {
        "notification": True,
        "subtype": "context_compressed",
        "message": msg,
        "stats": {
            "messagesCompressed": event.messages_compressed,
            "tokensSaved": event.tokens_saved_estimate,
        },
    }
    # B3: add budget-status keys ONLY when present, so the pre-B3 payload shape
    # (asserted byte-for-byte by callers) is unchanged when no window/char budget
    # is configured.
    budget = _budget_status_str(event.budget_status)
    local_budget = _budget_status_str(event.local_budget_status)
    if budget is not None:
        payload["budgetStatus"] = budget
    if local_budget is not None:
        payload["localBudgetStatus"] = local_budget
    return payload
