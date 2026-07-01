"""Surface-agnostic LLM tool-loop body.

``run_engine_loop`` is the post-slash-command, post-preflight-resume
half of the chat request: build the chat context, apply the
identity anchor, wire tool plumbing, and dispatch to
``run_query_engine``. It receives every bot-side dependency through
``EngineDependencies`` so this module never imports from ``bot/``
(enforced by ``tests/test_no_reverse_imports.py``).

Carved out of ``bot/agent_loop.py:llm_tool_loop`` per ADR-0001.
The bot side keeps ownership of slash-command dispatch (Task #8 will
extract it to a registry) and preflight-resume; everything that
follows lives here.
"""

from __future__ import annotations

import os
from typing import Any

from openai import APIError

from omicsclaw.runtime.context.assembler import (
    assemble_chat_context as _assemble_chat_context,
)
from omicsclaw.runtime.tools.hooks import build_default_lifecycle_hook_runtime
from omicsclaw.runtime.policy.state import ToolPolicyState
from omicsclaw.runtime.agent.query_engine import (
    QueryEngineConfig,
    QueryEngineContext,
    run_query_engine,
)
from omicsclaw.runtime.context.budget import CHARS_PER_TOKEN
from omicsclaw.runtime.context.compaction import ContextCompactionConfig
from omicsclaw.runtime.storage.transcript import (
    build_selective_replay_context,
)
from omicsclaw.providers.models import get_context_window
from omicsclaw.providers.patches import provider_has_unreliable_tool_calling

from ._dependencies import EngineDependencies
from ._identity_anchor import (
    apply_model_identity_anchor,
    resolve_effective_model_provider,
)


# Mirrors the constant previously inlined in bot/agent_loop.py.
# Lives here because the engine is the only consumer.
MAX_TOOL_ITERATIONS = int(os.getenv("OMICSCLAW_MAX_TOOL_ITERATIONS", "20"))
DEFAULT_MAX_TOKENS = 8192

LLM_NOT_CONFIGURED_MESSAGE = (
    "⚠ LLM is not configured.\n"
    "\n"
    "Set LLM_API_KEY (or OPENAI_API_KEY) in your environment or "
    ".env file, then restart `oc chat`. To configure interactively, "
    "run `oc onboard`."
)

_MODE_HINTS: dict[str, str] = {
    "code": (
        "You are in code mode. Prefer writing and editing code to "
        "accomplish the user's goals."
    ),
    "plan": (
        "You are in plan mode. Create detailed plans and explain "
        "your reasoning before taking action."
    ),
}


def _maybe_append_mode_hint(system_prompt: str, mode: str) -> str:
    if not mode or mode == "ask":
        return system_prompt
    hint = _MODE_HINTS.get(mode, "")
    if not hint:
        return system_prompt
    return system_prompt.rstrip() + "\n\n## Mode\n" + hint


def _maybe_append_caller_addition(system_prompt: str, addition: str) -> str:
    if not addition:
        return system_prompt
    return system_prompt.rstrip() + "\n\n" + addition.strip()


# BE-PERSONA-7 / ADR 0024 — per-session snapshot of the research-stance persona
# layer. The stance is a SESSION CONSTANT (the prompt prefix must stay byte-stable
# across turns), so we recall core://agent/research_stance once per session and
# reuse it instead of re-recalling every turn. A stance change therefore applies on
# the NEXT session (a deliberate re-warm), not mid-session — which is exactly the
# prefix-stability ADR 0024 wants. The empty (no-stance) result is cached too, so
# the common opt-out path also costs zero per-turn recalls.
_RESEARCH_STANCE_CACHE: dict[str, str] = {}
_RESEARCH_STANCE_CACHE_CAP = 4096


def reset_research_stance_cache() -> None:
    """Drop the per-session research-stance snapshots (tests; a forced re-warm)."""
    _RESEARCH_STANCE_CACHE.clear()


def _make_research_stance_loader(session_manager):
    """BE-PERSONA-7 — a loader that recalls ``core://agent/research_stance`` (the
    agent's research-stance persona layer) through the session store, with the
    store's shared fallback, snapshotting the result per session (see
    ``_RESEARCH_STANCE_CACHE``). Returns ``None`` when memory is unavailable so the
    persona layer degrades to a clean no-op (byte-identical legacy)."""
    store = getattr(session_manager, "store", None)
    if store is None or not hasattr(store, "recall_agent_uri"):
        return None

    async def _load(session_id: str) -> str:
        cached = _RESEARCH_STANCE_CACHE.get(session_id)
        if cached is not None:  # "" is a valid cached value (no stance set)
            return cached
        stance = await store.recall_agent_uri(session_id, "core://agent/research_stance")
        if len(_RESEARCH_STANCE_CACHE) >= _RESEARCH_STANCE_CACHE_CAP:
            _RESEARCH_STANCE_CACHE.clear()  # crude bound; sessions are short-lived
        _RESEARCH_STANCE_CACHE[session_id] = stance
        return stance

    return _load


# Bench (ADR 0020) — lifecycle-stage stance fragments. Additive guidance that
# shapes the stage's stance (read vs. compute vs. write); subordinate to SOUL.md
# and the base persona, it cannot override safety rules. The research-stance
# persona layer + the full 5-layer composer arrive in Phase 4.
_STAGE_FRAGMENTS: dict[str, str] = {
    "read": (
        "You are in the **Read** stage of a research investigation: help the user "
        "read and interpret papers. Answer from ingested sources and cite them; do "
        "not run heavyweight analyses here. If the user wants to compute, propose a "
        "one-click switch to the Analyze stage rather than launching it yourself."
    ),
    "ideate": (
        "You are in the **Ideate** stage: turn the thread's reading into testable, "
        "source-grounded hypotheses. Never fabricate a citation; flag an ungrounded "
        "hunch as such."
    ),
    "analyze": (
        "You are in the **Analyze** stage: run OmicsClaw skills on the thread's data "
        "and ground every result in real artifact values. When the user asks you to "
        "test a hypothesis, first build a handoff packet with kg_build_packet (pass the "
        "hypothesis slug), run the appropriate skill, then record the outcome with "
        "kg_record_result (verdict + a one-line summary from the real artifacts) so the "
        "Ideate stage can suggest a verdict for the user to confirm."
    ),
    "write": (
        "You are in the **Write** stage: draft from recorded analysis lineage and "
        "cited sources; preserve numbers verbatim and cite every claim."
    ),
}


def _maybe_append_stage_fragment(system_prompt: str, stage: str) -> str:
    """Append the Bench lifecycle-stage stance fragment (ADR 0020).

    Empty / unknown stage = no fragment, so the legacy / non-Bench path is
    byte-unchanged.
    """
    if not stage:
        return system_prompt
    fragment = _STAGE_FRAGMENTS.get(stage, "")
    if not fragment:
        return system_prompt
    return system_prompt.rstrip() + "\n\n## Stage\n" + fragment


# ADR 0024 — derive the context-collapse char budget from the model's window.
# Phase 3 made history append-only between collapses, removing the per-turn
# slide that used to bound small-context providers; this re-introduces a safe
# bound. Conservative blend (English ~4 chars/tok, CJK ~1.5) and half the window
# reserved for completion + headroom. We never EXCEED the proven default (no risk
# from an over-optimistic reported window), only shrink for known-small windows.
# Unknown windows (e.g. Ollama, which report None) keep the default; operators
# tune those via OMICSCLAW_MAX_PROMPT_CHARS. Reactive compaction on a context
# error remains the ultimate safety net.
_CHARS_PER_TOKEN = CHARS_PER_TOKEN
_PROMPT_BUDGET_FRACTION = 0.5
_DEFAULT_MAX_PROMPT_CHARS = 96000

# §9.3 slice 3 — compress-to-target. After a collapse/auto compaction, converge
# the TOTAL prompt (system + preserved tail) to this fraction of max_prompt_chars,
# so the target scales with the model's real char budget instead of the old fixed
# 12000/6000-char tail. Both sit well below their trigger ratios (0.82 / 0.92) so
# the re-warmed next turn cannot re-collapse (F2 one-compaction = one-rewarm), and
# auto keeps less than collapse.
_COLLAPSE_TARGET_RATIO = 0.55
_AUTO_COMPACT_TARGET_RATIO = 0.40


def resolve_max_prompt_chars(model: str) -> int:
    """Context-collapse char budget for ``model`` (ADR 0024)."""
    override = (os.environ.get("OMICSCLAW_MAX_PROMPT_CHARS") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    window = get_context_window(model)
    if not window or window <= 0:
        return _DEFAULT_MAX_PROMPT_CHARS
    derived = int(window * _CHARS_PER_TOKEN * _PROMPT_BUDGET_FRACTION)
    return min(_DEFAULT_MAX_PROMPT_CHARS, derived)


def _build_compaction_config(effective_model: str) -> ContextCompactionConfig:
    """Assemble the per-request compaction config for ``effective_model``.

    ADR 0024 — collapse budget scaled to the model's context window. §9.3 —
    observational token-budget status (context_window_tokens) plus budget-relative
    compress-to-target ratios (slice 3).
    """
    return ContextCompactionConfig(
        max_prompt_chars=resolve_max_prompt_chars(effective_model),
        context_window_tokens=get_context_window(effective_model),
        collapse_target_ratio=_COLLAPSE_TARGET_RATIO,
        auto_compact_target_ratio=_AUTO_COMPACT_TARGET_RATIO,
    )


def _prepend_user_turn_context(content: str | list, addition: str) -> str | list:
    """Prepend per-turn Volatile context (ADR 0024) to the user message.

    The Analysis Router's route context, autonomous understanding, and
    assisted-parameterization context are query-volatile, so they ride the
    user turn — frozen append-only into history — rather than the system
    prefix, keeping the Prompt prefix byte-stable across turns. Handles both
    plain-string and multimodal (list-of-parts) user content.
    """
    if not addition or not addition.strip():
        return content
    block = addition.strip()
    if isinstance(content, list):
        return [{"type": "text", "text": block}, *content]
    return f"{block}\n\n{content}"


async def run_engine_loop(
    *,
    deps: EngineDependencies,
    chat_id: int | str,
    user_content: str | list,
    user_id: str | None = None,
    platform: str | None = None,
    plan_context: str = "",
    workspace: str = "",
    pipeline_workspace: str = "",
    scoped_memory_scope: str = "",
    mcp_servers: tuple[str, ...] | None = None,
    output_style: str = "",
    progress_fn: Any = None,
    progress_update_fn: Any = None,
    on_tool_call: Any = None,
    on_tool_result: Any = None,
    on_stream_content: Any = None,
    on_stream_reasoning: Any = None,
    on_context_compacted: Any = None,
    on_pathology_signal: Any = None,
    model_override: str = "",
    extra_api_params: dict | None = None,
    max_tokens_override: int = 0,
    system_prompt_append: str = "",
    user_turn_context: str = "",
    mode: str = "",
    # Bench (ADR 0018/0020) — investigation thread + lifecycle stage lens.
    thread_id: str = "",
    stage: str = "",
    request_tool_approval: Any = None,
    policy_state: Any = None,
    cancel_event: Any = None,
) -> str:
    """Drive the LLM-plus-tools loop for a single chat turn.

    ``user_turn_context`` (ADR 0024) carries per-turn Volatile context — the
    Analysis Router's route context, autonomous understanding, and assisted
    parameterization — which is prepended to the user message rather than the
    system prefix, so the prefix stays cache-stable. ``system_prompt_append``
    remains a system-prefix addition for callers that genuinely want one.

    Returns the assistant's final user-facing reply.

    ``cancel_event`` (ADR 0009) is a ``threading.Event`` set by the
    Surface to request mid-flight cancellation. When provided, it is
    injected into ``tool_runtime_context`` so per-tool executors can
    forward it down to ``skill.runner.run_skill``.
    """
    if deps.llm is None:
        return LLM_NOT_CONFIGURED_MESSAGE

    transcript_store = deps.transcript_store
    transcript_store.max_history = deps.max_history
    transcript_store.max_history_chars = deps.max_history_chars
    transcript_store.max_conversations = deps.max_conversations

    transcript_context = build_selective_replay_context(
        transcript_store.get_history(chat_id),
        metadata=(
            {"pipeline_workspace": pipeline_workspace} if pipeline_workspace else None
        ),
        workspace=workspace,
        max_messages=transcript_store.max_history,
        max_chars=transcript_store.max_history_chars,
        sanitizer=transcript_store.sanitizer,
    )

    chat_context = await _assemble_chat_context(
        chat_id=chat_id,
        user_content=user_content,
        user_id=user_id,
        platform=platform,
        # Bench (AN-CTXRECALL-11) — scope the passive per-turn memory injection
        # to the active investigation thread (dataset/analysis only; global
        # prefs/insights/project_context stay shared). Empty = legacy unscoped.
        thread_id=thread_id,
        session_manager=deps.session_manager,
        # F3: no system_prompt_builder — the single injector assembly (with
        # research_stance folded in) is byte-equivalent to the old legacy
        # builder, so the redundant second assembly is dropped for this path.
        # Bench BE-PERSONA-7 — inject the agent's research-stance persona layer
        # (core://agent/research_stance); None loader / absent row = no-op.
        research_stance_loader=_make_research_stance_loader(deps.session_manager),
        skill_aliases=deps.skill_aliases,
        plan_context=plan_context,
        transcript_context=transcript_context,
        omicsclaw_dir=deps.omicsclaw_dir,
        workspace=workspace,
        pipeline_workspace=pipeline_workspace,
        scoped_memory_scope=scoped_memory_scope,
        mcp_servers=tuple(mcp_servers or ()),
        output_style=output_style,
    )

    effective_model, effective_provider = resolve_effective_model_provider(
        model_override, deps.omicsclaw_model, deps.llm_provider_name
    )
    system_prompt = apply_model_identity_anchor(
        chat_context.system_prompt, effective_model, effective_provider
    )
    system_prompt = _maybe_append_caller_addition(system_prompt, system_prompt_append)
    system_prompt = _maybe_append_mode_hint(system_prompt, mode)
    # Bench (ADR 0020) — append the lifecycle-stage stance fragment (additive,
    # subordinate to SOUL.md + base persona; empty/unknown stage = no-op).
    system_prompt = _maybe_append_stage_fragment(system_prompt, stage)

    # ADR 0024 — freeze the tool list by surface (a session constant), not by
    # per-turn query predicates. Byte-identical across turns ⇒ the tool segment
    # of the Prompt prefix stays cache-stable. Cache diagnostics will flip to
    # ``tool-list-changed`` if anything re-introduces per-turn tool variation.
    # Bench (ADR 0020) — sub-filter the (otherwise cache-stable) surface tool
    # list to the active lifecycle stage's default subset. Empty / analyze /
    # unknown stage is unfiltered, so the prompt-prefix tool segment stays
    # byte-stable for the legacy / non-Bench path (ADR 0024).
    request_tools = tuple(
        deps.tool_registry.to_openai_tools_for_request(
            chat_context.prompt_context.request,
            surface_only=True,
            stage=stage,
        )
    )
    hook_runtime = build_default_lifecycle_hook_runtime(deps.omicsclaw_dir)

    callbacks = deps.callbacks_builder(
        chat_id=chat_id,
        progress_fn=progress_fn,
        progress_update_fn=progress_update_fn,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        on_stream_content=on_stream_content,
        on_stream_reasoning=on_stream_reasoning,
        request_tool_approval=request_tool_approval,
        audit_fn=deps.audit_fn,
        deep_learning_methods=deps.deep_learning_methods,
        usage_accumulator=deps.usage_accumulator,
        on_context_compacted=on_context_compacted,
        on_pathology_signal=on_pathology_signal,
    )

    # Surface flows into audit/metrics; an absent platform is caller misuse,
    # not bot traffic — keep it visible as "unknown" instead of masquerading.
    surface = platform or "unknown"
    resolved_policy_state = ToolPolicyState.from_mapping(policy_state, surface=surface)

    return await run_query_engine(
        llm=deps.llm,
        context=QueryEngineContext(
            chat_id=chat_id,
            session_id=chat_context.session_id,
            system_prompt=system_prompt,
            user_message_content=_prepend_user_turn_context(
                chat_context.user_message_content, user_turn_context
            ),
            surface=surface,
            policy_state=resolved_policy_state,
            hook_runtime=hook_runtime,
            tool_runtime_context={
                "omicsclaw_dir": deps.omicsclaw_dir,
                "workspace": workspace,
                "pipeline_workspace": pipeline_workspace,
                # Bench (ADR 0018/0020) — investigation-thread id + stage lens
                # ride into per-tool executors. Phase 0: observable but inert
                # (no consumer yet). Phase 1A reads thread_id to scope
                # analysis://<thread_id>; Phase 2 reads stage for tool gating.
                "thread_id": thread_id,
                "stage": stage,
                # ADR 0009 — surface-initiated cancel propagates through
                # this dict into per-tool executors that forward it to
                # skill.runner.run_skill(cancel_event=...).
                "cancel_event": cancel_event,
            },
            request_tools=request_tools,
        ),
        tool_runtime=deps.tool_runtime,
        transcript_store=transcript_store,
        tool_result_store=deps.tool_result_store,
        config=QueryEngineConfig(
            model=model_override or deps.omicsclaw_model,
            max_iterations=MAX_TOOL_ITERATIONS,
            max_tokens=(
                max_tokens_override if max_tokens_override > 0 else DEFAULT_MAX_TOKENS
            ),
            llm_error_types=(APIError,),
            extra_api_params=extra_api_params or {},
            context_compaction=_build_compaction_config(effective_model),
            deepseek_reasoning_passback=(
                (deps.llm_provider_name or "").strip().lower() == "deepseek"
            ),
            # ADR 0027 — only local providers (Ollama) that silently truncate
            # and miss tool calls get the phantom-completion nudge.
            phantom_completion_guard=provider_has_unreliable_tool_calling(
                deps.llm_provider_name
            ),
        ),
        callbacks=callbacks,
    )
