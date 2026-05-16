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
import uuid
from typing import AsyncIterator

import omicsclaw.runtime.agent.state as _core

from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import (
    ContextCompacted,
    Error,
    Event,
    Final,
    PendingMedia,
    ProgressStart,
    ProgressUpdate,
    StreamContent,
    StreamReasoning,
    ToolCall,
    ToolResult,
)

_SENTINEL: object = object()


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

    async def _on_tool_result(tool: str, result) -> None:
        await queue.put(ToolResult(tool=tool, result=result))

    async def _on_stream_content(chunk: str) -> None:
        await queue.put(StreamContent(chunk=chunk))

    async def _on_stream_reasoning(chunk: str) -> None:
        await queue.put(StreamReasoning(chunk=chunk))

    async def _on_context_compacted(payload) -> None:
        await queue.put(ContextCompacted(payload=dict(payload) if payload else {}))

    async def _run() -> None:
        try:
            from omicsclaw.runtime.agent.loop import llm_tool_loop

            final_text = await llm_tool_loop(
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
                model_override=envelope.model_override,
                extra_api_params=envelope.extra_api_params,
                max_tokens_override=envelope.max_tokens_override,
                system_prompt_append=envelope.system_prompt_append,
                mode=envelope.mode,
                usage_accumulator=envelope.usage_accumulator,
                request_tool_approval=envelope.request_tool_approval,
                policy_state=envelope.policy_state,
            )

            chat_id = envelope.chat_id
            media_items = list(_core.pending_media.pop(chat_id, []) or [])
            if not media_items and chat_id is not None:
                alt_key = str(chat_id)
                if alt_key != chat_id:
                    media_items = list(_core.pending_media.pop(alt_key, []) or [])
            if media_items:
                await queue.put(PendingMedia(items=media_items))

            kind = (
                "preflight"
                if chat_id in _core.pending_preflight_requests
                else "normal"
            )
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
