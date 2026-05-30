"""``dispatch(envelope) -> AsyncIterator[Event]`` — per ADR 0006.

Wraps :func:`omicsclaw.runtime.agent.loop.llm_tool_loop`, translating its
seven positional callbacks plus return value, exception, and
``pending_media`` side-channel into a typed event stream.

Per-request state lives in this function's local scope; there is no class
held across calls. Surfaces iterate the stream and render the subset of
events relevant to their output channel.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import uuid
from collections.abc import Mapping
from typing import Any, AsyncIterator

from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import (
    ContextCompacted,
    Error,
    Event,
    Final,
    PathologyDetected,
    ProgressStart,
    ProgressUpdate,
    StreamContent,
    StreamReasoning,
    ToolCall,
    ToolResult,
)

_SENTINEL: object = object()


def _compaction_payload(event: Any) -> dict[str, Any]:
    """Coerce an ``on_context_compacted`` argument into a neutral dict.

    ``query_engine._emit_compaction_event`` fires this callback with a
    :class:`~omicsclaw.runtime.context.compaction.CompactionEvent` dataclass
    (frozen, ``slots=True`` — *not* iterable, so ``dict(event)`` raises
    ``TypeError: 'CompactionEvent' object is not iterable``). Per ADR 0006 the
    dispatcher is surface-agnostic, so it emits the event's fields as a plain
    dict and lets each Surface format it. A mapping is passed through; anything
    else (or ``None``) degrades to ``{}`` rather than crashing the turn.
    """
    if event is None:
        return {}
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        return dataclasses.asdict(event)
    if isinstance(event, Mapping):
        return dict(event)
    return {}


async def dispatch(envelope: MessageEnvelope) -> AsyncIterator[Event]:
    """Run one turn through ``llm_tool_loop``, yielding events as they arrive.

    The generator terminates after exactly one ``Final`` or ``Error`` event.
    If the consumer breaks out of the iteration early, the underlying loop
    task is cancelled.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _progress_fn(text: str) -> str:
        progress_id = str(uuid.uuid4())
        await queue.put(ProgressStart(progress_id=progress_id, text=text))
        return progress_id

    async def _progress_update_fn(handle, text: str) -> None:
        await queue.put(ProgressUpdate(progress_id=str(handle), text=text))

    async def _on_tool_call(tool: str, arguments) -> None:
        args_dict = dict(arguments) if arguments else {}
        await queue.put(ToolCall(tool=tool, arguments=args_dict))

    async def _on_tool_result(tool: str, result, metadata=None) -> None:
        meta = dict(metadata) if isinstance(metadata, dict) else None
        await queue.put(ToolResult(tool=tool, result=result, metadata=meta))

    async def _on_stream_content(chunk: str) -> None:
        await queue.put(StreamContent(chunk=chunk))

    async def _on_stream_reasoning(chunk: str) -> None:
        await queue.put(StreamReasoning(chunk=chunk))

    async def _on_context_compacted(event) -> None:
        await queue.put(ContextCompacted(payload=_compaction_payload(event)))

    async def _on_pathology_signal(signal) -> None:
        await queue.put(
            PathologyDetected(
                kind=signal.kind,
                tool_name=signal.tool_name,
                iteration=signal.iteration,
                count=signal.count,
                reason=signal.reason,
            )
        )

    async def _run() -> None:
        # Late import so tests that swap ``sys.modules['omicsclaw.runtime.agent.state']``
        # or patch ``state.llm_tool_loop`` are honoured on every call. The
        # real ``state`` module lazy-re-exports ``loop.llm_tool_loop``
        # through its ``__getattr__`` (memoised in module globals), so this
        # adds one ``sys.modules`` dict lookup per dispatch — negligible.
        _core = importlib.import_module("omicsclaw.runtime.agent.state")

        try:
            final_text = await _core.llm_tool_loop(
                chat_id=envelope.chat_id,
                user_content=envelope.content,
                user_id=envelope.user_id,
                platform=envelope.platform,
                plan_context=envelope.plan_context,
                workspace=envelope.workspace,
                pipeline_workspace=envelope.pipeline_workspace,
                scoped_memory_scope=envelope.scoped_memory_scope,
                mcp_servers=envelope.mcp_servers,
                output_style=envelope.output_style,
                progress_fn=_progress_fn,
                progress_update_fn=_progress_update_fn,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
                on_stream_content=_on_stream_content,
                on_stream_reasoning=_on_stream_reasoning,
                on_context_compacted=_on_context_compacted,
                on_pathology_signal=_on_pathology_signal,
                model_override=envelope.model_override,
                extra_api_params=envelope.extra_api_params,
                max_tokens_override=envelope.max_tokens_override,
                system_prompt_append=envelope.system_prompt_append,
                mode=envelope.mode,
                analysis_router_mode=envelope.analysis_router_mode,
                usage_accumulator=envelope.usage_accumulator,
                request_tool_approval=envelope.request_tool_approval,
                policy_state=envelope.policy_state,
                cancel_event=envelope.cancel_event,
            )

            # ``pending_media`` is deliberately not touched here — see
            # docstring of ``events.py`` for the rationale. Surfaces keep
            # their existing per-surface drain.
            pending_preflight = getattr(_core, "pending_preflight_requests", None) or {}
            kind = "preflight" if envelope.chat_id in pending_preflight else "normal"
            await queue.put(Final(text=final_text or "", kind=kind))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(Error(exception=exc))
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(_run())

    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


__all__ = ["dispatch"]
