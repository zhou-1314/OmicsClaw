"""Shared OmicsClaw skill execution runner.

The public surface here is ``run_skill`` (and a few legacy aliases). The
heavy lifting was carved out of this module into ``omicsclaw.skill.execution``
during OMI-12 P1.4:

- ``runtime.argv_builder``    — argv + filtered LLM-supplied flags
- ``runtime.subprocess_driver`` — Popen + reaper + cancel + log streaming
- ``runtime.output_finalize`` — rename, README, reproducibility notebook
- ``runtime.pipeline_runner`` — ``spatial-pipeline`` chain

The repository-root ``omicsclaw.py`` file remains the CLI wrapper, but any
surface that needs to run a skill should import ``run_skill`` from here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .registry import ensure_registry_loaded, registry
from .execution.argv_builder import (
    build_skill_argv,
    build_user_run_command,
    extract_flag_value,
    filter_forwarded_args,
)
from .execution.async_subprocess_driver import adrive_subprocess
from .execution.output_finalize import (
    deduplicate_path,
    finalize_output_directory,
    write_pipeline_readme,
)
from .execution.pipeline_runner import (
    SPATIAL_PIPELINE,
    run_pipeline_by_name,
    run_spatial_pipeline,
)
from .execution.python_runtime import get_skill_runner_python
from .execution.subprocess_driver import drive_subprocess
from .result import SkillRunResult, build_skill_run_result


OMICSCLAW_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = OMICSCLAW_DIR / "output"
# Honour ``OMICSCLAW_RUN_PYTHON`` (falls back to ``sys.executable``). Resolved
# fresh per run inside ``_prepare_skill_run``; this module constant is kept for
# backward-compat with importers and reflects the override at import time.
PYTHON = get_skill_runner_python()

if str(OMICSCLAW_DIR) not in sys.path:
    sys.path.insert(0, str(OMICSCLAW_DIR))

_COLOUR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
BOLD = "\033[1m" if _COLOUR else ""
GREEN = "\033[32m" if _COLOUR else ""
RED = "\033[31m" if _COLOUR else ""
CYAN = "\033[36m" if _COLOUR else ""
RESET = "\033[0m" if _COLOUR else ""


# ---------------------------------------------------------------------------
# Backwards-compatible aliases — tests and a few external surfaces import
# these private helpers by name. Keep them re-exported so the carve-out is
# transparent to callers.
# ---------------------------------------------------------------------------

_extract_flag_value = extract_flag_value
_build_user_run_command = build_user_run_command
_deduplicate_path = deduplicate_path
_finalize_output_directory = finalize_output_directory
_write_pipeline_readme = write_pipeline_readme


@dataclass(frozen=True)
class _PreparedSkillRun:
    """Everything ``run_skill`` / ``arun_skill`` need after setup, before spawn.

    Carved out of ``run_skill`` so the sync (CLI / bot / pipeline) path and
    the async executor path (OMI-12 audit P1 #4) can share the same
    resolution, argv build, and output-finalize logic without duplicating
    ~70 lines of bookkeeping. The two entry points differ only in *which*
    subprocess driver they call (sync ``drive_subprocess`` vs async
    ``adrive_subprocess``).
    """

    skill_name: str
    skill_info: dict[str, Any]
    script_path: Path
    resolved_input: str | None
    resolved_input_paths: list[str] | None
    out_dir: Path
    user_supplied_output_dir: bool
    generated_ts: str
    requested_method: str | None
    cmd: list[str]
    filtered_extra_args: list[str]
    env: dict[str, str]
    demo: bool
    session_path: str | None


def _prepare_skill_run(
    skill_name: str,
    *,
    input_path: str | None,
    input_paths: list[str] | None,
    output_dir: str | None,
    demo: bool,
    session_path: str | None,
    extra_args: list[str] | None,
    project_id: str = "",
    project_name: str = "",
    log_banner: bool = True,
) -> _PreparedSkillRun | SkillRunResult:
    """Resolve skill, build argv, prepare output dir. Returns the prepared
    run or a stable ``_err`` ``SkillRunResult`` on setup failure.

    ``log_banner`` controls whether the human-readable "Running …" banner
    prints; the async executor path keeps it silent because the jobs
    router has its own log stream.
    """
    skills = ensure_registry_loaded().skills
    skill_info = skills.get(skill_name)
    if skill_info is None:
        return _err(skill_name, f"Unknown skill '{skill_name}'. Available: {list(skills.keys())}")

    script_path: Path = skill_info["script"]
    if not script_path.exists():
        return _err(skill_name, f"Script not found: {script_path}")

    resolved_input_paths: list[str] | None = None
    if input_paths and len(input_paths) >= 2:
        resolved_input_paths = [str(Path(p).resolve()) for p in input_paths]

    resolved_input = input_path
    if session_path and not input_path and not demo and not resolved_input_paths:
        from omicsclaw.common.session import SpatialSession

        session = SpatialSession.load(session_path)
        if session.h5ad_path:
            resolved_input = session.h5ad_path

    if resolved_input:
        # Only resolve inputs that are an actual local file/dir. Free-form skill
        # inputs — a literature DOI ("10.1038/..."), a URL, or raw text — are NOT
        # paths: ``Path(...).resolve()`` would mangle them into a bogus
        # ``<cwd>/10.1038/...`` so the skill mis-detects the input type (the
        # documented ``oc run literature --input <doi|url>`` then silently falls
        # back to 'text'). A non-existent input passes through verbatim for the
        # skill to interpret (audit B).
        _candidate = Path(resolved_input)
        if _candidate.exists():
            resolved_input = str(_candidate.resolve())

    user_supplied_output_dir = output_dir is not None
    generated_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    requested_method = extract_flag_value(extra_args, "--method")

    if output_dir:
        # Explicit ``--output`` wins and keeps the user's exact layout; the
        # project-scoped resolver is bypassed (and so is index/manifest finalize,
        # gated on a sibling ``project_meta.json`` in ``_finalize_skill_run``).
        out_dir = Path(output_dir).resolve()
        # CLAUDE.md safety rule 4 / ADR 0035: warn before overwriting a non-empty
        # report directory (resolver-managed runs never collide thanks to the uid).
        if log_banner and out_dir.exists() and any(out_dir.iterdir()):
            print(
                f"{RED}Warning:{RESET} output dir {out_dir} already exists and is not "
                "empty — existing files may be overwritten.",
                file=sys.stderr,
            )
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        # ADR 0035: place the Run under its Project, with a readable, globally
        # unique, atomically-reserved directory name.
        from omicsclaw.common import run_paths

        output_root = Path(
            os.getenv("OMICSCLAW_OUTPUT_DIR", "") or DEFAULT_OUTPUT_ROOT
        ).expanduser()
        resolution = run_paths.resolve_run_dir(
            output_root=output_root,
            skill=skill_name,
            project_id=project_id,
            project_name=project_name,
            input_path=resolved_input,
            input_paths=resolved_input_paths,
            demo=demo,
            method=requested_method,
            timestamp=generated_ts,
        )
        out_dir = resolution.run_dir

    cmd = build_skill_argv(
        python_executable=get_skill_runner_python(),
        script_path=script_path,
        skill_info=skill_info,
        demo=demo,
        input_path=resolved_input,
        input_paths=resolved_input_paths,
        output_dir=out_dir,
    )
    if cmd is None:
        if demo and not skill_info.get("demo_args", ["--demo"]):
            return _err(
                skill_name,
                f"`{skill_name}` does not support --demo (workflow skills run on "
                "real preprocessed data via the consensus runtime); provide "
                "--input <preprocessed.h5ad>.",
            )
        return _err(skill_name, "No --input, --demo, or --session provided.")

    if log_banner:
        domain = skill_info.get("domain", "unknown")
        domain_display = registry.domains.get(domain, {}).get("name", domain.title())
        if demo:
            mode_str = f"{CYAN}demo mode{RESET}"
        elif resolved_input_paths:
            mode_str = f"inputs: {', '.join(resolved_input_paths)}"
        else:
            mode_str = f"input: {resolved_input}"
        print(f"\n{BOLD}Running {domain_display} skill:{RESET} {GREEN}{skill_name}{RESET} ({mode_str})")
        print(f"{BOLD}Output:{RESET} {out_dir}\n")

    filtered = filter_forwarded_args(
        extra_args,
        allowed_extra_flags=skill_info.get("allowed_extra_flags", set()),
    )
    cmd.extend(filtered)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(OMICSCLAW_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    # Isolate skill subprocesses from the user-site (``~/.local``) so a broken
    # or ABI-mismatched package there cannot shadow the analysis env's deps
    # (e.g. a stale ``~/.local`` torch breaking CellCharter). Operators can opt
    # out by exporting ``PYTHONNOUSERSITE=0`` before launch.
    env.setdefault("PYTHONNOUSERSITE", "1")

    return _PreparedSkillRun(
        skill_name=skill_name,
        skill_info=skill_info,
        script_path=script_path,
        resolved_input=resolved_input,
        resolved_input_paths=resolved_input_paths,
        out_dir=out_dir,
        user_supplied_output_dir=user_supplied_output_dir,
        generated_ts=generated_ts,
        requested_method=requested_method,
        cmd=cmd,
        filtered_extra_args=filtered,
        env=env,
        demo=demo,
        session_path=session_path,
    )


def _finalize_skill_run(
    prepared: _PreparedSkillRun,
    proc: subprocess.CompletedProcess,
    duration: float,
) -> SkillRunResult:
    """Run output finalization, build a ``SkillRunResult``, store session.

    Shared between the sync ``run_skill`` and the async ``arun_skill`` so
    success / failure shape stays identical regardless of which subprocess
    driver fired.
    """
    final_out_dir = prepared.out_dir
    actual_method = prepared.requested_method
    readme_path = ""
    notebook_path = ""
    if proc.returncode == 0:
        user_command = build_user_run_command(
            skill_name=prepared.skill_name,
            demo=prepared.demo,
            input_path=prepared.resolved_input,
            output_dir=prepared.out_dir,
            forwarded_args=prepared.filtered_extra_args,
        )
        final_out_dir, actual_method, readme_path, notebook_path, _ = finalize_output_directory(
            prepared.out_dir,
            skill_name=prepared.skill_name,
            skill_info=prepared.skill_info,
            timestamp=prepared.generated_ts,
            user_supplied_output_dir=prepared.user_supplied_output_dir,
            preferred_method=prepared.requested_method,
            actual_command=user_command,
        )
        # ADR 0035: record the Run in its Project (enrich manifest.json + append
        # the rebuildable index.jsonl). Gated on a sibling ``project_meta.json`` so
        # resolver-managed runs (CLI default + agent + channel) are indexed while an
        # explicit ``--output`` outside the output root is left untouched. Never let
        # bookkeeping failure break a successful run.
        try:
            from omicsclaw.common import run_paths

            if (final_out_dir.parent / run_paths.PROJECT_META_FILENAME).exists():
                run_paths.finalize_run(
                    final_out_dir,
                    skill=prepared.skill_name,
                    status="completed",
                    method=actual_method,
                    input_path=prepared.resolved_input,
                    surface="skill-runner",
                )
        except Exception:  # pragma: no cover - defensive
            pass

    output_files = sorted(
        [path.name for path in final_out_dir.rglob("*") if path.is_file()]
    ) if final_out_dir.exists() else []

    result = build_skill_run_result(
        skill=prepared.skill_name,
        success=proc.returncode == 0,
        exit_code=proc.returncode,
        output_dir=final_out_dir,
        files=output_files,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_seconds=duration,
        method=actual_method,
        readme_path=readme_path,
        notebook_path=notebook_path,
    )

    if prepared.session_path and result.success:
        _store_result_in_session(prepared.session_path, prepared.skill_name, final_out_dir)

    return result


def resolve_skill_alias(skill_name: str) -> str:
    """Resolve a user-facing skill name or legacy alias to its canonical alias."""
    skills = ensure_registry_loaded().skills
    if skill_name in skills:
        return skills[skill_name].get("alias", skill_name)

    for skill_key, skill_info in skills.items():
        legacy_aliases = skill_info.get("legacy_aliases", [])
        if skill_name in legacy_aliases:
            return skill_key

    if ":" in skill_name:
        _domain, skill = skill_name.split(":", 1)
        if skill in skills:
            return skill

    return skill_name


def run_skill(
    skill_name: str,
    *,
    input_path: str | None = None,
    input_paths: list[str] | None = None,
    output_dir: str | None = None,
    demo: bool = False,
    session_path: str | None = None,
    extra_args: list[str] | None = None,
    project_id: str = "",
    project_name: str = "",
    stdout_callback: Callable[[str], None] | None = None,
    stderr_callback: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SkillRunResult:
    """Run a single skill via subprocess and return a ``SkillRunResult``.

    The runner returns a typed ``SkillRunResult`` natively (OMI-12 P1.6);
    callers that still expect the legacy dict shape should call
    ``.to_legacy_dict()`` at their own boundary. Internal consumers that
    used to immediately do ``coerce_skill_run_result(run_skill(...))`` can
    drop the coercion and use the returned model directly.

    When ``stdout_callback`` / ``stderr_callback`` are supplied the runner
    invokes them once per line as the skill emits output (newline stripped),
    so long-running skills produce visible logs in real time. Aggregated
    ``stdout`` / ``stderr`` strings are still returned on the result.

    When ``cancel_event`` is supplied the runner watches it; if the event is
    set while the skill is running the child process group receives SIGTERM,
    waits a short grace period, then SIGKILL, ensuring cancelled jobs do not
    leak children consuming CPU/GPU until natural completion.
    """
    skill_name = resolve_skill_alias(skill_name)

    # Any ``<name>-pipeline`` whose YAML lives in ``pipelines/`` is dispatched
    # through the generic chain runner. ``run_pipeline_by_name`` returns
    # ``None`` when no YAML matches the alias, in which case we fall through
    # to the regular skill registry lookup so genuinely unknown aliases still
    # surface the standard "Unknown skill" error.
    if skill_name.endswith("-pipeline"):
        pipeline_result = run_pipeline_by_name(
            skill_name,
            default_output_root=Path(
                os.getenv("OMICSCLAW_OUTPUT_DIR", "") or DEFAULT_OUTPUT_ROOT
            ).expanduser(),
            err_factory=_err,
            input_path=input_path,
            output_dir=output_dir,
            demo=demo,
            session_path=session_path,
            project_id=project_id,
            project_name=project_name,
        )
        if pipeline_result is not None:
            return pipeline_result

    prepared = _prepare_skill_run(
        skill_name,
        input_path=input_path,
        input_paths=input_paths,
        output_dir=output_dir,
        demo=demo,
        session_path=session_path,
        extra_args=extra_args,
        project_id=project_id,
        project_name=project_name,
    )
    if isinstance(prepared, SkillRunResult):
        return prepared

    t0 = time.time()
    try:
        proc = drive_subprocess(
            prepared.cmd,
            cwd=prepared.script_path.parent,
            env=prepared.env,
            out_dir=prepared.out_dir,
            stdout_callback=stdout_callback,
            stderr_callback=stderr_callback,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        duration = time.time() - t0
        return _err(skill_name, str(exc), duration=duration)

    duration = time.time() - t0
    return _finalize_skill_run(prepared, proc, duration)


async def arun_skill(
    skill_name: str,
    *,
    input_path: str | None = None,
    input_paths: list[str] | None = None,
    output_dir: str | None = None,
    demo: bool = False,
    session_path: str | None = None,
    extra_args: list[str] | None = None,
    project_id: str = "",
    project_name: str = "",
) -> SkillRunResult:
    """Async sibling of :func:`run_skill` for callers already in an event loop.

    OMI-12 audit P1 #4: ``SkillRunnerExecutor`` used to wrap the blocking
    ``run_skill`` in ``asyncio.to_thread``, which parked one
    ``ThreadPoolExecutor`` worker for every active skill. With the default
    32-worker pool, busy desktop-server traffic could exhaust the pool and
    stall unrelated async work. This async-native entry point spawns the
    skill subprocess via :func:`asyncio.create_subprocess_exec` instead,
    so concurrent skills only cost one async task each, not one OS thread.

    Behavior parity with ``run_skill``:

    - Same skill resolution, argv build, env (``PYTHONPATH``), cwd
      (script's parent dir), and output-finalize logic (rename, README,
      notebook) — they share ``_prepare_skill_run`` and
      ``_finalize_skill_run``.
    - Same status-field + ``-9 → 0`` fallback for deciding success.
    - Same ``SkillRunResult`` return shape.

    Behavior deltas (documented):

    - Pipeline dispatch (``<name>-pipeline``) is **not** handled here.
      The async path is meant for the executor (one skill at a time);
      pipelines run via the sync ``run_skill`` from CLI / bot.
    - ``stdout_callback`` / ``stderr_callback`` are not supported.
      Per-line log streaming stays on the sync path that the bot uses.
    - Cancellation flows via :class:`asyncio.CancelledError` instead of
      a ``threading.Event`` — propagating the cancel through the
      awaiting task is enough; the underlying
      :func:`omicsclaw.skill.execution.async_subprocess_driver.adrive_subprocess`
      SIGTERM/SIGKILLs the process group on its way out.
    """
    skill_name = resolve_skill_alias(skill_name)

    prepared = _prepare_skill_run(
        skill_name,
        input_path=input_path,
        input_paths=input_paths,
        output_dir=output_dir,
        demo=demo,
        session_path=session_path,
        extra_args=extra_args,
        project_id=project_id,
        project_name=project_name,
        log_banner=False,
    )
    if isinstance(prepared, SkillRunResult):
        return prepared

    t0 = time.time()
    try:
        proc = await adrive_subprocess(
            prepared.cmd,
            cwd=prepared.script_path.parent,
            env=prepared.env,
            out_dir=prepared.out_dir,
        )
    except asyncio.CancelledError:
        # Re-raise so the awaiting task sees the cancellation. The driver
        # already SIGTERM/SIGKILL'd the process group on its way out, so
        # no child is left behind.
        raise
    except Exception as exc:
        duration = time.time() - t0
        return _err(skill_name, str(exc), duration=duration)

    duration = time.time() - t0
    return _finalize_skill_run(prepared, proc, duration)


def _store_result_in_session(session_path: str, skill_name: str, out_dir: Path) -> None:
    """Store skill result back into the session JSON."""
    try:
        from omicsclaw.common.session import SpatialSession

        result_json = out_dir / "result.json"
        if not result_json.exists():
            return
        session = SpatialSession.load(session_path)
        result_data = json.loads(result_json.read_text())
        session.add_skill_result(skill_name, result_data, output_dir=str(out_dir))

        processed = out_dir / "processed.h5ad"
        if processed.exists():
            session.h5ad_path = str(processed)
            session.mark_step(skill_name)

        session.save(session_path)
    except Exception:
        pass


def _err(skill: str, msg: str, duration: float = 0) -> SkillRunResult:
    return build_skill_run_result(
        skill=skill,
        success=False,
        exit_code=-1,
        output_dir=None,
        stderr=msg,
        duration_seconds=duration,
    )


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "OMICSCLAW_DIR",
    "PYTHON",
    "SPATIAL_PIPELINE",
    "resolve_skill_alias",
    "run_skill",
]
