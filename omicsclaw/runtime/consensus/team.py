"""Consensus team runtime — parallel skill-subprocess fan-out.

In-process ``asyncio.gather`` orchestration of N independent skill
subprocesses, ~50 lines of dedicated runtime code. Each member is a
deterministic ``omicsclaw.skill.runner.run_skill`` call, NOT an LLM
sub-agent. Cancellation flows via the ADR 0009 ``threading.Event`` chain
straight into killpg.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from omicsclaw.runtime.consensus.member import ConsensusMember

DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_PARALLEL_CEILING = 4
MIN_SURVIVING_MEMBERS = 2


@dataclass
class MemberRunResult:
    """One member's outcome from a team run."""

    member: ConsensusMember
    status: str  # "ok" | "failed" | "timeout" | "cancelled"
    duration_seconds: float
    output_dir: Path
    error: str | None = None
    skill_result: Any | None = None


@dataclass
class TeamRunResult:
    """Aggregate result of ``run_team``."""

    members: list[MemberRunResult]
    survived: list[MemberRunResult] = field(default_factory=list)
    failed: list[MemberRunResult] = field(default_factory=list)

    @property
    def n_survived(self) -> int:
        return len(self.survived)

    @property
    def n_failed(self) -> int:
        return len(self.failed)

    @property
    def total(self) -> int:
        return len(self.members)


def _compute_max_parallel(n_members: int, override: int | None = None) -> int:
    """``max_parallel = min(N, cpu_count // 2, 4)`` per ADR 0010."""
    if override is not None and override > 0:
        return min(n_members, override)
    cpu_half = max(1, (os.cpu_count() or 1) // 2)
    return min(n_members, cpu_half, MAX_PARALLEL_CEILING)


async def _run_one_member(
    member: ConsensusMember,
    *,
    input_path: str,
    output_root: Path,
    semaphore: asyncio.Semaphore,
    cancel_event: threading.Event | None,
    timeout_seconds: float,
    runner: Any,
) -> MemberRunResult:
    """Run a single member under the concurrency semaphore, with timeout/cancel."""
    started = time.monotonic()
    output_dir = member.member_output_dir(output_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    if cancel_event is not None and cancel_event.is_set():
        return MemberRunResult(
            member=member,
            status="cancelled",
            duration_seconds=0.0,
            output_dir=output_dir,
            error="cancel_event was set before member started",
        )

    async with semaphore:
        if cancel_event is not None and cancel_event.is_set():
            return MemberRunResult(
                member=member,
                status="cancelled",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error="cancel_event was set while waiting for semaphore",
            )
        try:
            skill_result = await asyncio.wait_for(
                asyncio.to_thread(
                    runner,
                    skill_name=member.skill_name,
                    input_path=input_path,
                    output_dir=str(output_dir),
                    extra_args=member.to_extra_args(),
                    cancel_event=cancel_event,
                ),
                timeout=timeout_seconds,
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
            return MemberRunResult(
                member=member,
                status=status,
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error=error,
                skill_result=skill_result,
            )
        except asyncio.TimeoutError:
            # NOTE: do NOT set cancel_event here — a per-member timeout is a
            # *member-local* failure, not a user-cancellation signal. Setting
            # the shared cancel_event would cascade and abort siblings,
            # contradicting ADR 0010 "≥2 survivors continue". The underlying
            # ``skill.runner.run_skill`` already kills the subprocess group on
            # asyncio cancellation of the to_thread coroutine, so leakage of
            # the timed-out subprocess is handled there.
            return MemberRunResult(
                member=member,
                status="timeout",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error=f"exceeded {timeout_seconds:.1f}s",
            )
        except asyncio.CancelledError:
            return MemberRunResult(
                member=member,
                status="cancelled",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error="asyncio cancellation",
            )
        except Exception as exc:  # noqa: BLE001  (we want any subprocess error)
            return MemberRunResult(
                member=member,
                status="failed",
                duration_seconds=time.monotonic() - started,
                output_dir=output_dir,
                error=f"{type(exc).__name__}: {exc}",
            )


def _partition_results(results: Sequence[MemberRunResult]) -> tuple[list[MemberRunResult], list[MemberRunResult]]:
    survived = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status != "ok"]
    return survived, failed


class InsufficientSurvivorsError(RuntimeError):
    """Raised when fewer than ``MIN_SURVIVING_MEMBERS`` members succeed.

    ADR 0010 forbids silent fallback to the narrative path; A-path failure
    is loud by design.
    """


async def run_team(
    members: Sequence[ConsensusMember],
    *,
    input_path: str,
    output_root: Path | str,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_parallel: int | None = None,
    runner: Any = None,
) -> TeamRunResult:
    """Fan out ``members`` in parallel and collect their results.

    Raises ``InsufficientSurvivorsError`` if fewer than 2 members succeed
    (ADR 0010 operational defaults). Cancellation is best-effort: in-flight
    members receive killpg via the underlying ``run_skill`` chain.
    """
    if not members:
        raise ValueError("members must be non-empty")
    if len({m.name for m in members}) != len(members):
        raise ValueError("member names must be unique")

    output_root_p = Path(output_root)
    output_root_p.mkdir(parents=True, exist_ok=True)

    if runner is None:
        # Late import keeps the runtime importable in environments that
        # haven't installed the full skill pipeline (e.g. lightweight tests
        # that exercise the operator math).
        from omicsclaw.skill.runner import run_skill as runner  # type: ignore[no-redef]

    parallel = _compute_max_parallel(len(members), max_parallel)
    semaphore = asyncio.Semaphore(parallel)

    coros = [
        _run_one_member(
            member,
            input_path=input_path,
            output_root=output_root_p,
            semaphore=semaphore,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        for member in members
    ]
    results: list[MemberRunResult] = await asyncio.gather(*coros)
    survived, failed = _partition_results(results)

    if len(survived) < MIN_SURVIVING_MEMBERS:
        survivors_label = (
            f"{len(survived)} surviving member"
            if len(survived) == 1
            else f"{len(survived)} surviving members"
        )
        failed_summary = "; ".join(
            f"{r.member.name}={r.status}({r.error})" for r in failed
        )
        raise InsufficientSurvivorsError(
            f"Only {survivors_label} (< {MIN_SURVIVING_MEMBERS} required). "
            f"Failed: {failed_summary or '(none recorded)'}"
        )

    return TeamRunResult(members=results, survived=survived, failed=failed)
