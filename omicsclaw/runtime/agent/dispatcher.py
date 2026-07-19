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

# One event may cross the producer/consumer handoff ahead of the Surface.  The
# callbacks run in the LLM/tool task, so a larger or unbounded queue would let a
# slow renderer accumulate arbitrarily many rich tool results before Surface
# backpressure can take effect.
DISPATCH_EVENT_QUEUE_MAX_ITEMS = 1


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
    queue: asyncio.Queue = asyncio.Queue(maxsize=DISPATCH_EVENT_QUEUE_MAX_ITEMS)
    producer_done = asyncio.Event()

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
                thread_id=envelope.thread_id,
                stage=envelope.stage,
                usage_accumulator=envelope.usage_accumulator,
                request_tool_approval=envelope.request_tool_approval,
                policy_state=envelope.policy_state,
                cancel_event=envelope.cancel_event,
                stored_user_content=envelope.stored_user_content,
                content_adapter=envelope.content_adapter,
                **(
                    {"transcript_store_override": envelope.transcript_turn}
                    if envelope.transcript_turn is not None
                    else {}
                ),
            )

            # ``pending_media`` is deliberately not touched here — see
            # docstring of ``events.py`` for the rationale. Surfaces keep
            # their existing per-surface drain.
            pending_preflight = getattr(_core, "pending_preflight_requests", None) or {}
            kind = "preflight" if envelope.chat_id in pending_preflight else "normal"
            terminal_ref = None
            if envelope.transcript_turn is not None:
                terminal_ref = envelope.transcript_turn.stage_terminal(
                    final_text or "",
                    terminal_kind=kind,
                )
            await queue.put(
                Final(
                    text=final_text or "",
                    kind=kind,
                    transcript_entry_id=(
                        terminal_ref.entry_id if terminal_ref is not None else ""
                    ),
                    transcript_content_sha256=(
                        terminal_ref.content_sha256 if terminal_ref is not None else ""
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(Error(exception=exc))
        finally:
            # Completion has its own wake-up channel: cancellation of a
            # callback blocked on the one-slot queue must not strand this task
            # attempting to enqueue a sentinel after the consumer has closed.
            producer_done.set()

    task = asyncio.create_task(_run())

    try:
        while True:
            if producer_done.is_set() and queue.empty():
                break
            get_task = asyncio.create_task(queue.get())
            done_task = asyncio.create_task(producer_done.wait())
            try:
                completed, _ = await asyncio.wait(
                    {get_task, done_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                get_task.cancel()
                done_task.cancel()
                await asyncio.gather(get_task, done_task, return_exceptions=True)
                raise
            if get_task in completed:
                item = get_task.result()
                if not done_task.done():
                    done_task.cancel()
                await asyncio.gather(done_task, return_exceptions=True)
                yield item
                continue
            if not get_task.done():
                get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


__all__ = ["DISPATCH_EVENT_QUEUE_MAX_ITEMS", "dispatch"]
