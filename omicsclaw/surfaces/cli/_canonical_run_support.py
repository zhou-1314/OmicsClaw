"""Canonical non-chat Run composition shared by local CLI Adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Callable, Mapping
import uuid

from omicsclaw.common.run_paths import peek_current_project
from omicsclaw.control import (
    ControlRuntime,
    RunAcceptanceStatus,
    RunRuntime,
    RunScope,
)
from omicsclaw.control.run_runtime import (
    RunAdmissionError,
    RunTerminalProjectionIntegrityError,
    RunTerminalResultUnavailable,
    RunTerminalWaitBackpressure,
    SimpleSkillRunTerminalResult,
)
from omicsclaw.skill.resource_scheduler import (
    ExecutionResourceBudget,
    detect_execution_resource_budget,
)

from ._skill_run_support import SkillRunCommandArgs


@dataclass(frozen=True, slots=True)
class CliRunRuntimeConfig:
    """Process-frozen scheduler and Run Store composition facts."""

    output_root: Path
    resource_budget: ExecutionResourceBudget
    max_buffered_runs: int
    max_active_runs: int


class CliRuntimeCloseUnconfirmed(RuntimeError):
    """Run ownership could not be proven stopped; Control must remain held."""


_QUARANTINED_CLI_RUNTIME_OWNERS: dict[
    int,
    tuple[ControlRuntime, RunRuntime | None],
] = {}


def _quarantine_cli_runtime_owner(
    control_runtime: ControlRuntime,
    run_runtime: RunRuntime | None,
) -> None:
    """Keep uncertain owners alive until a later proof or process exit."""

    _QUARANTINED_CLI_RUNTIME_OWNERS[id(control_runtime)] = (
        control_runtime,
        run_runtime,
    )


def _release_cli_runtime_owner(control_runtime: ControlRuntime) -> None:
    _QUARANTINED_CLI_RUNTIME_OWNERS.pop(id(control_runtime), None)


def _require_confirmed_run_close(result: object) -> None:
    if getattr(result, "unconfirmed_run_ids", ()):
        raise CliRuntimeCloseUnconfirmed("Run owner stop is unconfirmed")


async def _close_run_after_failed_start(run_runtime: RunRuntime) -> None:
    """Consume one bounded retry without releasing the Control owner early."""

    last_error: BaseException | None = None
    for _attempt in range(2):
        try:
            reconciled = await run_runtime.close()
            _require_confirmed_run_close(reconciled)
        except (asyncio.CancelledError, KeyboardInterrupt, Exception) as exc:
            last_error = exc
            continue
        return
    raise CliRuntimeCloseUnconfirmed("Run owner stop is unconfirmed") from last_error


@dataclass(slots=True)
class CliRuntimeBundle:
    """Own Control before Run; release Run before Control."""

    workspace_id: str
    control_runtime: ControlRuntime
    run_runtime: RunRuntime
    run_config: CliRunRuntimeConfig
    _run_closed: bool = field(default=False, init=False, repr=False)
    _control_closed: bool = field(default=False, init=False, repr=False)
    _pending_interrupt: str | None = field(default=None, init=False, repr=False)
    _close_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    async def close(self) -> None:
        try:
            async with self._close_lock:
                if not self._run_closed:
                    try:
                        reconciled = await self.run_runtime.close()
                    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
                        # RunRuntime shields its close task to completion before
                        # propagating caller cancellation. Re-read its idempotent
                        # result so Control is released only after stop proof.
                        self._remember_interrupt(exc)
                        reconciled = await self.run_runtime.close()
                    _require_confirmed_run_close(reconciled)
                    self._run_closed = True

                if not self._control_closed:
                    try:
                        await self.control_runtime.close()
                    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
                        self._remember_interrupt(exc)
                        await self.control_runtime.close()
                    self._control_closed = True
        except BaseException:
            _quarantine_cli_runtime_owner(
                self.control_runtime,
                self.run_runtime,
            )
            raise
        _release_cli_runtime_owner(self.control_runtime)

        pending_interrupt = self._pending_interrupt
        self._pending_interrupt = None
        if pending_interrupt == "keyboard":
            raise KeyboardInterrupt
        if pending_interrupt == "cancelled":
            raise asyncio.CancelledError

    def _remember_interrupt(self, exc: BaseException) -> None:
        if self._pending_interrupt is None:
            self._pending_interrupt = (
                "keyboard" if isinstance(exc, KeyboardInterrupt) else "cancelled"
            )


def resolve_cli_run_runtime_config(
    workspace_dir: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> CliRunRuntimeConfig:
    """Freeze output root and host budget once for the entire CLI process."""

    source = os.environ if environ is None else environ
    workspace = Path(workspace_dir).expanduser().resolve()
    configured_root = str(
        source.get("OMICSCLAW_OUTPUT_ROOT", "")
        or source.get("OMICSCLAW_OUTPUT_DIR", "")
    ).strip()
    output_root = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else workspace / "output"
    )
    return CliRunRuntimeConfig(
        output_root=output_root,
        resource_budget=detect_execution_resource_budget(
            output_root,
            environ=source,
        ),
        max_buffered_runs=_positive_integer(
            source,
            "OMICSCLAW_RUN_BUFFER_CAPACITY",
            32,
        ),
        max_active_runs=_positive_integer(
            source,
            "OMICSCLAW_RUN_MAX_ACTIVE",
            2,
        ),
    )


async def open_cli_runtime_bundle(
    workspace_dir: str | Path,
    *,
    run_config: CliRunRuntimeConfig | None = None,
) -> CliRuntimeBundle:
    """Start Control then Run, with reverse cleanup on every failure."""

    workspace_id = str(Path(workspace_dir).expanduser().resolve())
    config = run_config or resolve_cli_run_runtime_config(workspace_id)
    control_runtime = ControlRuntime.for_local_surface(
        workspace_id=workspace_id,
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    run_runtime: RunRuntime | None = None
    try:
        await control_runtime.start()
        run_runtime = RunRuntime.for_local_surface(
            repository=control_runtime.repository,
            output_root=config.output_root,
            resource_budget=config.resource_budget,
            max_buffered_runs=config.max_buffered_runs,
            max_active_runs=config.max_active_runs,
        )
        await run_runtime.start()
    except BaseException as start_error:
        if run_runtime is not None:
            try:
                await _close_run_after_failed_start(run_runtime)
            except BaseException as cleanup_error:
                # The Runtime may still own a scientific process.  Retain the
                # Control lifetime owner and surface the cleanup failure; the
                # one-shot root process must fail rather than open split brain.
                _quarantine_cli_runtime_owner(control_runtime, run_runtime)
                raise cleanup_error from start_error
        try:
            await control_runtime.close()
        except BaseException as cleanup_error:
            _quarantine_cli_runtime_owner(control_runtime, run_runtime)
            raise CliRuntimeCloseUnconfirmed(
                "Control owner close is unconfirmed"
            ) from cleanup_error
        _release_cli_runtime_owner(control_runtime)
        raise start_error
    return CliRuntimeBundle(
        workspace_id=workspace_id,
        control_runtime=control_runtime,
        run_runtime=run_runtime,
        run_config=config,
    )


async def reopen_cli_runtime_bundle(
    current: CliRuntimeBundle,
    workspace_dir: str | Path,
) -> CliRuntimeBundle:
    """Change Control workspace while preserving one process scheduler domain."""

    frozen = current.run_config
    await current.close()
    return await open_cli_runtime_bundle(workspace_dir, run_config=frozen)


async def execute_canonical_demo_run(
    command: SkillRunCommandArgs,
    *,
    run_runtime: RunRuntime,
    submission_id_factory: Callable[[], str] | None = None,
    scope: RunScope | None = None,
    confirm_task_cancellation: bool = False,
) -> dict[str, Any]:
    """Execute one exact demo request without any legacy-runner fallback."""

    if (
        not command.demo
        or command.input_path is not None
        or command.output_dir is not None
        or command.method is not None
    ):
        return build_canonical_demo_failure_result(
            command.skill, "canonical_demo_options_not_supported"
        )
    create_submission_id = submission_id_factory or (lambda: uuid.uuid4().hex)
    try:
        submission_id = create_submission_id()
        submission = await run_runtime.build_simple_skill_demo_submission(
            run_submission_id=submission_id,
            skill_id=command.skill,
            scope=scope,
        )
        submitted = await run_runtime.submit(submission)
    except RunAdmissionError as exc:
        return build_canonical_demo_failure_result(command.skill, exc.code)
    except Exception:
        return build_canonical_demo_failure_result(
            command.skill, "canonical_run_unavailable"
        )

    if submitted.acceptance_status not in {
        RunAcceptanceStatus.ACCEPTED,
        RunAcceptanceStatus.DUPLICATE,
    }:
        return build_canonical_demo_failure_result(
            command.skill,
            submitted.code or submitted.acceptance_status.value,
        )
    if submitted.receipt is None:
        return build_canonical_demo_failure_result(
            command.skill, "canonical_run_receipt_missing"
        )
    run_id = submitted.receipt.run_id
    try:
        outcome = await run_runtime.wait_for_terminal_result(run_id)
    except KeyboardInterrupt:
        try:
            await run_runtime.cancel(run_id)
            outcome = await run_runtime.wait_for_terminal_result(run_id)
        except Exception:
            return build_canonical_demo_failure_result(
                command.skill, "run_cancel_unconfirmed", run_id=run_id
            )
    except asyncio.CancelledError as cancelled:
        cancel_task = asyncio.create_task(run_runtime.cancel(run_id))
        try:
            await asyncio.shield(cancel_task)
            if confirm_task_cancellation:
                terminal_task = asyncio.create_task(
                    run_runtime.wait_for_terminal_result(run_id)
                )
                try:
                    await asyncio.shield(terminal_task)
                except Exception:
                    pass
        except Exception:
            pass
        raise cancelled
    except RunTerminalProjectionIntegrityError as exc:
        return build_canonical_demo_failure_result(
            command.skill, exc.code, run_id=run_id
        )
    except RunTerminalWaitBackpressure:
        return build_canonical_demo_failure_result(
            command.skill, "wait_backpressure", run_id=run_id
        )
    except RunTerminalResultUnavailable:
        return build_canonical_demo_failure_result(
            command.skill, "runtime_closed", run_id=run_id
        )
    except Exception:
        return build_canonical_demo_failure_result(
            command.skill, "terminal_result_unavailable", run_id=run_id
        )
    return _terminal_result(outcome)


def _terminal_result(outcome: SimpleSkillRunTerminalResult) -> dict[str, Any]:
    receipt = outcome.receipt
    duration_seconds = 0.0
    if receipt.finished_at_ms is not None:
        duration_seconds = max(
            0.0,
            (receipt.finished_at_ms - receipt.created_at_ms) / 1000.0,
        )
    output = outcome.output
    return {
        "skill": outcome.skill_id,
        "success": outcome.success,
        "exit_code": 0 if outcome.success else 1,
        "output_dir": output.output_dir if output is not None else "",
        "files": [],
        "stdout": "",
        "stderr": "" if outcome.success else str(receipt.terminal_code or receipt.status),
        "duration_seconds": duration_seconds,
        "method": None,
        "readme_path": (
            str(output.readme_path or "") if output is not None else ""
        ),
        "notebook_path": (
            str(output.notebook_path or "") if output is not None else ""
        ),
        "run_id": receipt.run_id,
    }


def build_canonical_demo_failure_result(
    skill: str,
    code: str,
    *,
    run_id: str = "",
    exit_code: int = 1,
) -> dict[str, Any]:
    return {
        "skill": skill,
        "success": False,
        "exit_code": exit_code,
        "output_dir": "",
        "files": [],
        "stdout": "",
        "stderr": code,
        "duration_seconds": 0.0,
        "method": None,
        "readme_path": "",
        "notebook_path": "",
        "run_id": run_id,
    }


def resolve_root_run_scope(bundle: CliRuntimeBundle) -> RunScope:
    """Resolve legacy navigation only as a hint, then prove it in Control.

    The filesystem pointer cannot establish Project identity.  A missing,
    malformed, stale, or archived pointer therefore means Unassigned; only an
    opaque ID whose authoritative Control Project is still active is accepted.
    Unexpected Control read failures remain failures rather than silently
    changing the requested Scope.
    """

    project_id, _display_name = peek_current_project(bundle.run_config.output_root)
    return bundle.run_runtime.resolve_cli_navigation_scope(project_id or None)


async def execute_root_canonical_demo_run(
    skill: str,
    *,
    workspace_dir: str | Path,
    scope: RunScope | None = None,
) -> dict[str, Any]:
    """Own one root-CLI Runtime bundle around one exact canonical demo."""

    bundle: CliRuntimeBundle | None = None
    result: dict[str, Any] | None = None
    interrupted: BaseException | None = None
    try:
        bundle = await open_cli_runtime_bundle(workspace_dir)
        frozen_scope = scope if scope is not None else resolve_root_run_scope(bundle)
        result = await execute_canonical_demo_run(
            SkillRunCommandArgs(skill=skill, demo=True),
            run_runtime=bundle.run_runtime,
            scope=frozen_scope,
            confirm_task_cancellation=True,
        )
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        interrupted = exc
    except CliRuntimeCloseUnconfirmed:
        result = build_canonical_demo_failure_result(
            skill,
            "canonical_runtime_close_unconfirmed",
        )
    except Exception:
        result = build_canonical_demo_failure_result(
            skill,
            "canonical_run_unavailable",
        )

    close_unconfirmed = False
    if bundle is not None:
        try:
            await bundle.close()
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            if interrupted is None:
                interrupted = exc
        except Exception:
            # One bounded retry consumes RunRuntime's explicit retry contract;
            # persistent failure outranks an otherwise ordinary Ctrl-C result.
            try:
                await bundle.close()
            except (asyncio.CancelledError, KeyboardInterrupt) as exc:
                if interrupted is None:
                    interrupted = exc
            except Exception:
                close_unconfirmed = True
    if close_unconfirmed:
        return build_canonical_demo_failure_result(
            skill,
            "canonical_runtime_close_unconfirmed",
        )
    if interrupted is not None:
        raise interrupted
    return result or build_canonical_demo_failure_result(
        skill,
        "canonical_run_unavailable",
    )


def run_root_canonical_demo(
    skill: str,
    *,
    workspace_dir: str | Path,
    scope: RunScope | None = None,
) -> dict[str, Any]:
    """Synchronous root-CLI boundary with content-free interrupt projection."""

    execution = execute_root_canonical_demo_run(
        skill,
        workspace_dir=workspace_dir,
        scope=scope,
    )
    try:
        return asyncio.run(execution)
    except (asyncio.CancelledError, KeyboardInterrupt):
        execution.close()
        return build_canonical_demo_failure_result(
            skill,
            "canonical_run_interrupted",
            exit_code=130,
        )
    except Exception:
        execution.close()
        return build_canonical_demo_failure_result(
            skill,
            "canonical_run_unavailable",
        )


def _positive_integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = str(environ.get(name, "") or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(raw)


__all__ = [
    "CliRunRuntimeConfig",
    "CliRuntimeCloseUnconfirmed",
    "CliRuntimeBundle",
    "build_canonical_demo_failure_result",
    "execute_canonical_demo_run",
    "execute_root_canonical_demo_run",
    "open_cli_runtime_bundle",
    "reopen_cli_runtime_bundle",
    "resolve_root_run_scope",
    "resolve_cli_run_runtime_config",
    "run_root_canonical_demo",
]
