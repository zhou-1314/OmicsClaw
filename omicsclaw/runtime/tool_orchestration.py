from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .tool_executor import ToolCallable, invoke_tool
from .tool_spec import ToolSpec


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]
    spec: ToolSpec | None
    executor: ToolCallable | None
    runtime_context: dict[str, Any] | None = None

    @property
    def can_run_concurrently(self) -> bool:
        return bool(
            self.spec
            and self.executor
            and self.spec.read_only
            and self.spec.concurrency_safe
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    request: ToolExecutionRequest
    output: Any
    success: bool
    error: Exception | None = None


async def _execute_single_request(request: ToolExecutionRequest) -> ToolExecutionResult:
    if request.spec is None or request.executor is None:
        return ToolExecutionResult(
            request=request,
            output=f"Unknown tool: {request.name}",
            success=False,
        )

    try:
        output = await invoke_tool(
            request.spec,
            request.executor,
            request.arguments,
            runtime_context=request.runtime_context,
        )
        return ToolExecutionResult(
            request=request,
            output=output,
            success=True,
        )
    except Exception as exc:
        return ToolExecutionResult(
            request=request,
            output=f"Error executing {request.name}: {type(exc).__name__}: {exc}",
            success=False,
            error=exc,
        )


async def _execute_concurrent_batch(
    requests: list[ToolExecutionRequest],
) -> list[ToolExecutionResult]:
    return list(await asyncio.gather(*(_execute_single_request(request) for request in requests)))


async def execute_tool_requests(
    requests: list[ToolExecutionRequest],
) -> list[ToolExecutionResult]:
    """Execute tool requests with write barriers and stable output ordering.

    Consecutive read-only, concurrency-safe tools run concurrently.
    Any other tool acts as a barrier and is executed serially.
    """

    results: list[ToolExecutionResult] = []
    concurrent_batch: list[ToolExecutionRequest] = []

    async def flush_concurrent_batch() -> None:
        nonlocal concurrent_batch
        if not concurrent_batch:
            return
        results.extend(await _execute_concurrent_batch(concurrent_batch))
        concurrent_batch = []

    for request in requests:
        if request.can_run_concurrently:
            concurrent_batch.append(request)
            continue

        await flush_concurrent_batch()
        results.append(await _execute_single_request(request))

    await flush_concurrent_batch()
    return results
