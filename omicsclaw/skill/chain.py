"""Skill execution helpers for chaining runs from a single tool call.

``run_skill_via_shared_runner`` is the core async wrapper around
``omicsclaw.skill.runner.run_skill`` — it composes forwarded
CLI args, streams stdout/stderr through callbacks, surfaces
user-guidance blocks extracted from skill output, and returns a
uniform result dict that callers (bot tool dispatch, auto-prepare
chain) can consume without duplicating wire-up.

``run_omics_skill_step`` is a higher-level convenience that builds a
fresh timestamped output directory under a caller-supplied
``output_root`` before delegating. ``normalize_extra_args`` filters
the user-supplied ``--`` arg list to drop ``--output`` (the runner
owns that) and rewrite snake_case flags to kebab-case.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path

from omicsclaw.common import run_paths
from omicsclaw.common.user_guidance import (
    extract_user_guidance_lines,
    render_guidance_block,
    strip_user_guidance_lines,
)
from .log_stream import skill_log_emitter_var
from .lookup import lookup_skill_info
from .resource_scheduler import (
    ExecutionResourceRequest,
    get_process_resource_scheduler,
)

logger = logging.getLogger("omicsclaw.skill.chain")

# ADR 0061 D2: every scientific process reserves one indivisible slot from the
# single global Execution Resource Scheduler before it launches, so concurrent
# chain runs (agent tool dispatch, Bench ``execute_omicsclaw``, auto-prepare)
# share the one capacity authority instead of silently overcommitting the host.
# A chain step is an opaque skill subprocess that declares no resources of its
# own, so it reserves the minimal process-count slot: memory/threads/disk are
# pinned to the floor (never gate an undeclared step) and the process dimension
# gates via the budget's ``max_processes``. Mirrors
# ``runtime/workflow/fan_out._STEP_RESOURCE_REQUEST``; a shared home can wait
# until the scheduler module's in-flight edits settle.
_PROCESS_SLOT_REQUEST = ExecutionResourceRequest(
    cpu_cores=1,
    memory_mib=1,
    gpu_devices=0,
    threads=1,
    temporary_disk_mib=0,
)


def normalize_extra_args(extra_args) -> list[str]:
    if not extra_args or not isinstance(extra_args, list):
        return []
    filtered = []
    skip_next = False
    for arg in extra_args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--output":
            skip_next = True
            continue
        if arg.startswith("--output="):
            continue
        if arg.startswith("--"):
            eq_pos = arg.find("=")
            if eq_pos > 0:
                flag_part = arg[:eq_pos].replace("_", "-")
                arg = flag_part + arg[eq_pos:]
            else:
                arg = arg.replace("_", "-")
        filtered.append(arg)
    return filtered


async def run_skill_via_shared_runner(
    *,
    skill_key: str,
    input_path: str | None,
    session_path: str | None,
    mode: str,
    method: str = "",
    data_type: str = "",
    batch_key: str = "",
    n_epochs: int | None = None,
    extra_args: list[str] | None = None,
    out_dir: Path,
    cancel_event: threading.Event | None = None,
) -> dict:
    from . import runner as skill_runner
    from .result import SkillRunResult

    runner_skill = "spatial-pipeline" if skill_key == "pipeline" else skill_key
    skill_info = lookup_skill_info(runner_skill)
    canonical_skill = skill_info.get("alias") or runner_skill

    forwarded_args: list[str] = []
    if method:
        forwarded_args.extend(["--method", method])
    if data_type:
        forwarded_args.extend(["--data-type", data_type])
    if batch_key:
        forwarded_args.extend(["--batch-key", batch_key])
    if n_epochs is not None:
        # The legacy alias ``spatial-domain-identification`` was the only skill
        # that wanted ``--epochs`` instead of ``--n-epochs``, but the registry
        # now resolves that alias to canonical ``spatial-domains`` before we
        # land here — the conditional was dead code. ``argv_builder``'s
        # ``--epochs`` ↔ ``--n-epochs`` rewrite (see
        # ``omicsclaw/core/runtime/argv_builder.py``) handles whichever flag
        # each skill's ``allowed_extra_flags`` actually accepts.
        forwarded_args.extend(["--n-epochs", str(int(n_epochs))])
    forwarded_args.extend(normalize_extra_args(extra_args))

    # Resolve the live-log sink (if a surface installed one) here, in the
    # coroutine context where the ContextVar is visible, and close over it + the
    # run id by value: the per-line callbacks below fire on raw subprocess reader
    # threads (subprocess_driver) which do NOT inherit ContextVars.
    skill_log_sink = skill_log_emitter_var.get(None)
    skill_log_run_id = None
    if skill_log_sink is not None:
        try:
            skill_log_run_id = skill_log_sink.begin_skill(canonical_skill)
        except Exception:
            skill_log_sink = None

    def _emit_stdout(line: str) -> None:
        logger.info("[%s:stdout] %s", canonical_skill, line)
        if skill_log_sink is not None and skill_log_run_id is not None:
            try:
                skill_log_sink.emit(skill_log_run_id, "stdout", line)
            except Exception:
                pass

    def _emit_stderr(line: str) -> None:
        logger.info("[%s:stderr] %s", canonical_skill, line)
        if skill_log_sink is not None and skill_log_run_id is not None:
            try:
                skill_log_sink.emit(skill_log_run_id, "stderr", line)
            except Exception:
                pass

    def _emit_status(message: str) -> None:
        # Adaptive-env provisioning progress ("Preparing environment…/installing …")
        # rides the existing live-log bridge as a distinct ``status`` stream tag so
        # the chat shows it before the skill subprocess produces any output.
        logger.info("[%s:status] %s", canonical_skill, message)
        if skill_log_sink is not None and skill_log_run_id is not None:
            try:
                skill_log_sink.emit(skill_log_run_id, "status", message)
            except Exception:
                pass

    # ADR 0009: prefer the caller-supplied cancel_event (the per-request
    # signal threaded down from MessageEnvelope) so Surface-initiated aborts
    # propagate to subprocess_driver._cancel_watcher. Fall back to a local
    # Event when no caller is wired (preserves legacy callers and the
    # asyncio.CancelledError → cancel_event.set() bridge below).
    effective_cancel_event = cancel_event if cancel_event is not None else threading.Event()

    def _run() -> SkillRunResult:
        return skill_runner.run_skill(
            runner_skill,
            input_path=str(input_path) if input_path else None,
            output_dir=str(out_dir),
            demo=mode == "demo",
            session_path=session_path,
            extra_args=forwarded_args or None,
            stdout_callback=_emit_stdout,
            stderr_callback=_emit_stderr,
            cancel_event=effective_cancel_event,
            status_callback=_emit_status,
        )

    # Admission gate: hold one global process slot for the lifetime of the
    # subprocess, released on success, failure or cancellation by reserve()'s
    # context manager. The chain never runs inside another lease — its callers
    # are the agent loop, Bench executors and preflight, never a fan-out / plan /
    # RunRuntime step that already holds one — so this reservation cannot nest
    # and deadlock the process-global FIFO.
    scheduler = get_process_resource_scheduler(out_dir)
    async with scheduler.reserve(_PROCESS_SLOT_REQUEST):
        try:
            run_result = await asyncio.to_thread(_run)
        except asyncio.CancelledError:
            effective_cancel_event.set()
            raise
    stdout_str = run_result.stdout
    stderr_str = run_result.stderr
    guidance_block = render_guidance_block(extract_user_guidance_lines(stderr_str))
    clean_stderr = strip_user_guidance_lines(stderr_str)
    clean_stdout = strip_user_guidance_lines(stdout_str)
    error_text = clean_stderr[-1500:] if clean_stderr else clean_stdout[-1500:] if clean_stdout else "unknown error"
    returncode = run_result.adapter_exit_code
    output_dir = run_result.output_path or out_dir
    return {
        "success": run_result.success,
        "returncode": returncode,
        "out_dir": output_dir,
        "output_dir": str(output_dir),
        "stdout": stdout_str,
        "stderr": stderr_str,
        "guidance_block": guidance_block,
        "error_text": error_text,
    }


async def run_omics_skill_step(
    *,
    output_root: Path,
    skill_key: str,
    input_path: str | None,
    mode: str,
    method: str = "",
    data_type: str = "",
    batch_key: str = "",
    n_epochs: int | None = None,
    extra_args: list[str] | None = None,
    project_id: str = "",
    project_name: str = "",
    cancel_event: threading.Event | None = None,
) -> dict:
    # ADR 0035: place the Run under its Project (default when thread-less).
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = run_paths.resolve_run_dir(
        output_root=output_root,
        skill=skill_key,
        project_id=project_id,
        project_name=project_name,
        input_path=input_path,
        demo=(mode == "demo"),
        method=method,
        timestamp=ts,
    ).run_dir

    return await run_skill_via_shared_runner(
        skill_key=skill_key,
        input_path=input_path,
        session_path=None,
        mode=mode,
        method=method,
        data_type=data_type,
        batch_key=batch_key,
        n_epochs=n_epochs,
        extra_args=extra_args,
        out_dir=out_dir,
        cancel_event=cancel_event,
    )


__all__ = [
    "normalize_extra_args",
    "run_skill_via_shared_runner",
    "run_omics_skill_step",
]
