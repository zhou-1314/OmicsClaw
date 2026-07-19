"""Multi-round LLM dispatch loop — the agent's central control flow.

Carved out of ``bot/core.py`` per ADR 0001 (#121, the final bot/core
slice). Every User-facing entry (bot channels, ``omicsclaw/app/server.py``,
``omicsclaw/interactive/interactive.py``) walks through ``llm_tool_loop``
to interleave LLM completions with tool execution until the model emits
a final user-facing message.

Cross-module access:

* Stable omicsclaw.runtime.agent.state symbols (``OUTPUT_DIR``, ``OMICSCLAW_DIR``,
  ``transcript_store``, ``tool_result_store``,
  ``pending_preflight_requests``, ``audit``, ``MAX_HISTORY``,
  ``MAX_CONVERSATIONS``, ...) imported at module top.
* Runtime-reassigned globals (``llm``, ``OMICSCLAW_MODEL``,
  ``LLM_PROVIDER_NAME``, ``memory_store``, ``session_manager``)
  accessed via ``_core.<name>`` at call time. ``omicsclaw.runtime.agent.session.init()``
  sets them after modules finish loading.
* Sibling helpers (``omicsclaw.skill.orchestration``,
  ``omicsclaw.runtime.tools.builders.agent_executors``) imported from canonical homes.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import APIError, AsyncOpenAI, OpenAIError

# Late-binding handle for runtime-mutated omicsclaw.runtime.agent.state globals.
import omicsclaw.runtime.agent.state as _core

# Stable omicsclaw.runtime.agent.state symbols.
from omicsclaw.runtime.agent.state import (
    BOT_START_TIME,
    DATA_DIR,
    DEEP_LEARNING_METHODS,
    EXAMPLES_DIR,
    MAX_CONVERSATIONS,
    MAX_HISTORY,
    MAX_HISTORY_CHARS,
    OMICSCLAW_DIR,
    OUTPUT_DIR,
    _primary_skill_count,
    _skill_registry,
    audit,
    format_skills_table,
    pending_preflight_requests,
    pending_candidate_chain_confirmations,
    tool_result_store,
    transcript_store,
)
from omicsclaw.services.billing import accumulate_usage as _accumulate_usage
from omicsclaw.surfaces.channels.commands import SlashCommandContext
from omicsclaw.surfaces.channels.commands import dispatch as _dispatch_slash_command
from omicsclaw.runtime.agent.parameter_loop import (
    _apply_preflight_answers,
    _build_pending_preflight_message,
    _extract_pending_preflight_payload,
    _is_affirmative_preflight_confirmation,
    _parse_preflight_reply,
    _preflight_payload_needs_reply,
    _remember_pending_preflight_request,
)
from omicsclaw.runtime.tools.builders.agent_executors import (
    _build_tool_runtime,
    execute_omicsclaw,
    get_tool_executors,
    get_tool_runtime,
)

from omicsclaw.common.user_guidance import strip_user_guidance_lines
from omicsclaw.providers.timeout import build_llm_timeout_policy
from omicsclaw.engine import (
    EngineDependencies,
    apply_model_identity_anchor,
    resolve_effective_model_provider,
    run_engine_loop,
)
from omicsclaw.runtime.tools.builders.agent import (
    BotToolContext,
    build_bot_tool_registry,
)
from omicsclaw.runtime.context.assembler import (
    assemble_chat_context as _assemble_chat_context,
    extract_user_text as _extract_user_text,
)
from omicsclaw.analysis_router import (
    AnalysisRoute,
    AnalysisRouteKind,
    extract_valid_input_paths,
    route_analysis_request,
)
from omicsclaw.skill.capability_resolver import CapabilityDecision
from omicsclaw.runtime.policy.policy import TOOL_POLICY_ALLOW
from omicsclaw.runtime.agent.query_engine import (
    QueryEngineCallbacks,
    QueryEngineConfig,
    run_query_engine,
)
from omicsclaw.runtime.context.system_prompt import build_system_prompt
from omicsclaw.runtime.tools.orchestration import (
    EXECUTION_STATUS_POLICY_BLOCKED,
    ToolExecutionRequest,
)
from omicsclaw.runtime.tools.spec import PROGRESS_POLICY_ANALYSIS
from omicsclaw.runtime.storage.transcript import (
    build_selective_replay_context,
    sanitize_tool_history as _runtime_sanitize_tool_history,
)

logger = logging.getLogger("omicsclaw.omicsclaw.runtime.agent.loop")


# ---------------------------------------------------------------------------
# System prompt + tool-registry hooks
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: str = ""

def _ensure_system_prompt():
    global SYSTEM_PROMPT
    if not SYSTEM_PROMPT:
        SYSTEM_PROMPT = build_system_prompt(omicsclaw_dir=str(OMICSCLAW_DIR))

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

def get_tools() -> list[dict]:
    return list(get_tool_runtime().openai_tools)


def _build_bot_tool_context() -> BotToolContext:
    """Thin alias around the canonical ``build_default_bot_tool_context``
    in ``omicsclaw/runtime/bot_tools.py``. Kept as a module-private hook
    so other bot/core.py callers can monkeypatch in tests if needed —
    do not inline this call site away."""
    from omicsclaw.runtime.tools.builders.agent import build_default_bot_tool_context

    return build_default_bot_tool_context()


def get_tool_registry():
    return build_bot_tool_registry(_build_bot_tool_context())


def _build_llm_timeout():
    """Build the shared timeout policy for the AsyncOpenAI client."""
    return build_llm_timeout_policy(log=logger).as_httpx_timeout()


MAX_TOOL_ITERATIONS = int(os.getenv("OMICSCLAW_MAX_TOOL_ITERATIONS", "20"))  # Increased from 10, configurable


# ---------------------------------------------------------------------------
# LLM tool loop
# ---------------------------------------------------------------------------

def _format_analysis_route_context(route: AnalysisRoute) -> str:
    """Render route metadata for the existing chat engine.

    This is the first, non-disruptive integration slice: the router result is
    made visible to the LLM, but execution still flows through the current
    engine until exact/partial/no-skill executors land.
    """
    if route.kind is AnalysisRouteKind.CHAT:
        return ""

    decision = route.capability_decision
    lines = [
        "## Analysis Router",
        f"- route_kind: {route.kind.value}",
        f"- coverage: {decision.coverage}",
        f"- chosen_skill: {route.chosen_skill or 'none'}",
        f"- domain: {decision.domain or 'unknown'}",
        f"- confidence: {round(route.confidence, 3)}",
        f"- should_search_web: {route.should_search_web}",
    ]
    if decision.missing_capabilities:
        lines.append("- missing_capabilities: " + "; ".join(decision.missing_capabilities))
    candidate_chain = route.metadata.get("candidate_chain") or {}
    if candidate_chain:
        if candidate_chain.get("validated_order"):
            lines.append(
                "- candidate_topo_chain: "
                + " -> ".join(candidate_chain.get("skills", []))
            )
        else:
            lines.append(
                "- unresolved_candidate_intents: "
                + "; ".join(candidate_chain.get("requested_skills", []))
            )
            lines.append(
                f"- unresolved_dependency_pairs: {len(candidate_chain.get('unresolved_pairs', []))}"
            )
        lines.append(f"- candidate_plan_digest: {route.metadata.get('plan_digest', '')}")
        lines.append(
            f"- candidate_chain_edges: {len(candidate_chain.get('edges', []))} provenance records"
        )
        resource_ready = candidate_chain.get("resource_ready") is True
        lines.append(f"- candidate_resource_ready: {str(resource_ready).lower()}")
        missing_resources = candidate_chain.get("missing_resource_requests") or []
        if missing_resources:
            lines.append(
                "- missing_resource_requests: "
                + "; ".join(str(skill) for skill in missing_resources)
            )
        if not resource_ready:
            lines.append(
                "- candidate_plan_execution: blocked; do not ask for confirmation "
                "or execute until every skill declares a complete compute reservation"
            )
    if route.preflight_required:
        lines.extend(
            [
                "- preflight_required: true",
                f"- precondition_status: {decision.precondition_status}",
            ]
        )
        if decision.missing_preconditions:
            lines.append(
                "- missing_preconditions: "
                + "; ".join(decision.missing_preconditions)
            )
        if decision.recommended_preparation:
            lines.append(
                "- recommended_preparation: "
                + "; ".join(decision.recommended_preparation)
            )
        lines.append(
            "- execution_rule: do not execute the selected skill until preflight succeeds"
        )
    elif candidate_chain and route.metadata.get("requires_confirmation") is True:
        lines.append(
            "- execution_rule: candidate plan only; confirm the complete chain before executing any step"
        )
    elif candidate_chain:
        lines.append(
            "- execution_rule: candidate plan is not executable; resolve dependency "
            "and resource contract gaps first"
        )
    elif route.kind is AnalysisRouteKind.EXACT_SKILL:
        lines.append(
            "- execution_rule: deterministic route, assisted parameterization"
        )
    elif route.kind is AnalysisRouteKind.PARTIAL_SKILL:
        lines.append(
            "- execution_rule: skill-first composition; do not rewrite the matched skill"
        )
    elif route.kind is AnalysisRouteKind.NO_SKILL:
        lines.append(
            "- execution_rule: autonomous analysis path; execute through the autonomous code runner"
        )
    return "\n".join(lines)


def _merge_system_prompt_additions(*additions: str) -> str:
    return "\n\n".join(
        addition.strip()
        for addition in additions
        if str(addition or "").strip()
    )


def _route_user_text_with_input_state(user_text: str) -> AnalysisRoute:
    """Route text with a cached, read-only profile of its first trusted input."""
    try:
        input_paths = extract_valid_input_paths(user_text)
    except Exception as exc:
        logger.warning("Input-state path extraction failed (non-fatal): %s", exc)
        input_paths = []
    if not input_paths:
        return route_analysis_request(user_text)

    input_path = input_paths[0]
    from omicsclaw.skill.preconditions import probe_input_profile

    return route_analysis_request(
        user_text,
        file_path=input_path,
        input_profile=probe_input_profile(input_path),
    )


def _build_analysis_route_context(user_content: str | list) -> str:
    user_text = _extract_user_text(user_content)
    try:
        route = _route_user_text_with_input_state(user_text)
    except Exception as exc:
        logger.warning("Analysis Router context failed (non-fatal): %s", exc)
        return ""
    return _format_analysis_route_context(route)


_CANDIDATE_PLAN_CONFIRMATIONS = frozenset(
    {
        "y",
        "yes",
        "yes please",
        "yes confirm",
        "yes confirm this plan",
        "yes go ahead",
        "ok",
        "okay",
        "confirm",
        "confirmed",
        "confirm this plan",
        "accept",
        "accept this plan",
        "continue",
        "continue with this plan",
        "proceed",
        "proceed with this plan",
        "go ahead",
        "确认",
        "确认执行",
        "确认这个计划",
        "确认该计划",
        "可以",
        "可以执行",
        "继续",
        "接受",
        "同意",
        "同意执行",
    }
)


def _is_explicit_candidate_plan_confirmation(user_text: str) -> bool:
    """Accept only a short standalone reply, never a matching substring."""

    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", user_text.lower()).strip()
    normalized = " ".join(normalized.split())
    return normalized in _CANDIDATE_PLAN_CONFIRMATIONS


def _candidate_chain_gate_for_turn(
    chat_id: int | str,
    user_text: str,
    route: AnalysisRoute,
) -> dict:
    """Bind a composite plan confirmation to one chat and one plan digest."""

    if route.metadata.get("requires_confirmation"):
        candidate_chain = dict(route.metadata.get("candidate_chain") or {})
        gate = {
            "plan_digest": str(route.metadata.get("plan_digest") or ""),
            "skills": list(candidate_chain.get("skills", [])),
            "candidate_chain": candidate_chain,
            "confirmed": False,
        }
        pending_candidate_chain_confirmations[chat_id] = dict(gate)
        return gate

    pending = pending_candidate_chain_confirmations.get(chat_id)
    if not pending:
        return {}

    lowered = user_text.strip().lower()
    if any(
        marker in lowered
        for marker in ("cancel", "reject", "stop", "取消", "拒绝", "停止", "不执行")
    ):
        pending_candidate_chain_confirmations.pop(chat_id, None)
        return dict(pending) | {"confirmed": False}
    if route.kind is not AnalysisRouteKind.CHAT:
        # A new concrete analysis request replaces the pending plan.
        pending_candidate_chain_confirmations.pop(chat_id, None)
        return {}
    if _is_explicit_candidate_plan_confirmation(user_text):
        confirmed = dict(pending) | {"confirmed": True}
        pending_candidate_chain_confirmations[chat_id] = confirmed
        return confirmed

    # Keep the exact digest and its current confirmation state across chat
    # turns, so an interrupted plan cannot silently lose or gain authority.
    return dict(pending)


_AUTONOMOUS_UNDERSTANDING_DIRECTIVE = (
    "## Autonomous Understanding Preflight\n"
    "This request matches no exact skill but carries a real input file, so it will run\n"
    "through the autonomous code path. OmicsClaw has already inspected the file; the\n"
    "deterministic data schema is included below. Before writing or running any code:\n"
    "1. Form a concrete, data-grounded analysis plan from the schema (use the real\n"
    "   obs/var/obsm/layers keys, shape, and platform — never guess keys).\n"
    "2. Ask the user ONE focused question ONLY on consequential ambiguity (a choice that\n"
    "   materially changes the result and cannot be safely defaulted). Otherwise proceed\n"
    "   with documented defaults and explicitly state your assumptions.\n"
    "3. Call `autonomous_analysis_execute`, passing `analysis_plan` (your plan) and\n"
    "   `data_schema` (the schema below) so generated code and repairs use real keys.\n"
    "4. After it returns, judge whether the produced artifacts satisfy your plan; if they\n"
    "   do not, re-delegate a corrected run rather than trusting exit code 0.\n"
    "5. Then interpret the results scientifically — separate computed results from\n"
    "   interpretive claims, cite concrete artifacts, and keep the OmicsClaw disclaimer."
)


def _format_autonomous_understanding_block(schema_report: str) -> str:
    return "\n\n".join([_AUTONOMOUS_UNDERSTANDING_DIRECTIVE, schema_report.strip()])


_EXACT_SKILL_ASSISTED_PARAM_DIRECTIVE = (
    "## Assisted Parameterization (Exact skill match)\n"
    "This request maps to one built-in skill (its SKILL.md method menu is included\n"
    "below); the *skill* choice is fixed. Your job is to recommend *how* to run it —\n"
    "the method and key parameters — grounded in that method menu and, when present,\n"
    "the inspected data schema below. Before calling the `omicsclaw` tool:\n"
    "1. Always show a short recommendation: the chosen method, a one-line rationale,\n"
    "   and any materially different alternative method.\n"
    "2. If the user named a specific (valid) method, use it as-is — do not second-guess.\n"
    "3. If the best method is clear or safe, proceed: call `omicsclaw` with that method\n"
    "   and key parameters, explicitly stating the assumptions you made.\n"
    "4. Ask the user ONE focused question ONLY when the choice is genuinely consequential\n"
    "   (methods differ materially in result or cost and the request does not\n"
    "   disambiguate). Otherwise do not interrogate — proceed with documented defaults.\n"
    "5. If the inspected data is missing a precondition the method needs (e.g. no\n"
    "   `obsm['X_pca']` / `obsm['spatial']`), do not run — state what is missing and the\n"
    "   remediation (e.g. run the preprocessing skill first).\n"
    "Recommend only WITHIN this skill: never silently fall back to an undocumented\n"
    "default, and never switch skills (surface a near-tie as a suggestion, not a swap)."
)


def _format_exact_skill_assisted_param_block(skill_md: str, schema_report: str) -> str:
    parts = [_EXACT_SKILL_ASSISTED_PARAM_DIRECTIVE]
    if skill_md.strip():
        parts.append("### Matched skill — method menu (SKILL.md)\n\n" + skill_md.strip())
    if schema_report.strip():
        parts.append(schema_report.strip())
    return "\n\n".join(parts)


async def _build_autonomous_understanding_context(user_content: str | list) -> str:
    """Deterministic data-inspection preflight for autonomous routes (ADR 0014).

    When a request routes to ``no_skill`` / ``partial_skill`` and carries a
    trusted ``.h5ad`` input path, OmicsClaw inspects it up front and injects the
    real schema plus a plan/validate/interpret directive, so the outer LLM
    understands the data before generating code. Returns ``""`` (a no-op that
    leaves the base route context untouched) for chat and exact-skill routes,
    when no trusted file path is present, or when inspection is unavailable.
    """
    user_text = _extract_user_text(user_content)
    if not user_text.strip():
        return ""
    try:
        route = route_analysis_request(user_text)
    except Exception as exc:
        logger.warning("Autonomous understanding routing failed (non-fatal): %s", exc)
        return ""
    if route.kind not in (AnalysisRouteKind.NO_SKILL, AnalysisRouteKind.PARTIAL_SKILL):
        return ""
    if route.metadata.get("candidate_chain"):
        # A composite built-in plan is not an uncovered-code fallback. Keep the
        # autonomous executor out of both the prompt and confirmation seam.
        return ""
    try:
        input_paths = extract_valid_input_paths(user_text)
    except Exception:
        input_paths = []
    if not input_paths:
        return ""
    try:
        from omicsclaw.runtime.tools.builders.agent_executors import (
            execute_inspect_data,
        )

        schema_report = await execute_inspect_data({"file_path": input_paths[0]})
    except Exception as exc:
        logger.warning(
            "Autonomous understanding preflight inspect_data failed (non-fatal): %s",
            exc,
        )
        return ""
    # ``execute_inspect_data`` only profiles .h5ad and returns an error string
    # otherwise; only inject when we have a real schema report.
    if not str(schema_report or "").startswith("## Data Inspection"):
        return ""
    return _format_autonomous_understanding_block(schema_report)


async def _build_exact_skill_assisted_param_context(user_content: str | list) -> str:
    """Data-grounded assisted parameterization for Exact skill matches (ADR 0015).

    When a request routes to ``exact_skill``, inject the matched
    skill's SKILL.md method menu plus — when a trusted input file is present —
    its ``inspect_data`` schema, and direct the outer LLM to recommend the method
    and key parameters *within that skill* (always showing the recommendation;
    asking one focused question only on consequential ambiguity). Returns ``""``
    for chat / partial / no-skill routes, or when neither
    a method menu nor a schema is available — leaving the route context untouched.

    This is the exact-skill analogue of ``_build_autonomous_understanding_context``;
    the two are mutually exclusive on ``route.kind``, so at most one ``inspect_data``
    round-trip fires per turn.
    """
    user_text = _extract_user_text(user_content)
    if not user_text.strip():
        return ""
    try:
        route = _route_user_text_with_input_state(user_text)
    except Exception as exc:
        logger.warning("Assisted-parameterization routing failed (non-fatal): %s", exc)
        return ""
    if route.kind is not AnalysisRouteKind.EXACT_SKILL or not route.chosen_skill:
        return ""

    try:
        from omicsclaw.skill.orchestration import load_skill_md

        skill_md = load_skill_md(route.chosen_skill)
    except Exception as exc:
        logger.warning("load_skill_md failed (non-fatal): %s", exc)
        skill_md = ""

    schema_report = ""
    try:
        input_paths = extract_valid_input_paths(user_text)
    except Exception:
        input_paths = []
    if input_paths:
        try:
            from omicsclaw.runtime.tools.builders.agent_executors import (
                execute_inspect_data,
            )

            report = await execute_inspect_data({"file_path": input_paths[0]})
            if str(report or "").startswith("## Data Inspection"):
                schema_report = report
        except Exception as exc:
            logger.warning(
                "Assisted-parameterization inspect_data failed (non-fatal): %s", exc
            )

    if not skill_md and not schema_report:
        return ""
    return _format_exact_skill_assisted_param_block(skill_md, schema_report)


def _build_engine_dependencies(
    *, usage_accumulator=None, transcript_store_override=None
) -> EngineDependencies:
    def _bind_callbacks_builder(**engine_kwargs):
        # Engine doesn't carry the bot's logger; bind it here so the
        # callback builder still gets the right ``logger_obj`` arg.
        return _build_bot_query_engine_callbacks(logger_obj=logger, **engine_kwargs)

    return EngineDependencies(
        transcript_store=(
            transcript_store
            if transcript_store_override is None
            else transcript_store_override
        ),
        tool_result_store=tool_result_store,
        llm=_core.llm,
        omicsclaw_model=_core.OMICSCLAW_MODEL or "",
        llm_provider_name=_core.LLM_PROVIDER_NAME or "",
        session_manager=_core.session_manager,
        omicsclaw_dir=str(OMICSCLAW_DIR),
        max_history=MAX_HISTORY,
        max_history_chars=MAX_HISTORY_CHARS or None,
        max_conversations=MAX_CONVERSATIONS,
        audit_fn=audit,
        usage_accumulator=usage_accumulator or _accumulate_usage,
        skill_aliases=tuple(_skill_registry().skills.keys()),
        deep_learning_methods=DEEP_LEARNING_METHODS,
        tool_runtime=_build_tool_runtime(),
        tool_registry=get_tool_registry(),
        callbacks_builder=_bind_callbacks_builder,
    )


def _format_llm_api_error_message(exc: Exception) -> str:
    detail = str(exc).strip() or type(exc).__name__
    provider = (_core.LLM_PROVIDER_NAME or "").strip().lower()
    base_url = ""
    try:
        from omicsclaw.providers.runtime import get_active_provider_runtime

        runtime = get_active_provider_runtime()
        base_url = str(getattr(runtime, "base_url", "") or "").strip()
    except Exception:
        base_url = ""
    if not base_url:
        base_url = str(
            os.getenv("LLM_BASE_URL", "") or os.getenv("OMICSCLAW_BASE_URL", "") or ""
        ).strip()

    if provider == "custom":
        endpoint_hint = (
            f" Custom endpoint base_url is `{base_url}`."
            if base_url
            else " Custom endpoint base_url is empty."
        )
        return (
            "LLM provider request failed for the custom endpoint:"
            f" {detail}.{endpoint_hint} Ensure the base URL is the "
            "OpenAI-compatible API root, commonly ending in `/v1`, not the "
            "provider dashboard or homepage."
        )

    # Ollama-specific: translate the upstream "does not support tools"
    # 400 into actionable guidance. OmicsClaw needs function-calling for
    # every turn, so models like deepseek-r1 / gemma3 can't be used as the
    # agent's primary model regardless of how capable they are otherwise.
    if provider == "ollama" and "does not support tools" in detail.lower():
        active_model = str(getattr(_core, "OMICSCLAW_MODEL", "") or "")
        model_hint = f" `{active_model}`" if active_model else ""
        return (
            f"The selected Ollama model{model_hint} does not support tool "
            "calling, which OmicsClaw requires for every turn. Pick a "
            "tool-capable model — e.g. `qwen2.5:7b`, `qwen3:8b`, "
            "`llama3.1:8b`, `llama3.3:70b`, `gemma4:e4b`, `mistral`, or "
            "`command-r` — via Settings → Provider, or by setting "
            "OMICSCLAW_MODEL. Reasoning-only models (deepseek-r1) and "
            "older Gemma versions (gemma2, gemma3) lack tool support; "
            "Gemma 4 added native function calling and works."
        )

    return f"Sorry, I'm having trouble thinking right now -- API error: {detail}"


def _sanitize_tool_history(history: list[dict], warn: bool = True) -> list[dict]:
    return _runtime_sanitize_tool_history(history, warn=warn)


def _normalize_tool_callback_args(callback, args: tuple) -> tuple:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return args

    positional_capacity = 0
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return args
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_capacity += 1
    return args[:positional_capacity]


async def _emit_tool_callback(callback, *args) -> None:
    if not callback:
        return
    callback_args = _normalize_tool_callback_args(callback, args)
    if asyncio.iscoroutinefunction(callback):
        await callback(*callback_args)
    else:
        callback(*callback_args)


def _coerce_timeout_seconds(value) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return max(1, round(seconds))


def _extract_timeout_seconds_from_text(text: str) -> int | None:
    if not text:
        return None

    patterns = (
        r"timed out after (?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b",
        r"timeout after (?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b",
    )
    lowered = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered, re.IGNORECASE)
        if not match:
            continue
        seconds = _coerce_timeout_seconds(match.group("seconds"))
        if seconds is not None:
            return seconds
    return None


def _extract_tool_timeout_seconds(execution_result, display_output) -> int | None:
    error = getattr(execution_result, "error", None)
    if error is not None:
        for attr_name in (
            "timeout",
            "timeout_seconds",
            "elapsed_seconds",
            "elapsed_time_seconds",
            "seconds",
        ):
            seconds = _coerce_timeout_seconds(getattr(error, attr_name, None))
            if seconds is not None:
                return seconds

        seconds = _extract_timeout_seconds_from_text(str(error))
        if seconds is not None:
            return seconds

    display_text = str(display_output or "")
    if "timed out" in display_text.lower() or "timeout" in display_text.lower():
        return _extract_timeout_seconds_from_text(display_text)

    return None


def _build_tool_result_callback_metadata(
    execution_result,
    display_output,
    *,
    pending_preflight: dict | None = None,
) -> dict[str, object]:
    timeout_seconds = _extract_tool_timeout_seconds(execution_result, display_output)
    is_error = bool(not getattr(execution_result, "success", False) or timeout_seconds)
    # A preflight that needs user input is not a failure — the subprocess
    # exits non-zero by design so callers can stash state and prompt. UIs
    # gating on ``is_error`` would otherwise hide the confirmation text.
    if pending_preflight and not timeout_seconds:
        is_error = False

    metadata: dict[str, object] = {
        "status": getattr(execution_result, "status", ""),
        "success": bool(getattr(execution_result, "success", False)),
        "is_error": is_error,
    }

    error = getattr(execution_result, "error", None)
    if error is not None:
        metadata["error_type"] = type(error).__name__
    if timeout_seconds is not None:
        metadata["timed_out"] = True
        metadata["elapsed_seconds"] = timeout_seconds
    if pending_preflight:
        metadata["preflight_pending"] = True
        metadata["preflight_payload"] = pending_preflight
    return metadata


def _build_bot_query_engine_callbacks(
    *,
    chat_id: int | str,
    progress_fn,
    progress_update_fn,
    on_tool_call,
    on_tool_result,
    on_stream_content,
    on_stream_reasoning,
    request_tool_approval,
    logger_obj,
    audit_fn,
    deep_learning_methods: set[str],
    usage_accumulator,
    on_context_compacted=None,
    on_pathology_signal=None,
):
    notified_methods: set[str] = set()

    async def before_tool(request: ToolExecutionRequest):
        func_name = request.name
        func_args = request.arguments
        spec = request.spec
        policy_decision = request.policy_decision
        logger_obj.info(f"Tool call: {func_name}({json.dumps(func_args)[:200]})")
        audit_fn(
            "tool_call",
            chat_id=str(chat_id),
            tool=func_name,
            args_preview=json.dumps(func_args, default=str)[:300],
            policy_action=(
                policy_decision.action if policy_decision is not None else TOOL_POLICY_ALLOW
            ),
        )
        await _emit_tool_callback(on_tool_call, func_name, func_args)

        progress_handle = None
        if (
            policy_decision is not None
            and not policy_decision.allows_execution
        ):
            return {"progress_handle": None}

        if spec is not None and spec.progress_policy == PROGRESS_POLICY_ANALYSIS and progress_fn:
            dl_method = (func_args.get("method") or "").lower()
            if dl_method in deep_learning_methods and dl_method not in notified_methods:
                notified_methods.add(dl_method)
                method_display = func_args.get("method", dl_method)
                progress_handle = await progress_fn(
                    f"⏳ **{method_display}** is a deep learning method and may take "
                    f"10-60 minutes depending on data size. Please be patient...\n\n"
                    f"💡 The analysis is running on the server, you can leave this "
                    f"chat open and come back later."
                )
        return {"progress_handle": progress_handle}

    async def after_tool(execution_result, result_record, tool_state):
        request = execution_result.request
        func_name = request.name
        func_args = request.arguments
        progress_handle = (tool_state or {}).get("progress_handle")
        policy_decision = execution_result.policy_decision

        if progress_handle and progress_update_fn:
            method_display = func_args.get("method") or "analysis"
            if execution_result.success:
                await progress_update_fn(
                    progress_handle,
                    f"✅ **{method_display}** analysis complete!"
                )
            else:
                error_name = type(execution_result.error).__name__ if execution_result.error else "Error"
                await progress_update_fn(
                    progress_handle,
                    f"❌ **{method_display}** failed: {error_name}"
                )

        if (
            execution_result.status == EXECUTION_STATUS_POLICY_BLOCKED
            and policy_decision is not None
        ):
            audit_fn(
                "tool_policy_blocked",
                chat_id=str(chat_id),
                tool=func_name,
                action=policy_decision.action,
                reason=policy_decision.reason[:300],
                risk=policy_decision.risk_level,
            )

        if execution_result.error:
            logger_obj.error(
                "Tool %s raised: %s",
                func_name,
                execution_result.error,
                exc_info=(
                    type(execution_result.error),
                    execution_result.error,
                    execution_result.error.__traceback__,
                ),
            )
            audit_fn(
                "tool_error",
                chat_id=str(chat_id),
                tool=func_name,
                error=str(execution_result.error)[:300],
            )

        if request.executor:
            display_output = result_record.content
            pending_payload_for_metadata: dict | None = None
            if func_name == "omicsclaw":
                pending_payload = _extract_pending_preflight_payload(display_output)
                if _preflight_payload_needs_reply(pending_payload):
                    _remember_pending_preflight_request(
                        chat_id,
                        args=func_args,
                        payload=pending_payload,
                    )
                    pending_payload_for_metadata = pending_payload
                else:
                    pending_preflight_requests.pop(chat_id, None)
            if func_name == "consult_knowledge":
                try:
                    from omicsclaw.knowledge.retriever import consume_runtime_notice

                    notice = consume_runtime_notice()
                    if notice:
                        display_output = f"{notice}\n{display_output}"
                except Exception:
                    pass
            await _emit_tool_callback(
                on_tool_result,
                func_name,
                display_output,
                _build_tool_result_callback_metadata(
                    execution_result,
                    display_output,
                    pending_preflight=pending_payload_for_metadata,
                ),
            )

    def on_llm_error(exc: Exception) -> str:
        logger_obj.debug("LLM API error: %s", exc)
        return _format_llm_api_error_message(exc)

    return QueryEngineCallbacks(
        accumulate_usage=usage_accumulator,
        on_stream_content=on_stream_content,
        on_stream_reasoning=on_stream_reasoning,
        before_tool=before_tool,
        after_tool=after_tool,
        request_tool_approval=request_tool_approval,
        on_llm_error=on_llm_error,
        on_context_compacted=on_context_compacted,
        on_pathology_signal=on_pathology_signal,
    )


async def _maybe_resume_pending_preflight_request(
    *,
    chat_id: int | str,
    user_content: str | list,
    session_id: str | None,
) -> str | None:
    state = pending_preflight_requests.get(chat_id)
    if not state or not isinstance(user_content, str):
        return None

    user_text = user_content.strip()
    if not user_text or user_text.startswith("/"):
        return None

    if (
        state.get("payload", {}).get("confirmations")
        and not state.get("pending_fields")
        and not _is_affirmative_preflight_confirmation(user_text)
    ):
        pending_preflight_requests.pop(chat_id, None)
        return None

    resolved, remaining = _parse_preflight_reply(state, user_text)
    state["answers"] = resolved
    if remaining:
        pending_preflight_requests[chat_id] = state
        return _build_pending_preflight_message(state, answered=resolved, remaining_fields=remaining)

    updated_args = _apply_preflight_answers(
        state.get("original_args", {}),
        state.get("pending_fields", []),
        resolved,
    )
    if state.get("payload", {}).get("confirmations"):
        updated_args["confirmed_preflight"] = True
    pending_preflight_requests.pop(chat_id, None)
    result = await execute_omicsclaw(updated_args, session_id=session_id, chat_id=chat_id)

    pending_payload = _extract_pending_preflight_payload(result)
    if _preflight_payload_needs_reply(pending_payload):
        _remember_pending_preflight_request(
            chat_id,
            args=updated_args,
            payload=pending_payload,
        )
    return strip_user_guidance_lines(result) or result


async def llm_tool_loop(
    chat_id: int | str,
    user_content: str | list,
    user_id: str = None,
    platform: str = None,
    plan_context: str = "",
    workspace: str = "",
    pipeline_workspace: str = "",
    scoped_memory_scope: str = "",
    mcp_servers: tuple[str, ...] | None = None,
    output_style: str = "",
    progress_fn=None,
    progress_update_fn=None,
    on_tool_call=None,
    on_tool_result=None,
    on_stream_content=None,
    on_stream_reasoning=None,
    on_context_compacted=None,
    on_pathology_signal=None,
    # Per-request runtime overrides (desktop app frontend)
    model_override: str = "",
    extra_api_params: dict | None = None,
    max_tokens_override: int = 0,
    system_prompt_append: str = "",
    mode: str = "",
    # Bench — investigation thread (ADR 0018) + lifecycle stage lens (ADR 0020).
    thread_id: str = "",
    stage: str = "",
    usage_accumulator=None,
    request_tool_approval=None,
    policy_state=None,
    cancel_event=None,
    transcript_store_override=None,
    stored_user_content=None,
    content_adapter=None,
) -> str:
    """
    Run the LLM tool-use loop:
    1. Append user message to history
    2. Call LLM with system prompt + history + tools
    3. If tool_calls -> execute -> append results -> call again
    4. Return final text

    progress_fn: async callable(msg) -> handle. Sends a progress message, returns a handle.
    progress_update_fn: async callable(handle, msg). Updates a previously sent progress message.
    on_tool_call: async callable(tool.name, arguments: dict). Called before a tool executes.
    on_tool_result: async callable(tool.name, result: Any). Called after a tool completes.
    on_stream_content: async callable(chunk: str). Called as final text streams in.
    """
    # Slash-command dispatch — registry lives in omicsclaw.surfaces.channels.commands.
    # Unknown / commands return None and fall through to the LLM,
    # preserving the original if-elif behaviour.
    if isinstance(user_content, str) and user_content.strip().startswith("/"):
        slash_result = await _dispatch_slash_command(
            SlashCommandContext(
                chat_id=chat_id,
                user_id=user_id,
                platform=platform,
                user_text=user_content,
                workspace=workspace,
                pipeline_workspace=pipeline_workspace,
            )
        )
        if slash_result is not None:
            return slash_result

    active_transcript_store = (
        transcript_store
        if transcript_store_override is None
        else transcript_store_override
    )

    resumed_result = await _maybe_resume_pending_preflight_request(
        chat_id=chat_id,
        user_content=user_content,
        session_id=f"{platform}:{user_id}:{chat_id}" if user_id and platform else None,
    )
    if resumed_result is not None:
        active_transcript_store.append_user_message(
            chat_id,
            user_content if stored_user_content is None else stored_user_content,
        )
        defer = getattr(active_transcript_store, "defer_terminal_message", None)
        if callable(defer):
            defer(chat_id, content=resumed_result)
        else:
            active_transcript_store.append_assistant_message(
                chat_id, content=resumed_result
            )
        return resumed_result

    user_text = _extract_user_text(user_content)
    try:
        analysis_route = _route_user_text_with_input_state(user_text)
    except Exception as exc:
        logger.warning("Analysis Router context failed (non-fatal): %s", exc)
        analysis_route = AnalysisRoute(
            kind=AnalysisRouteKind.CHAT,
            capability_decision=CapabilityDecision(query=user_text),
        )
    analysis_route_context = _format_analysis_route_context(analysis_route)
    candidate_chain_gate = _candidate_chain_gate_for_turn(
        chat_id,
        user_text,
        analysis_route,
    )
    autonomous_understanding_context = await _build_autonomous_understanding_context(
        user_content
    )
    exact_skill_assisted_param_context = await _build_exact_skill_assisted_param_context(
        user_content
    )

    _ensure_system_prompt()
    deps = _build_engine_dependencies(
        usage_accumulator=usage_accumulator,
        transcript_store_override=transcript_store_override,
    )

    return await run_engine_loop(
        deps=deps,
        chat_id=chat_id,
        user_content=user_content,
        stored_user_content=stored_user_content,
        content_adapter=content_adapter,
        user_id=user_id,
        platform=platform,
        plan_context=plan_context,
        workspace=workspace,
        pipeline_workspace=pipeline_workspace,
        scoped_memory_scope=scoped_memory_scope,
        mcp_servers=mcp_servers,
        output_style=output_style,
        progress_fn=progress_fn,
        progress_update_fn=progress_update_fn,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        on_stream_content=on_stream_content,
        on_stream_reasoning=on_stream_reasoning,
        on_context_compacted=on_context_compacted,
        on_pathology_signal=on_pathology_signal,
        model_override=model_override,
        extra_api_params=extra_api_params,
        max_tokens_override=max_tokens_override,
        # ADR 0024 — caller's system addition stays in the (stable) system
        # prefix; the per-turn route / understanding / assisted-param context
        # is Volatile context and rides the user turn instead.
        system_prompt_append=system_prompt_append,
        user_turn_context=_merge_system_prompt_additions(
            analysis_route_context,
            autonomous_understanding_context,
            exact_skill_assisted_param_context,
        ),
        mode=mode,
        thread_id=thread_id,
        stage=stage,
        request_tool_approval=request_tool_approval,
        policy_state=policy_state,
        cancel_event=cancel_event,
        candidate_chain_gate=candidate_chain_gate,
    )


# ---------------------------------------------------------------------------
