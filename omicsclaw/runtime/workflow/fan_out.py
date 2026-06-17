"""Workflow runtime — parallel skill-subprocess fan-out (ADR 0016 L1).

In-process ``asyncio.gather`` orchestration of N independent skill
subprocesses — the one topology primitive the workflow runtime owns. Each
step is a deterministic ``omicsclaw.skill.runner.run_skill`` call, NOT an LLM
sub-agent. Cancellation flows via the ADR 0009 ``threading.Event`` chain
straight into killpg.

Domain-neutral: a ``WorkflowStep`` is anything carrying ``name`` /
``skill_name`` / ``to_extra_args()``. The runtime never imports a concrete
step type — its caller supplies one — and how a step's outputs are read is the
caller's concern, never the runtime's.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_PARALLEL_CEILING = 4


@runtime_checkable
class WorkflowStep(Protocol):
    """One fan-out target: a deterministic skill subprocess, not an LLM agent.

    The runtime needs only ``name`` (output subdir + label), ``skill_name``
    (what ``run_skill`` runs), and ``to_extra_args()`` (CLI flags). Output
    schemas are not its concern.
    """

    name: str
    skill_name: str

    def to_extra_args(self) -> list[str]: ...


@dataclass
class StepRunResult:
    """One step's outcome from a fan-out run."""

    step: WorkflowStep
    status: str  # "ok" | "failed" | "timeout" | "cancelled"
    duration_seconds: float
    output_dir: Path
    error: str | None = None
    skill_result: Any | None = None


@dataclass
class FanOutResult:
    """Aggregate result of ``fan_out``."""

    steps: list[StepRunResult]
    survived: list[StepRunResult] = field(default_factory=list)
    failed: list[StepRunResult] = field(default_factory=list)

    @property
    def n_survived(self) -> int:
        return len(self.survived)

    @property
    def n_failed(self) -> int:
        return len(self.failed)

    @property
    def total(self) -> int:
        return len(self.steps)


def _compute_max_parallel(n_steps: int, override: int | None = None) -> int:
    """Concurrency cap: ``min(N, cpu_count // 2, MAX_PARALLEL_CEILING)``."""
    if override is not None and override > 0:
        return min(n_steps, override)
    cpu_half = max(1, (os.cpu_count() or 1) // 2)
    return min(n_steps, cpu_half, MAX_PARALLEL_CEILING)


async def _run_one_step(
    step: WorkflowStep,
    *,
    input_path: str,
    output_root: Path,
    semaphore: asyncio.Semaphore,
    cancel_event: threading.Event | None,
    timeout_seconds: float,
    runner: Any,
    loop: asyncio.AbstractEventLoop,
) -> StepRunResult:
    """Run a single step under the concurrency semaphore, with timeout/cancel."""
    started = time.monotonic()
    output_dir = output_root / step.name
    output_dir.mkdir(parents=True, exist_ok=True)

    if cancel_event is not None and cancel_event.is_set():
        return StepRunResult(
            step=step,
            status="cancelled",
            duration_seconds=0.0,
            output_dir=output_dir,
            error="cancel_event was set before step started",
        )

    async with semaphore:
        if cancel_event is not None and cancel_event.is_set():
            return StepRunResult(
                step=step,
                status="cancelled",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error="cancel_event was set while waiting for semaphore",
            )
        try:
            # Run the blocking runner in a short-lived DAEMON thread bridged to
            # asyncio (not a shared/loop-default or pooled executor). There is no
            # threadpool lifecycle to manage and — crucially — a timed-out or
            # cancelled (unkillable) runner thread is a daemon, so it can NEVER
            # block process or test teardown. On the normal path the thread sets
            # the result and exits on its own. The deferred hard-timeout leak
            # (ADR 0029) is thus harmless to interpreter/loop shutdown.
            cf_future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()

            def _invoke_runner() -> None:
                try:
                    cf_future.set_result(
                        runner(
                            skill_name=step.skill_name,
                            input_path=input_path,
                            output_dir=str(output_dir),
                            extra_args=step.to_extra_args(),
                            cancel_event=cancel_event,
                        )
                    )
                except BaseException as exc:  # noqa: BLE001 — propagate to the awaiter
                    if not cf_future.cancelled():
                        cf_future.set_exception(exc)

            threading.Thread(
                target=_invoke_runner, name=f"fanout-{step.name}", daemon=True
            ).start()
            skill_result = await asyncio.wait_for(
                asyncio.wrap_future(cf_future, loop=loop), timeout=timeout_seconds
            )
            status = "ok"
            error = None
            # The runner returns SkillRunResult (or compatible). Treat
            # explicit failure attributes as failures so we don't pretend
            # a non-zero exit succeeded.
            exit_code = getattr(skill_result, "exit_code", None)
            if exit_code is not None and exit_code != 0:
                status = "failed"
                error = f"skill exit_code={exit_code}"
            return StepRunResult(
                step=step,
                status=status,
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error=error,
                skill_result=skill_result,
            )
        except asyncio.TimeoutError:
            # NOTE: do NOT set cancel_event here — a per-step timeout is a
            # *step-local* failure, not a user-cancellation signal. Setting
            # the shared cancel_event would cascade and abort sibling steps,
            # which must each fail or survive independently. The underlying
            # ``skill.runner.run_skill`` already kills the subprocess group on
            # asyncio cancellation of the to_thread coroutine, so leakage of
            # the timed-out subprocess is handled there.
            return StepRunResult(
                step=step,
                status="timeout",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error=f"exceeded {timeout_seconds:.1f}s",
            )
        except asyncio.CancelledError:
            return StepRunResult(
                step=step,
                status="cancelled",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error="asyncio cancellation",
            )
        except Exception as exc:  # noqa: BLE001  (we want any subprocess error)
            return StepRunResult(
                step=step,
                status="failed",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error=f"{type(exc).__name__}: {exc}",
            )


def _partition_results(
    results: Sequence[StepRunResult],
) -> tuple[list[StepRunResult], list[StepRunResult]]:
    survived = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status != "ok"]
    return survived, failed


class InsufficientSurvivorsError(RuntimeError):
    """Raised when fewer steps survive a fan-out than the caller required.

    The runtime never sets this threshold itself: it is raised only when a
    caller opts in via ``fan_out(required_survivors=...)`` and fewer steps
    succeed than requested.
    """


async def fan_out(
    steps: Sequence[WorkflowStep],
    *,
    input_path: str,
    output_root: Path | str,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_parallel: int | None = None,
    required_survivors: int | None = None,
    runner: Any = None,
) -> FanOutResult:
    """Fan out ``steps`` in parallel and collect their results.

    By default every step's outcome is reported and no minimum is enforced —
    a single survivor (or none) still returns a ``FanOutResult``. Pass
    ``required_survivors=N`` to raise ``InsufficientSurvivorsError`` when fewer
    than ``N`` steps succeed; the threshold is always the caller's choice, never
    the runtime's. Cancellation is best-effort: in-flight steps receive killpg
    via the underlying ``run_skill`` chain.
    """
    if not steps:
        raise ValueError("steps must be non-empty")
    if len({s.name for s in steps}) != len(steps):
        raise ValueError("step names must be unique")

    output_root_p = Path(output_root)
    output_root_p.mkdir(parents=True, exist_ok=True)

    if runner is None:
        # Late import keeps the runtime importable in environments that
        # haven't installed the full skill pipeline (e.g. lightweight tests
        # that exercise the operator math).
        from omicsclaw.skill.runner import run_skill as runner  # type: ignore[no-redef]

    # Concurrency is gated by the semaphore; each step runs its blocking runner in
    # its OWN short-lived daemon thread (see _run_one_step). No shared/pooled/
    # loop-default executor is involved, so there is no threadpool lifetime to
    # manage and no worker can outlive the call into the caller's teardown — the
    # earlier asyncio.to_thread / ThreadPoolExecutor variants could leave an idle
    # worker that stalled pytest teardown across a test sequence.
    parallel = _compute_max_parallel(len(steps), max_parallel)
    semaphore = asyncio.Semaphore(parallel)
    loop = asyncio.get_running_loop()
    coros = [
        _run_one_step(
            step,
            input_path=input_path,
            output_root=output_root_p,
            semaphore=semaphore,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            runner=runner,
            loop=loop,
        )
        for step in steps
    ]
    results: list[StepRunResult] = await asyncio.gather(*coros)
    survived, failed = _partition_results(results)

    if required_survivors is not None and len(survived) < required_survivors:
        survivors_label = (
            f"{len(survived)} surviving step"
            if len(survived) == 1
            else f"{len(survived)} surviving steps"
        )
        failed_summary = "; ".join(
            f"{r.step.name}={r.status}({r.error})" for r in failed
        )
        raise InsufficientSurvivorsError(
            f"Only {survivors_label} (< {required_survivors} required). "
            f"Failed: {failed_summary or '(none recorded)'}"
        )

    return FanOutResult(steps=results, survived=survived, failed=failed)
