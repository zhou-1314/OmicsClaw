from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from omicsclaw.common.user_guidance import (
    extract_user_guidance_payloads,
    render_guidance_block,
)
from omicsclaw.providers.patches import apply_deepseek_reasoning_passback

from ..context.budget import estimate_message_size
from ..context.compaction import (
    CompactionEvent,
    ContextCompactionConfig,
    PreparedModelMessages,
    prepare_model_messages,
    wrap_compaction_summary,
)
from ..tools.hooks import (
    EVENT_SESSION_RESUME,
    EVENT_SESSION_START,
    EVENT_TOOL_AFTER,
    EVENT_TOOL_BEFORE,
    EVENT_TOOL_FAILURE,
)
from ..tools.hooks import SessionHookPayload, ToolHookPayload
from ..tools.hooks import HOOK_MODE_CONTEXT, HOOK_MODE_NOTICE
from ..policy.policy import TOOL_POLICY_REQUIRE_APPROVAL, evaluate_tool_policy
from ..tools.hooks import LifecycleHookRuntime
from ..policy.state import ToolPolicyState
from ..tools.execution_hooks import (
    build_default_tool_execution_hooks,
    merge_tool_execution_hooks,
)
from ..tools.orchestration import (
    EXECUTION_STATUS_POLICY_BLOCKED,
    ToolExecutionRequest,
    ToolExecutionResult,
    execute_tool_requests,
)
from ..tools.registry import ToolRuntime
from ..storage.tool_result import ToolResultRecord, ToolResultStore
from ..storage.transcript import TranscriptStore
from ..context.budget import (
    check_token_budget,
    create_token_budget_tracker,
    record_completion_tokens,
)
from .cache_diagnostics import (
    CACHE_DIAGNOSTICS,
    REASON_HISTORY_SHIFTED,
    REASON_SYSTEM_CHANGED,
    REASON_TOOL_LIST_CHANGED,
    CacheTurnDiagnostics,
    compute_segment_hash,
    extract_cache_tokens,
)
from .loop_pathology import detect as detect_loop_pathology
from .loop_pathology import detect_phantom_completion
from .loop_pathology import read_access_target
from .loop_state import (
    LoopState,
    PathologySignal,
    ToolCallRecord,
    ToolErrorRecord,
    compute_args_digest,
)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _prepend_volatile_to_user_content(content: Any, addition: str) -> Any:
    """Prepend per-turn Volatile context (ADR 0024) to the user message content.

    Used for session-hook fragments so they ride the append-only user turn
    instead of the cache-stable system prefix. Handles both plain-string and
    multimodal (list-of-parts) user content; a falsy addition is a no-op.
    """
    if not addition or not str(addition).strip():
        return content
    block = str(addition).strip()
    if isinstance(content, list):
        return [{"type": "text", "text": block}, *content]
    return f"{block}\n\n{content}"


def _prepare_tool_runtime_context(
    runtime_context: dict[str, Any] | None,
) -> dict[str, Any]:
    prepared = dict(runtime_context or {})
    omicsclaw_dir = str(prepared.get("omicsclaw_dir", "") or "").strip()
    if not omicsclaw_dir:
        return prepared
    return merge_tool_execution_hooks(
        prepared,
        build_default_tool_execution_hooks(omicsclaw_dir),
    )


@dataclass(frozen=True, slots=True)
class MaterializedToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class MaterializedMessage:
    content: str | None
    tool_calls: list[MaterializedToolCall] | None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class QueryEngineContext:
    chat_id: int | str
    session_id: str | None
    system_prompt: str
    user_message_content: Any
    surface: str = "bot"
    policy_state: ToolPolicyState | None = None
    hook_runtime: LifecycleHookRuntime | None = None
    tool_runtime_context: dict[str, Any] | None = None
    token_budget: int | str | None = None
    # Phase 1 (tool-list-compression): when set, this tuple replaces the
    # full ``tool_runtime.openai_tools`` payload sent to the LLM, so
    # callers can exercise ``ToolRegistry.to_openai_tools_for_request``
    # to filter tools per request. ``None`` (default) preserves legacy
    # behavior — the full registered tool list is sent.
    request_tools: tuple[dict[str, Any], ...] | None = None


@dataclass(slots=True)
class QueryEngineCallbacks:
    accumulate_usage: Callable[[Any], Any] | None = None
    on_stream_content: Callable[[str], Any] | None = None
    on_stream_reasoning: Callable[[str], Any] | None = None
    before_tool: Callable[[ToolExecutionRequest], Any] | None = None
    after_tool: Callable[[ToolExecutionResult, ToolResultRecord, Any], Any] | None = (
        None
    )
    request_tool_approval: (
        Callable[[ToolExecutionRequest, ToolExecutionResult], Any] | None
    ) = None
    on_llm_error: Callable[[Exception], Any] | None = None
    on_context_compacted: Callable[["CompactionEvent"], Any] | None = None
    on_pathology_signal: Callable[[PathologySignal], Any] | None = None
    # ADR 0024 — per-turn prompt-prefix cache diagnostics (hit ratio +
    # inferred miss reason). Surfaces subscribe to display/log them; ``None``
    # (default) still records into the process-wide ``CACHE_DIAGNOSTICS`` sink.
    on_cache_diagnostics: Callable[["CacheTurnDiagnostics"], Any] | None = None
    # ADR 0009 — declared for callback-construction symmetry. The
    # functional path uses ``context.tool_runtime_context["cancel_event"]``
    # (a dict already merged into each ToolExecutionRequest.runtime_context
    # by ``_build_execution_requests``); this field carries the same Event
    # so direct callers of the engine that don't go through
    # ``tool_runtime_context`` still have a place to attach the signal.
    cancel_event: Any = None


@dataclass(frozen=True, slots=True)
class QueryEngineConfig:
    model: str
    max_iterations: int = 20
    max_tokens: int = 8192
    llm_error_types: tuple[type[BaseException], ...] = (Exception,)
    context_compaction: ContextCompactionConfig = field(
        default_factory=ContextCompactionConfig
    )
    extra_api_params: dict[str, Any] = field(default_factory=dict)
    # DeepSeek thinking-mode endpoints reject requests where any historical
    # assistant message lacks ``reasoning_content``. Set to True for the
    # ``deepseek`` provider so the chat path mirrors the autoagent passback.
    deepseek_reasoning_passback: bool = False
    # ADR 0027 — arm the phantom-completion guard for providers whose models
    # silently truncate context and miss tool calls (Ollama). When True and
    # the model ends a turn with a no-tool-call message that *claims* analysis
    # work, the loop nudges it once to actually call the tool instead of
    # returning the fabricated narration.
    phantom_completion_guard: bool = False


@dataclass(frozen=True, slots=True)
class PlannedToolCallRun:
    execution_results: list[ToolExecutionResult]
    interruption_message: str = ""


def _extract_completion_tokens(response_usage, delta) -> int:
    if isinstance(delta, dict):
        try:
            value = int(delta.get("completion_tokens", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value

    try:
        return int(getattr(response_usage, "completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _record_usage(
    response_usage,
    callbacks: QueryEngineCallbacks,
    *,
    on_usage_delta: Callable[[Any, Any], None] | None = None,
) -> None:
    delta = None
    if callbacks.accumulate_usage:
        delta = callbacks.accumulate_usage(response_usage)
    if on_usage_delta is not None:
        on_usage_delta(response_usage, delta)


async def _emit_cache_diagnostics(
    *,
    callbacks: QueryEngineCallbacks,
    chat_id: Any,
    response_usage: Any,
    tool_hash: str,
    system_hash: str,
) -> None:
    """Record one LLM call's prompt-prefix cache outcome (ADR 0024 Phase 0).

    Always rolls the cross-turn prefix hashes forward (so the next call can
    attribute a miss) and accumulates per-session hit/miss totals into the
    ``CACHE_DIAGNOSTICS`` sink; additionally invokes the optional
    ``on_cache_diagnostics`` surface callback when one is wired.
    """
    tokens = extract_cache_tokens(response_usage)
    diagnostics = CACHE_DIAGNOSTICS.record(
        chat_id,
        tool_hash=tool_hash,
        system_hash=system_hash,
        tokens=tokens,
    )
    # ADR 0024 §5 — surface the prefix-cache outcome in logs (every surface).
    # An unexpected mid-session miss (tool/system/history churn) means the stable
    # prefix is being re-billed at full price, so it warns; healthy hits are DEBUG.
    if diagnostics.has_signal:
        import logging

        logger = logging.getLogger(__name__)
        if diagnostics.miss_reason in (
            REASON_TOOL_LIST_CHANGED,
            REASON_SYSTEM_CHANGED,
            REASON_HISTORY_SHIFTED,
        ):
            logger.warning(
                "prompt cache miss (%s): hit_ratio=%.0f%% — the stable prefix is "
                "being re-billed; investigate prefix stability (ADR 0024)",
                diagnostics.miss_reason,
                diagnostics.hit_ratio * 100,
            )
        else:
            logger.debug(
                "prompt cache: hit_ratio=%.0f%% reason=%s",
                diagnostics.hit_ratio * 100,
                diagnostics.miss_reason,
            )
    if callbacks.on_cache_diagnostics is not None:
        await _maybe_await(callbacks.on_cache_diagnostics(diagnostics))


def _is_prompt_too_long_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 413:
        return True
    http_status = getattr(exc, "http_status", None)
    if http_status == 413:
        return True

    message = str(exc).lower()
    patterns = (
        "prompt too long",
        "context length",
        "maximum context",
        "too many tokens",
        "request too large",
        "request entity too large",
        "413",
    )
    return any(pattern in message for pattern in patterns)


_EMPTY_COMPLETION_MESSAGE = (
    "LLM provider returned an empty completion. Check that the configured "
    "provider endpoint is OpenAI-compatible, the model name is valid, and for "
    "custom endpoints include the API base path such as /v1 when required."
)


def _merge_response_segments(segments: list[str], current: str) -> str:
    merged = [
        segment.strip()
        for segment in [*segments, current]
        if segment and segment.strip()
    ]
    if not merged:
        return _EMPTY_COMPLETION_MESSAGE
    return "\n\n".join(merged)


def _normalize_permission_resolution(
    raw: Any,
    *,
    request: ToolExecutionRequest,
    fallback_surface: str,
) -> tuple[str, dict[str, Any] | None, ToolPolicyState | None, str, bool]:
    if raw is None:
        return ("deny", None, None, "", False)

    if isinstance(raw, str):
        behavior = raw.strip().lower()
        if behavior in {"allow", "deny"}:
            return (behavior, None, None, "", False)
        return ("deny", None, None, raw.strip(), False)

    if not isinstance(raw, Mapping):
        return ("deny", None, None, "", False)

    behavior = str(raw.get("behavior") or raw.get("decision") or "").strip().lower()
    if behavior not in {"allow", "deny"}:
        behavior = "deny"

    updated_arguments_raw = raw.get("updated_arguments")
    if updated_arguments_raw is None:
        updated_arguments_raw = raw.get("updated_input")
    updated_arguments = (
        dict(updated_arguments_raw)
        if isinstance(updated_arguments_raw, Mapping)
        else None
    )

    policy_state_raw = raw.get("policy_state")
    policy_state = (
        ToolPolicyState.from_mapping(
            policy_state_raw,
            surface=str(
                (
                    (request.runtime_context or {}).get("surface")
                    or fallback_surface
                    or ""
                )
            ).strip(),
        )
        if policy_state_raw is not None
        else None
    )

    message = str(raw.get("message", "") or "").strip()
    persist = bool(raw.get("persist", False))
    return (behavior, updated_arguments, policy_state, message, persist)


def _build_ask_user_interruption_message(tool_name: str, text: str | None) -> str:
    """Halt the turn after `ask_user` so the agent waits for the user's choice.

    Deterministic counterpart to the prompt guidance (SOUL rule 9 + tool
    description): when the ask_user tool returns its structured question, end
    the turn immediately instead of relying on the model to stop on its own.
    """
    if tool_name != "ask_user" or not text:
        return ""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(payload, dict) or payload.get("kind") != "ask_user":
        return ""
    question = str(payload.get("question") or "").strip()
    return (
        f"Awaiting the user's reply to: {question}"
        if question
        else "Awaiting the user's reply."
    )


def _build_preflight_interruption_message(text: str | None) -> str:
    payloads = extract_user_guidance_payloads(text)
    if not payloads:
        return ""
    relevant = [
        payload
        for payload in payloads
        if payload.get("kind") == "preflight"
        and payload.get("status") in {"needs_user_input", "blocked"}
    ]
    if not relevant:
        return ""
    return render_guidance_block([], payloads=relevant, title="Important follow-up")


def _extract_text_fragments(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        for key in ("text", "content", "summary", "reasoning"):
            fragment = value.get(key)
            if isinstance(fragment, str) and fragment:
                return [fragment]
        return []
    if isinstance(value, (list, tuple)):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_extract_text_fragments(item))
        return fragments
    for attr in ("text", "content", "summary", "reasoning"):
        fragment = getattr(value, attr, None)
        if isinstance(fragment, str) and fragment:
            return [fragment]
    return []


def _extract_stream_delta_chunks(delta: Any) -> tuple[list[str], list[str]]:
    text_chunks: list[str] = []
    reasoning_chunks: list[str] = []

    content = getattr(delta, "content", None)
    if isinstance(content, str) and content:
        # Ollama's OpenAI-compatible endpoint sends content="" alongside every
        # reasoning delta for thinking models (e.g. Gemma). Skip empty strings so
        # we don't emit an empty `text` event per reasoning token (which the
        # desktop app would read as the thinking phase ending between tokens).
        text_chunks.append(content)
    elif content is not None and not isinstance(content, str):
        parts = content if isinstance(content, (list, tuple)) else [content]
        for part in parts:
            part_type = ""
            if isinstance(part, Mapping):
                part_type = str(part.get("type", "") or "").strip().lower()
            else:
                part_type = str(getattr(part, "type", "") or "").strip().lower()
            fragments = _extract_text_fragments(part)
            if part_type in {"reasoning", "thinking", "summary"}:
                reasoning_chunks.extend(fragments)
            else:
                text_chunks.extend(fragments)

    for attr in ("reasoning", "reasoning_content", "thinking"):
        reasoning_chunks.extend(_extract_text_fragments(getattr(delta, attr, None)))

    return text_chunks, reasoning_chunks


def _materialize_message_from_choice_message(message) -> MaterializedMessage:
    tool_calls = None
    if getattr(message, "tool_calls", None):
        tool_calls = [
            MaterializedToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments,
            )
            for tc in message.tool_calls
        ]
    reasoning_content: str | None = None
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            reasoning_content = value
            break
    return MaterializedMessage(
        content=getattr(message, "content", None),
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


async def _materialize_message_from_stream(
    response,
    callbacks: QueryEngineCallbacks,
    *,
    on_usage_delta: Callable[[Any, Any], None] | None = None,
) -> MaterializedMessage:
    final_content = ""
    final_reasoning = ""
    tool_calls_dict: dict[int, MaterializedToolCall] = {}
    # Some OpenAI-compatible proxies (ccproxy, LiteLLM, some Gemini transports)
    # emit cumulative `chunk.usage` on every chunk, not just the terminal one.
    # Record the last snapshot once, after the stream ends, to avoid inflating
    # accumulated totals for those providers.
    last_usage: Any = None

    async for chunk in response:
        if chunk.usage:
            last_usage = chunk.usage
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta
        text_chunks, reasoning_chunks = _extract_stream_delta_chunks(delta)
        for reasoning_chunk in reasoning_chunks:
            final_reasoning += reasoning_chunk
            if callbacks.on_stream_reasoning:
                await _maybe_await(callbacks.on_stream_reasoning(reasoning_chunk))
        for text_chunk in text_chunks:
            final_content += text_chunk
            if callbacks.on_stream_content:
                await _maybe_await(callbacks.on_stream_content(text_chunk))

        if delta.tool_calls:
            for tc_chunk in delta.tool_calls:
                tc_index = tc_chunk.index
                if tc_index not in tool_calls_dict:
                    tool_calls_dict[tc_index] = MaterializedToolCall(
                        id=tc_chunk.id or "",
                        name=tc_chunk.function.name or "",
                        arguments=tc_chunk.function.arguments or "",
                    )
                else:
                    existing = tool_calls_dict[tc_index]
                    tool_calls_dict[tc_index] = MaterializedToolCall(
                        id=existing.id or tc_chunk.id or "",
                        name=existing.name + (tc_chunk.function.name or ""),
                        arguments=existing.arguments
                        + (tc_chunk.function.arguments or ""),
                    )

    if last_usage is not None:
        _record_usage(last_usage, callbacks, on_usage_delta=on_usage_delta)

    tool_calls = [tool_calls_dict[idx] for idx in sorted(tool_calls_dict)] or None
    return MaterializedMessage(
        content=final_content or None,
        tool_calls=tool_calls,
        reasoning_content=final_reasoning or None,
    )


async def _materialize_message(
    response,
    callbacks: QueryEngineCallbacks,
    *,
    on_usage_delta: Callable[[Any, Any], None] | None = None,
) -> MaterializedMessage:
    if callbacks.on_stream_content is not None:
        return await _materialize_message_from_stream(
            response,
            callbacks,
            on_usage_delta=on_usage_delta,
        )

    if getattr(response, "usage", None):
        _record_usage(
            response.usage,
            callbacks,
            on_usage_delta=on_usage_delta,
        )
    return _materialize_message_from_choice_message(response.choices[0].message)


async def _emit_compaction_event(
    *,
    callbacks: QueryEngineCallbacks,
    pre_chars: int,
    post_chars: int,
    history_len: int,
    kept_len: int,
    applied_stages: tuple[str, ...],
) -> None:
    """Build a CompactionEvent and dispatch via the callback.

    Failures in the user-supplied callback are logged at WARNING and
    swallowed — compaction itself succeeded; failing to notify must
    not abort the turn.
    """
    saved_chars = max(0, pre_chars - post_chars)
    omitted = max(0, history_len - kept_len)
    event = CompactionEvent(
        messages_compressed=omitted,
        tokens_saved_estimate=int(saved_chars / 3.5),
        applied_stages=applied_stages,
    )
    try:
        await _maybe_await(callbacks.on_context_compacted(event))
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "on_context_compacted callback raised; ignoring.",
            exc_info=True,
        )


def _persist_prepared_compaction(
    *,
    transcript_store: TranscriptStore,
    chat_id: int | str,
    prepared_messages: PreparedModelMessages,
) -> None:
    if not prepared_messages.persisted_summary.strip():
        return
    compacted_history = [
        {
            "role": "system",
            "content": wrap_compaction_summary(prepared_messages.persisted_summary),
        },
        *prepared_messages.messages,
    ]
    transcript_store.replace_history(chat_id, compacted_history)


def _format_pathology_correction(signal: PathologySignal) -> str:
    if signal.kind == "pingpong":
        return (
            f"Loop detector: tool '{signal.tool_name}' was called "
            f"{signal.count} times with the same arguments in this loop. "
            "Reconsider your approach or finalize with current information."
        )
    if signal.kind == "repeated_read":
        target = signal.target or signal.tool_name or "that file"
        return (
            f"Loop detector: you have read the same resource ('{target}') "
            f"{signal.count} times in this loop (via file_read / grep_files / "
            "inspect_*). Its contents are already in your context — re-reading "
            "it wastes tokens and adds nothing. Use what you already have, or "
            "read a DIFFERENT artifact, then finalize."
        )
    if signal.kind == "repeated_failure":
        return (
            f"Loop detector: tool '{signal.tool_name}' has failed "
            f"{signal.count} times in this loop. "
            "Reconsider your approach or finalize with current information."
        )
    if signal.kind == "phantom_completion":
        return (
            "Loop detector: you described or claimed analysis work, but you did "
            "not call any tool, so nothing actually ran and no real results "
            "exist. If the task requires running an analysis, call the "
            "appropriate tool now (e.g. the `omicsclaw` tool to run a skill). "
            "Never report results, figures, or QC reports you have not obtained "
            "from a real tool result. If no tool is needed, say so plainly."
        )
    return f"Loop detector: {signal.reason}"


def _is_new_pathology(signal: PathologySignal, state: LoopState) -> bool:
    """Return True when this (kind, tool_name) hasn't been recorded yet.

    Once the LLM has been warned about pingpong on tool A, repeating the
    same warning every iteration is spam — the LLM already saw it.
    ``MAX_TOOL_ITERATIONS`` remains the terminal backstop if the LLM
    ignores the warning. A *different* (kind, tool_name) — e.g. tool A
    recovers, then tool B starts pingponging — does fire a fresh signal.
    """
    if not state.signals:
        return True
    last = state.signals[-1]

    def _key(s: PathologySignal) -> tuple[str, str | None]:
        # ``repeated_read`` identity is the file (different tools hit the same
        # target); the other kinds are identified by tool_name.
        return (s.kind, s.target if s.target else s.tool_name)

    return _key(last) != _key(signal)


async def _build_execution_requests(
    *,
    tool_calls: list,
    context: "QueryEngineContext",
    callbacks: "QueryEngineCallbacks",
    tool_runtime: "ToolRuntime",
    tool_runtime_context: dict | None,
    current_policy_state: "ToolPolicyState | None",
    hook_runtime,
) -> tuple[list["ToolExecutionRequest"], dict[str, Any]]:
    """Parse assistant tool_calls into ToolExecutionRequest list (ADR 0008 L4).

    For each tc: resolves executor + spec, parses JSON args, builds
    runtime_context, evaluates tool policy, emits EVENT_TOOL_BEFORE
    hook, and runs before_tool callback. Returns (requests,
    tool_states_by_call_id) — tool_states is later threaded into
    after_tool via _record_tool_outcome.
    """
    execution_requests: list[ToolExecutionRequest] = []
    tool_states: dict[str, Any] = {}
    for tc in tool_calls:
        executor = tool_runtime.executors.get(tc.name)
        tool_spec = tool_runtime.specs_by_name.get(tc.name)
        try:
            func_args = json.loads(tc.arguments) if executor else {}
        except json.JSONDecodeError:
            func_args = {}

        runtime_context = {
            "session_id": context.session_id,
            "chat_id": context.chat_id,
            "surface": context.surface,
            "policy_state": current_policy_state,
        }
        if callbacks.request_tool_approval is not None:
            runtime_context["request_tool_approval"] = callbacks.request_tool_approval
        if tool_runtime_context:
            runtime_context.update(tool_runtime_context)
        request = ToolExecutionRequest(
            call_id=tc.id,
            name=tc.name,
            arguments=func_args,
            spec=tool_spec,
            executor=executor,
            runtime_context=runtime_context,
            policy_decision=evaluate_tool_policy(
                tc.name,
                tool_spec,
                runtime_context=runtime_context,
            ),
        )
        if hook_runtime is not None:
            hook_runtime.emit(
                EVENT_TOOL_BEFORE,
                ToolHookPayload(
                    tool_name=tc.name,
                    call_id=tc.id,
                    status="pending",
                    success=False,
                    surface=context.surface,
                    session_id=str(context.session_id or ""),
                    chat_id=str(context.chat_id),
                    policy_action=(
                        request.policy_decision.action
                        if request.policy_decision is not None
                        else ""
                    ),
                ),
                context=runtime_context,
            )
        if executor and callbacks.before_tool is not None:
            tool_states[tc.id] = await _maybe_await(callbacks.before_tool(request))
        execution_requests.append(request)
    return execution_requests, tool_states


async def _record_tool_outcome(
    *,
    execution_result: "ToolExecutionResult",
    context: "QueryEngineContext",
    callbacks: "QueryEngineCallbacks",
    tool_result_store: "ToolResultStore",
    transcript_store: "TranscriptStore",
    hook_runtime,
    tool_state: Any,
    state: "LoopState",
) -> str:
    """Persist one tool result + record into LoopState (ADR 0008 L3).

    Fires hook_runtime AFTER/FAILURE events, injects pre-call rule
    preamble, records into tool_result_store, invokes after_tool
    callback, appends to transcript, and stamps ToolCallRecord /
    ToolErrorRecord into ``state``. Returns the per-result candidate
    interruption_message ('' if this result is not preflight-pending).
    """
    request = execution_result.request
    record_output = execution_result.output
    if hook_runtime is not None:
        event_name = EVENT_TOOL_AFTER
        if not execution_result.success:
            event_name = EVENT_TOOL_FAILURE
        hook_runtime.emit(
            event_name,
            ToolHookPayload(
                tool_name=request.name,
                call_id=request.call_id,
                status=execution_result.status,
                success=execution_result.success,
                surface=context.surface,
                session_id=str(context.session_id or ""),
                chat_id=str(context.chat_id),
                policy_action=(
                    execution_result.policy_decision.action
                    if execution_result.policy_decision is not None
                    else ""
                ),
            ),
            context={
                "session_id": context.session_id,
                "chat_id": context.chat_id,
                "surface": context.surface,
                "workspace": str(
                    (request.runtime_context or {}).get("pipeline_workspace")
                    or (request.runtime_context or {}).get("workspace")
                    or ""
                ).strip(),
            },
        )
        notices = hook_runtime.consume_pending_messages(
            mode=HOOK_MODE_NOTICE,
            event_names=(
                EVENT_TOOL_BEFORE,
                EVENT_TOOL_AFTER,
                EVENT_TOOL_FAILURE,
            ),
            call_id=request.call_id,
        )
        if notices:
            record_output = "\n".join([*notices, str(record_output)]).strip()
    # Phase 4 (system-prompt-compression refactor): prepend the
    # matched pre-call rule preamble (engineering / skill-execution
    # discipline) to the tool result so the model sees the rule
    # right before it reasons about the result. This is the
    # runtime wiring of ``PreCallRuleInjector`` —
    # ``build_pre_call_rule_text`` is otherwise dead abstraction.
    from ..tools.execution_hooks import (
        DEFAULT_PRE_CALL_RULE_INJECTORS,
        build_pre_call_rule_text,
    )

    preamble_text = build_pre_call_rule_text(
        tool_name=request.name,
        tool_args=request.arguments or {},
        injectors=DEFAULT_PRE_CALL_RULE_INJECTORS,
    )
    if preamble_text:
        record_output = f"{preamble_text}\n\n{record_output}".strip()
    result_record = tool_result_store.record(
        chat_id=context.chat_id,
        tool_call_id=request.call_id,
        tool_name=request.name,
        output=record_output,
        success=execution_result.success,
        error=execution_result.error,
        spec=request.spec,
        policy_decision=execution_result.policy_decision,
        execution_trace=(
            execution_result.trace.to_dict()
            if execution_result.trace is not None
            else None
        ),
    )

    if callbacks.after_tool is not None:
        await _maybe_await(
            callbacks.after_tool(
                execution_result,
                result_record,
                tool_state,
            )
        )

    transcript_store.append_tool_message(
        context.chat_id,
        tool_call_id=request.call_id,
        content=result_record.content,
    )

    state.tool_calls.append(
        ToolCallRecord(
            name=request.name,
            args_digest=compute_args_digest(request.arguments),
            iteration=state.iteration,
            succeeded=execution_result.success,
            target=read_access_target(request.name, request.arguments),
        )
    )
    if not execution_result.success:
        error_obj = execution_result.error
        state.errors.append(
            ToolErrorRecord(
                tool_name=request.name,
                iteration=state.iteration,
                error_class=type(error_obj).__name__
                if error_obj is not None
                else "ToolFailure",
                message_head=(str(error_obj) if error_obj is not None else "")[:200],
            )
        )

    ask_user_interruption = _build_ask_user_interruption_message(
        request.name, result_record.content
    )
    if ask_user_interruption:
        return ask_user_interruption
    return _build_preflight_interruption_message(result_record.content)


async def _execute_planned_tool_calls(
    *,
    planned_tool_calls: list[MaterializedToolCall],
    context: "QueryEngineContext",
    callbacks: "QueryEngineCallbacks",
    tool_runtime: ToolRuntime,
    tool_result_store: "ToolResultStore",
    transcript_store: "TranscriptStore",
    hook_runtime,
    tool_runtime_context: dict[str, Any] | None,
    current_policy_state: "ToolPolicyState | None",
    state: "LoopState",
) -> tuple[list["ToolExecutionResult"], str, "ToolPolicyState | None"]:
    """Execute deterministic tool calls through the same pipeline as LLM calls."""
    assistant_tool_calls = [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": tc.arguments,
            },
        }
        for tc in planned_tool_calls
    ]
    transcript_store.append_assistant_message(
        context.chat_id,
        content="",
        tool_calls=assistant_tool_calls,
    )

    execution_requests, tool_states = await _build_execution_requests(
        tool_calls=planned_tool_calls,
        context=context,
        callbacks=callbacks,
        tool_runtime=tool_runtime,
        tool_runtime_context=tool_runtime_context,
        current_policy_state=current_policy_state,
        hook_runtime=hook_runtime,
    )
    execution_results = await execute_tool_requests(execution_requests)
    if callbacks.request_tool_approval is not None:
        execution_results, current_policy_state = await _resolve_tool_approval_flow(
            execution_results=execution_results,
            callbacks=callbacks,
            context=context,
            current_policy_state=current_policy_state,
        )

    interruption_message = ""
    for execution_result in execution_results:
        candidate = await _record_tool_outcome(
            execution_result=execution_result,
            context=context,
            callbacks=callbacks,
            tool_result_store=tool_result_store,
            transcript_store=transcript_store,
            hook_runtime=hook_runtime,
            tool_state=tool_states.get(execution_result.request.call_id),
            state=state,
        )
        if not interruption_message:
            interruption_message = candidate
    return execution_results, interruption_message, current_policy_state


async def _resolve_tool_approval_flow(
    *,
    execution_results: list["ToolExecutionResult"],
    callbacks: "QueryEngineCallbacks",
    context: "QueryEngineContext",
    current_policy_state: "ToolPolicyState | None",
) -> tuple[list["ToolExecutionResult"], "ToolPolicyState | None"]:
    """Drive request_tool_approval for any REQUIRE_APPROVAL results (ADR 0008 L2).

    Returns (resolved_results, possibly_updated_policy_state). The
    caller mirrors the returned policy_state back into its own local so
    the next iteration's tool-request build sees the updated state.
    """
    resolved_execution_results: list[ToolExecutionResult] = []
    for execution_result in execution_results:
        request = execution_result.request
        if (
            execution_result.status == EXECUTION_STATUS_POLICY_BLOCKED
            and execution_result.policy_decision is not None
            and execution_result.policy_decision.action
            == TOOL_POLICY_REQUIRE_APPROVAL
        ):
            resolution = await _maybe_await(
                callbacks.request_tool_approval(request, execution_result)
            )
            (
                approval_behavior,
                updated_arguments,
                updated_policy_state,
                deny_message,
                approval_persist,
            ) = _normalize_permission_resolution(
                resolution,
                request=request,
                fallback_surface=context.surface,
            )
            if approval_behavior == "allow":
                base_policy_state = ToolPolicyState.from_mapping(
                    (request.runtime_context or {}).get("policy_state"),
                    surface=context.surface,
                )
                effective_policy_state = updated_policy_state
                if effective_policy_state is None:
                    effective_policy_state = ToolPolicyState(
                        surface=base_policy_state.surface or context.surface,
                        trusted=base_policy_state.trusted,
                        background=base_policy_state.background,
                        auto_approve_ask=base_policy_state.auto_approve_ask,
                        approved_tool_names=(
                            base_policy_state.approved_tool_names
                            | frozenset({request.name})
                        ),
                    )
                approved_runtime_context = dict(request.runtime_context or {})
                approved_runtime_context["policy_state"] = (
                    effective_policy_state
                )
                approved_request = ToolExecutionRequest(
                    call_id=request.call_id,
                    name=request.name,
                    arguments=(
                        updated_arguments
                        if updated_arguments is not None
                        else request.arguments
                    ),
                    spec=request.spec,
                    executor=request.executor,
                    runtime_context=approved_runtime_context,
                    policy_decision=evaluate_tool_policy(
                        request.name,
                        request.spec,
                        runtime_context=approved_runtime_context,
                    ),
                )
                execution_result = (
                    await execute_tool_requests([approved_request])
                )[0]
                if updated_policy_state is not None and approval_persist:
                    current_policy_state = updated_policy_state
            elif deny_message:
                execution_result = ToolExecutionResult(
                    request=request,
                    output=deny_message,
                    success=False,
                    error=execution_result.error,
                    status=execution_result.status,
                    policy_decision=execution_result.policy_decision,
                    trace=execution_result.trace,
                )
        resolved_execution_results.append(execution_result)
    return resolved_execution_results, current_policy_state


async def run_planned_tool_calls(
    *,
    calls: list[tuple[str, dict[str, Any]]],
    context: QueryEngineContext,
    tool_runtime: ToolRuntime,
    transcript_store: TranscriptStore,
    tool_result_store: ToolResultStore,
    callbacks: QueryEngineCallbacks | None = None,
    append_user_message: bool = True,
) -> PlannedToolCallRun:
    """Execute caller-planned tool calls without asking the LLM to choose them.

    This preserves the same transcript, callback, hook, policy, approval, and
    tool-result-store contracts that ``run_query_engine`` uses for model-
    generated tool calls.
    """
    callbacks = callbacks or QueryEngineCallbacks()
    hook_runtime = context.hook_runtime
    tool_runtime_context = _prepare_tool_runtime_context(context.tool_runtime_context)
    history_before = list(transcript_store.get_history(context.chat_id))

    if hook_runtime is not None:
        session_event_name = (
            EVENT_SESSION_RESUME if history_before else EVENT_SESSION_START
        )
        hook_runtime.emit(
            session_event_name,
            SessionHookPayload(
                chat_id=str(context.chat_id),
                session_id=str(context.session_id or ""),
                surface=context.surface,
                resumed=bool(history_before),
                message_count=len(history_before),
            ),
            context={
                "chat_id": str(context.chat_id),
                "session_id": str(context.session_id or ""),
                "surface": context.surface,
            },
        )
        hook_runtime.consume_pending_messages(
            mode=HOOK_MODE_CONTEXT,
            event_names=(session_event_name,),
        )

    transcript_store.touch(context.chat_id)
    for _evicted_chat in transcript_store.evict_lru_conversations():
        # ADR 0024 — release per-chat cache diagnostics when its transcript is
        # evicted, so the CACHE_DIAGNOSTICS sink can't grow unbounded.
        CACHE_DIAGNOSTICS.reset(_evicted_chat)
    if append_user_message:
        transcript_store.append_user_message(context.chat_id, context.user_message_content)
    transcript_store.prepare_history(context.chat_id)

    planned_tool_calls = [
        MaterializedToolCall(
            id=f"planned-{index + 1}-{name}",
            name=name,
            arguments=json.dumps(arguments or {}, ensure_ascii=False),
        )
        for index, (name, arguments) in enumerate(calls)
    ]
    if not planned_tool_calls:
        return PlannedToolCallRun(execution_results=[])

    state = LoopState()
    execution_results, interruption_message, _ = await _execute_planned_tool_calls(
        planned_tool_calls=planned_tool_calls,
        context=context,
        callbacks=callbacks,
        tool_runtime=tool_runtime,
        tool_result_store=tool_result_store,
        transcript_store=transcript_store,
        hook_runtime=hook_runtime,
        tool_runtime_context=tool_runtime_context,
        current_policy_state=context.policy_state,
        state=state,
    )
    return PlannedToolCallRun(
        execution_results=execution_results,
        interruption_message=interruption_message,
    )


async def _call_llm_with_reactive_compact_retry(
    *,
    llm,
    context: QueryEngineContext,
    tool_runtime: ToolRuntime,
    config: QueryEngineConfig,
    callbacks: QueryEngineCallbacks,
    system_prompt: str,
    history: list,
    request_system_prompt: str,
    request_messages: list,
    transcript_store: TranscriptStore,
    tool_result_store: ToolResultStore,
    compaction_metadata: dict | None,
    compaction_workspace: str | None,
    on_usage_delta,
    has_attempted_reactive_compact: bool,
) -> tuple["MaterializedMessage", bool, str]:
    """One LLM turn with on-demand reactive compaction (ADR 0008 L1).

    Returns the materialised assistant message, the (possibly flipped)
    ``has_attempted_reactive_compact`` flag, and the ``request_system_prompt``
    **actually sent** — which differs from the caller's value when reactive
    compaction rebuilt it, so the caller's cache diagnostics (ADR 0024) hash the
    real sent bytes and attribute the collapse re-warm as ``system-changed``.
    Raises the underlying LLM error if reactive compaction cannot recover; the
    caller is responsible for routing the exception through ``on_llm_error``.
    """
    while True:
        kwargs = {}
        if callbacks.on_stream_content is not None:
            kwargs = {"stream": True, "stream_options": {"include_usage": True}}
        if config.extra_api_params:
            kwargs.update(config.extra_api_params)

        try:
            # Phase 1 (tool-list-compression): use per-request tool
            # list when caller provided one (via
            # ``QueryEngineContext.request_tools``). Falls back to
            # the full registry payload for backward compatibility.
            request_tools = (
                list(context.request_tools)
                if context.request_tools is not None
                else list(tool_runtime.openai_tools)
            )
            response = await llm.chat.completions.create(
                model=config.model,
                max_tokens=config.max_tokens,
                messages=[{"role": "system", "content": request_system_prompt}]
                + request_messages,
                tools=request_tools,
                **kwargs,
            )
            last_message = await _materialize_message(
                response,
                callbacks,
                on_usage_delta=on_usage_delta,
            )
            return last_message, has_attempted_reactive_compact, request_system_prompt
        except config.llm_error_types as exc:
            if not (
                not has_attempted_reactive_compact
                and config.context_compaction.enabled
                and _is_prompt_too_long_error(exc)
            ):
                raise

            reactive_pre_chars = sum(estimate_message_size(m) for m in history)
            reactive_messages = prepare_model_messages(
                system_prompt=system_prompt,
                history=history,
                chat_id=context.chat_id,
                tool_result_store=tool_result_store,
                config=config.context_compaction,
                metadata=compaction_metadata,
                workspace=compaction_workspace,
                force_reactive_compact=True,
            )
            has_attempted_reactive_compact = True
            if reactive_messages.applied_stages and (
                reactive_messages.system_prompt != request_system_prompt
                or reactive_messages.messages != request_messages
            ):
                request_system_prompt = reactive_messages.system_prompt
                request_messages = reactive_messages.messages
                if config.deepseek_reasoning_passback:
                    request_messages = apply_deepseek_reasoning_passback(
                        request_messages
                    )
                _persist_prepared_compaction(
                    transcript_store=transcript_store,
                    chat_id=context.chat_id,
                    prepared_messages=reactive_messages,
                )
                if callbacks.on_context_compacted:
                    await _emit_compaction_event(
                        callbacks=callbacks,
                        pre_chars=reactive_pre_chars,
                        post_chars=reactive_messages.estimated_chars,
                        history_len=len(history),
                        kept_len=len(reactive_messages.messages),
                        applied_stages=reactive_messages.applied_stages,
                    )
                continue
            raise


async def run_query_engine(
    *,
    llm,
    context: QueryEngineContext,
    tool_runtime: ToolRuntime,
    transcript_store: TranscriptStore,
    tool_result_store: ToolResultStore,
    config: QueryEngineConfig,
    callbacks: QueryEngineCallbacks | None = None,
) -> str:
    callbacks = callbacks or QueryEngineCallbacks()
    hook_runtime = context.hook_runtime
    tool_runtime_context = _prepare_tool_runtime_context(context.tool_runtime_context)
    history_before = list(transcript_store.get_history(context.chat_id))
    system_prompt = context.system_prompt
    # ADR 0024 — session-start/resume hook fragments are per-turn Volatile
    # context (a resume hook fires every turn and may inject changing content),
    # so they ride the user turn instead of the system prefix, keeping the
    # prefix byte-stable.
    session_hook_context = ""

    if hook_runtime is not None:
        session_event_name = (
            EVENT_SESSION_RESUME if history_before else EVENT_SESSION_START
        )
        hook_runtime.emit(
            session_event_name,
            SessionHookPayload(
                chat_id=str(context.chat_id),
                session_id=str(context.session_id or ""),
                surface=context.surface,
                resumed=bool(history_before),
                message_count=len(history_before),
            ),
            context={
                "chat_id": str(context.chat_id),
                "session_id": str(context.session_id or ""),
                "surface": context.surface,
            },
        )
        context_fragments = hook_runtime.consume_pending_messages(
            mode=HOOK_MODE_CONTEXT,
            event_names=(session_event_name,),
        )
        if context_fragments:
            session_hook_context = (
                "## Active Session Hooks\n\n"
                + "\n\n".join(
                    fragment for fragment in context_fragments if fragment.strip()
                )
            ).strip()

    transcript_store.touch(context.chat_id)
    for _evicted_chat in transcript_store.evict_lru_conversations():
        # ADR 0024 — release per-chat cache diagnostics when its transcript is
        # evicted, so the CACHE_DIAGNOSTICS sink can't grow unbounded.
        CACHE_DIAGNOSTICS.reset(_evicted_chat)
    transcript_store.append_user_message(
        context.chat_id,
        _prepend_volatile_to_user_content(
            context.user_message_content, session_hook_context
        ),
    )
    transcript_store.prepare_history(context.chat_id)

    budget_tracker = create_token_budget_tracker(context.token_budget)
    accumulated_response_segments: list[str] = []
    has_attempted_reactive_compact = False
    current_policy_state = context.policy_state
    pipeline_workspace = str(
        (tool_runtime_context or {}).get("pipeline_workspace", "") or ""
    ).strip()
    workspace = str((tool_runtime_context or {}).get("workspace", "") or "").strip()
    compaction_metadata = (
        {"pipeline_workspace": pipeline_workspace} if pipeline_workspace else None
    )
    compaction_workspace = pipeline_workspace or workspace or None

    # ADR 0024 — the tool segment is frozen for the whole call (the per-turn
    # frozen tool list), so hash it once; ``_observe_usage_delta`` captures each
    # call's usage so cache diagnostics can be emitted after the call returns.
    _diag_tool_payload = (
        list(context.request_tools)
        if context.request_tools is not None
        else list(tool_runtime.openai_tools)
    )
    diag_tool_hash = compute_segment_hash(_diag_tool_payload)
    _last_response_usage: dict[str, Any] = {"usage": None}

    def _observe_usage_delta(response_usage, delta) -> None:
        _last_response_usage["usage"] = response_usage
        completion_tokens = _extract_completion_tokens(response_usage, delta)
        if completion_tokens > 0:
            record_completion_tokens(budget_tracker, completion_tokens)

    last_message: MaterializedMessage | None = None
    state = LoopState()
    for iteration_index in range(config.max_iterations):
        state.iteration = iteration_index
        history = transcript_store.prepare_history(context.chat_id)
        pre_chars = sum(estimate_message_size(m) for m in history)
        prepared_messages = prepare_model_messages(
            system_prompt=system_prompt,
            history=history,
            chat_id=context.chat_id,
            tool_result_store=tool_result_store,
            config=config.context_compaction,
            metadata=compaction_metadata,
            workspace=compaction_workspace,
        )
        request_system_prompt = prepared_messages.system_prompt
        request_messages = prepared_messages.messages
        if config.deepseek_reasoning_passback:
            request_messages = apply_deepseek_reasoning_passback(request_messages)
        _persist_prepared_compaction(
            transcript_store=transcript_store,
            chat_id=context.chat_id,
            prepared_messages=prepared_messages,
        )
        if prepared_messages.applied_stages and callbacks.on_context_compacted:
            await _emit_compaction_event(
                callbacks=callbacks,
                pre_chars=pre_chars,
                post_chars=prepared_messages.estimated_chars,
                history_len=len(history),
                kept_len=len(prepared_messages.messages),
                applied_stages=prepared_messages.applied_stages,
            )
        try:
            last_message, has_attempted_reactive_compact, sent_system_prompt = (
                await _call_llm_with_reactive_compact_retry(
                    llm=llm,
                    context=context,
                    tool_runtime=tool_runtime,
                    config=config,
                    callbacks=callbacks,
                    system_prompt=system_prompt,
                    history=history,
                    request_system_prompt=request_system_prompt,
                    request_messages=request_messages,
                    transcript_store=transcript_store,
                    tool_result_store=tool_result_store,
                    compaction_metadata=compaction_metadata,
                    compaction_workspace=compaction_workspace,
                    on_usage_delta=_observe_usage_delta,
                    has_attempted_reactive_compact=has_attempted_reactive_compact,
                )
            )
        except config.llm_error_types as exc:
            if callbacks.on_llm_error is not None:
                return await _maybe_await(callbacks.on_llm_error(exc))
            raise

        # ADR 0024 — attribute this call's prefix-cache outcome. ``sent_system_prompt``
        # is the exact system bytes the helper actually sent (reactive compaction may
        # have rebuilt them mid-call), so a collapse re-warm is correctly attributed as
        # ``system-changed`` rather than mis-reported one turn late. The tool hash is
        # constant for the call; a per-turn tool churn would surface as ``tool-list-changed``.
        await _emit_cache_diagnostics(
            callbacks=callbacks,
            chat_id=context.chat_id,
            response_usage=_last_response_usage["usage"],
            tool_hash=diag_tool_hash,
            system_hash=compute_segment_hash(sent_system_prompt),
        )
        _last_response_usage["usage"] = None

        assistant_tool_calls = None
        if last_message.tool_calls:
            assistant_tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments,
                    },
                }
                for tc in last_message.tool_calls
            ]
        transcript_store.append_assistant_message(
            context.chat_id,
            content=last_message.content or "",
            tool_calls=assistant_tool_calls,
            reasoning_content=last_message.reasoning_content,
        )

        if not last_message.tool_calls:
            current_response = last_message.content or ""
            # ADR 0027 — phantom completion: the model ended the turn with a
            # message that *claims* analysis work but called no tool, and no
            # execution tool has run this loop. Nudge it once to actually call
            # the tool instead of returning the fabricated narration. The
            # `_is_new_pathology` dedup bounds this to a single nudge; if the
            # model ignores it, control falls through to the normal return.
            phantom_signal = detect_phantom_completion(
                content=current_response,
                state=state,
                enabled=config.phantom_completion_guard,
            )
            if phantom_signal is not None and _is_new_pathology(phantom_signal, state):
                state.signals.append(phantom_signal)
                if callbacks.on_pathology_signal is not None:
                    await _maybe_await(callbacks.on_pathology_signal(phantom_signal))
                transcript_store.append_user_message(
                    context.chat_id,
                    _format_pathology_correction(phantom_signal),
                )
                # Deliberately do NOT accumulate the fabricated narration into
                # the final answer — it described results that do not exist.
                continue
            budget_decision = check_token_budget(budget_tracker)
            if budget_decision.action == "continue":
                if current_response.strip():
                    accumulated_response_segments.append(current_response)
                transcript_store.append_user_message(
                    context.chat_id,
                    budget_decision.nudge_message,
                )
                continue
            return _merge_response_segments(
                accumulated_response_segments, current_response
            )

        _, interruption_message, current_policy_state = (
            await _execute_planned_tool_calls(
                planned_tool_calls=last_message.tool_calls,
                context=context,
                callbacks=callbacks,
                tool_runtime=tool_runtime,
                tool_result_store=tool_result_store,
                transcript_store=transcript_store,
                hook_runtime=hook_runtime,
                tool_runtime_context=tool_runtime_context,
                current_policy_state=current_policy_state,
                state=state,
            )
        )

        if interruption_message:
            transcript_store.append_assistant_message(
                context.chat_id,
                content=interruption_message,
            )
            return interruption_message

        pathology_signal = detect_loop_pathology(state)
        if pathology_signal is not None and _is_new_pathology(pathology_signal, state):
            state.signals.append(pathology_signal)
            if callbacks.on_pathology_signal is not None:
                await _maybe_await(callbacks.on_pathology_signal(pathology_signal))
            transcript_store.append_user_message(
                context.chat_id,
                _format_pathology_correction(pathology_signal),
            )

    if last_message and last_message.content:
        return _merge_response_segments(
            accumulated_response_segments, last_message.content
        )
    return "(max tool iterations reached)"
