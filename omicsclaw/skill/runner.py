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
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .registry import (
    RegistrySnapshot,
    ensure_registry_loaded,
    governed_skill_replacement,
)
from .preconditions import format_precondition_failure, preflight_skill_execution
from .execution.argv_builder import (
    build_skill_argv,
    build_user_run_command,
    extract_flag_value,
    filter_forwarded_args,
)
from .execution.async_subprocess_driver import (
    ProcessTreeStopUnconfirmed,
    adrive_subprocess,
)
from .execution.environment import scrub_internal_control_credentials
from .execution.env_resolver import resolve_skill_runtime
from .execution.output_finalize import (
    deduplicate_path,
    finalize_output_directory,
    write_pipeline_readme,
)
from .execution.output_ownership import (
    OutputDirectoryClaimError,
    bind_output_claim_audit_identity,
    claim_fresh_output_directory,
    collect_output_claim_identities,
    is_scientific_output_file,
)
from .execution.pipeline_runner import (
    SPATIAL_PIPELINE,
    run_pipeline_by_name,
    run_spatial_pipeline,  # noqa: F401 - backward-compatible module export
)
from .execution.python_runtime import get_skill_runner_python
from .execution.subprocess_driver import drive_subprocess
from .execution_contract import verify_skill_run_outputs
from .result import SkillRunAuditIdentity, SkillRunResult, build_skill_run_result


OMICSCLAW_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = OMICSCLAW_DIR / "output"
# Honour ``OMICSCLAW_RUN_PYTHON`` (falls back to ``sys.executable``). Resolved
# fresh per run inside ``_prepare_skill_run``; this module constant is kept for
# backward-compat with importers and reflects the override at import time.
PYTHON = get_skill_runner_python()
logger = logging.getLogger(__name__)

if str(OMICSCLAW_DIR) not in sys.path:
    sys.path.insert(0, str(OMICSCLAW_DIR))

_COLOUR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
BOLD = "\033[1m" if _COLOUR else ""
GREEN = "\033[32m" if _COLOUR else ""
CYAN = "\033[36m" if _COLOUR else ""
RESET = "\033[0m" if _COLOUR else ""

_RESOURCE_THREAD_ENV = frozenset(
    {
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    }
)
_RESOURCE_ENV = _RESOURCE_THREAD_ENV | {"CUDA_VISIBLE_DEVICES", "TMPDIR"}
_PROVENANCE_DRIFT_MESSAGE = (
    "Skill execution provenance changed while the subprocess was running."
)


def _anndata_validation_env(
    runtime_env: Mapping[str, str],
    *,
    runtime_cwd: Path,
) -> dict[str, str]:
    """Keep the Skill runtime environment without Backend import authority.

    Skill entry points need the repository root prepended to ``PYTHONPATH`` so
    local ``omicsclaw`` imports work.  The AnnData verifier does not: retaining
    that runner-owned prefix would let an ``anndata.py`` in the Backend source
    tree shadow the package installed in the selected Skill runtime.  Remove
    every Backend-root-equivalent entry (plus unsafe empty entries), preserving
    every other caller/runtime path and all site/virtual-environment variables.
    """

    env = {str(key): str(value) for key, value in runtime_env.items()}
    pythonpath = env.get("PYTHONPATH")
    if pythonpath is None:
        return env
    try:
        backend_root = OMICSCLAW_DIR.resolve(strict=False)
    except (OSError, RuntimeError):
        backend_root = OMICSCLAW_DIR.absolute()

    try:
        producer_cwd = Path(runtime_cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("Skill runtime cwd cannot be verified") from exc
    if not producer_cwd.is_dir():
        raise RuntimeError("Skill runtime cwd is not a directory")

    entries: list[str] = []
    for entry in pythonpath.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            candidate = producer_cwd / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            # A path that cannot be canonicalised (notably a symlink loop)
            # cannot be proven independent from Backend import authority.
            continue
        if candidate == backend_root:
            continue
        # Freeze relative entries against the exact producer cwd before the
        # verifier starts.  ``python -P`` still honours explicit PYTHONPATH,
        # and retaining a relative spelling would let verifier cwd select a
        # different import graph from the Skill subprocess.
        entries.append(str(candidate))
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
    else:
        env.pop("PYTHONPATH", None)
    return env


def _visible_output_files(output_dir: Path) -> list[str]:
    claim_identities = collect_output_claim_identities(output_dir)
    return sorted(
        path.name
        for path in output_dir.rglob("*")
        if is_scientific_output_file(
            path,
            output_root=output_dir,
            claim_identities=claim_identities,
        )
    )


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
    skills_root: Path
    # Immutable pre-spawn audit identity. These hashes describe the code that
    # was selected for execution even if a developer edits files while the
    # subprocess is running.
    skill_hash: str
    source_hash: str
    environment_id: str
    requires_manifest: bool = False
    # Adaptive-env provenance (which interpreter served the run).
    runtime_source: str = "base"


def _result_audit_identity(
    *,
    skill: str,
    skill_info: Mapping[str, Any] | None,
    skill_hash: str | None,
    source_hash: str | None,
    environment_id: str | None,
) -> SkillRunAuditIdentity | None:
    """Build result provenance only when both pre-spawn hashes were frozen."""
    if skill_hash is None or source_hash is None:
        return None
    info = skill_info or {}
    return SkillRunAuditIdentity(
        skill_id=str(info.get("alias") or info.get("canonical_name") or skill),
        skill_version=str(info.get("version") or "unknown"),
        skill_hash=skill_hash or "unknown",
        source_hash=source_hash or "unknown",
        environment_id=environment_id or "unknown",
    )


def _prepared_audit_identity(prepared: _PreparedSkillRun) -> SkillRunAuditIdentity:
    identity = _result_audit_identity(
        skill=prepared.skill_name,
        skill_info=prepared.skill_info,
        skill_hash=prepared.skill_hash,
        source_hash=prepared.source_hash,
        environment_id=prepared.environment_id,
    )
    if identity is None:  # pragma: no cover - prepared runs always freeze hashes
        raise RuntimeError("prepared Skill run has no frozen audit identity")
    return identity


def _prepared_skill_revision(prepared: _PreparedSkillRun) -> dict[str, str]:
    identity = _prepared_audit_identity(prepared)
    return {
        "skill_id": identity.skill_id,
        "skill_version": identity.skill_version,
        "manifest_hash": identity.skill_hash,
        "source_hash": identity.source_hash,
    }


def _prepared_revision_is_current(prepared: _PreparedSkillRun) -> bool:
    """Return whether on-disk execution provenance still matches pre-spawn."""
    try:
        from .evolution import capture_skill_execution_identity

        manifest_hash, source_hash = capture_skill_execution_identity(
            prepared.script_path,
            skills_root=prepared.skills_root,
            directory_name=(
                str(prepared.skill_info.get("directory_name") or "")
                if prepared.requires_manifest
                else ""
            ),
        )
    except Exception:
        return False
    return manifest_hash == prepared.skill_hash and source_hash == prepared.source_hash


def _prepared_framework_failure(
    prepared: _PreparedSkillRun,
    *,
    duration: float,
    message: str,
) -> SkillRunResult:
    """Record one sanitized framework/provenance failure for a prepared run."""
    files = _visible_output_files(prepared.out_dir) if prepared.out_dir.exists() else []
    result = build_skill_run_result(
        skill=prepared.skill_name,
        success=False,
        exit_code=1,
        output_dir=prepared.out_dir,
        files=files,
        stderr=message,
        duration_seconds=duration,
        method=prepared.requested_method,
        runtime_source=prepared.runtime_source,
        error_kind="contract_validator_failed",
        audit_identity=_prepared_audit_identity(prepared),
    )
    _record_skill_run_event(
        result,
        skill_info=prepared.skill_info,
        evidence_kind="demo" if prepared.demo else "ordinary",
        skill_hash=prepared.skill_hash,
        source_hash=prepared.source_hash,
        runtime_entry_path=prepared.script_path,
        environment_id=prepared.environment_id,
    )
    return result


def _prepared_missing_runtime_failure(
    prepared: _PreparedSkillRun,
    *,
    duration: float,
) -> SkillRunResult:
    """Record an actionable missing-interpreter dependency failure."""
    runtime_language = str(prepared.skill_info.get("runtime_language") or "python")
    interpreter = prepared.cmd[0] if prepared.cmd else runtime_language
    result = build_skill_run_result(
        skill=prepared.skill_name,
        success=False,
        exit_code=-1,
        output_dir=prepared.out_dir,
        stderr=(
            f"Required {runtime_language} runtime interpreter {interpreter!r} "
            "is not installed or not available on PATH."
        ),
        duration_seconds=duration,
        method=prepared.requested_method,
        runtime_source=prepared.runtime_source,
        error_kind="missing_dependency",
        audit_identity=_prepared_audit_identity(prepared),
    )
    _record_skill_run_event(
        result,
        skill_info=prepared.skill_info,
        evidence_kind="demo" if prepared.demo else "ordinary",
        skill_hash=prepared.skill_hash,
        source_hash=prepared.source_hash,
        runtime_entry_path=prepared.script_path,
        environment_id=prepared.environment_id,
    )
    return result


def _record_prepared_cancellation(
    prepared: _PreparedSkillRun,
    *,
    duration: float,
) -> None:
    """Persist exactly one cancellation event for an already prepared run."""
    result = build_skill_run_result(
        skill=prepared.skill_name,
        success=False,
        exit_code=-1,
        output_dir=prepared.out_dir,
        stderr="Skill execution cancelled.",
        duration_seconds=duration,
        method=prepared.requested_method,
        runtime_source=prepared.runtime_source,
        error_kind="cancelled",
        audit_identity=_prepared_audit_identity(prepared),
    )
    _record_skill_run_event(
        result,
        skill_info=prepared.skill_info,
        evidence_kind="demo" if prepared.demo else "ordinary",
        skill_hash=prepared.skill_hash,
        source_hash=prepared.source_hash,
        runtime_entry_path=prepared.script_path,
        environment_id=prepared.environment_id,
    )


def _prepare_skill_run(
    skill_name: str,
    *,
    input_path: str | None,
    input_paths: list[str] | None,
    output_dir: str | None,
    demo: bool,
    session_path: str | None,
    extra_args: list[str] | None,
    resource_env: Mapping[str, str] | None = None,
    trusted_resource_temp_dir: str | None = None,
    allow_adaptive_environment: bool = True,
    project_id: str = "",
    project_name: str = "",
    log_banner: bool = True,
    status_callback: Callable[[str], None] | None = None,
    registry_snapshot: RegistrySnapshot | None = None,
    expected_skill_revision: Mapping[str, str] | None = None,
) -> _PreparedSkillRun | SkillRunResult:
    """Resolve skill, build argv, prepare output dir. Returns the prepared
    run or a stable ``_err`` ``SkillRunResult`` on setup failure.

    ``log_banner`` controls whether the human-readable "Running …" banner
    prints; the async executor path keeps it silent because the jobs
    router has its own log stream.
    """
    snapshot = registry_snapshot or ensure_registry_loaded().snapshot()
    skills = snapshot.skills
    skills_root = snapshot.loaded_dir
    skill_info = skills.get(skill_name)
    if skill_info is None:
        return _err(
            skill_name,
            f"Unknown skill '{skill_name}'. Available: {list(skills.keys())}",
        )

    if str(skill_info.get("lifecycle_status") or "mvp") == "deprecated":
        replacement = governed_skill_replacement(snapshot, skill_info)
        hint = (
            f" Use governed replacement '{replacement[0]}'."
            if replacement is not None
            else " No governed replacement is currently available."
        )
        return _err(
            skill_name,
            f"Skill '{skill_name}' is deprecated.{hint}",
            skill_info=skill_info,
            skills_root=skills_root,
        )

    script_path: Path = skill_info["script"]
    if not script_path.exists():
        return _err(
            skill_name,
            f"Script not found: {script_path}",
            skill_info=skill_info,
            skills_root=skills_root,
        )
    registry_skill_revision: dict[str, str] | None = None
    requires_manifest = snapshot.skill_manifest_revisions.get(skill_name) not in (
        None,
        "unknown",
    )
    if (
        skill_name in snapshot.skill_manifest_revisions
        or expected_skill_revision is not None
    ):
        try:
            registry_skill_revision = snapshot.skill_revision(skill_name)
        except Exception:
            return _err(
                skill_name,
                "Skill manifest no longer matches the loaded Registry; "
                "reload the Registry before execution.",
                skill_info=skill_info,
                skills_root=skills_root,
                skill_hash="unknown",
                source_hash="unknown",
                environment_id="unknown",
            )
    if expected_skill_revision is not None and (
        registry_skill_revision is None
        or registry_skill_revision != dict(expected_skill_revision)
    ):
        revision = registry_skill_revision or {}
        return _err(
            skill_name,
            "Skill execution provenance does not match the bound authority.",
            skill_info=skill_info,
            skills_root=skills_root,
            evidence_kind="demo" if demo else "ordinary",
            skill_hash=revision.get("manifest_hash", "unknown"),
            source_hash=revision.get("source_hash", "unknown"),
            runtime_entry_path=script_path,
            environment_id="unknown",
            error_kind="contract_validator_failed",
        )

    resolved_input_paths: list[str] | None = None
    if input_paths and len(input_paths) >= 2:
        resolved_input_paths = [str(Path(p).resolve()) for p in input_paths]

    resolved_input = input_path
    if session_path and not input_path and not demo and not resolved_input_paths:
        session_assessment = preflight_skill_execution(
            skill_name,
            session_path=session_path,
            registry=snapshot,
        )
        if session_assessment is not None and not session_assessment.execution_ready:
            return _err(
                skill_name,
                format_precondition_failure(session_assessment),
                skill_info=skill_info,
                skills_root=skills_root,
            )

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

    assessment = preflight_skill_execution(
        skill_name,
        input_path=resolved_input,
        input_paths=resolved_input_paths,
        demo=demo,
        companion_paths=[
            value
            for value in (
                extract_flag_value(extra_args, "--read2")
                if "--read2" in skill_info.get("allowed_extra_flags", set())
                else None,
            )
            if value
        ],
        registry=snapshot,
    )
    if assessment is not None and not assessment.execution_ready:
        return _err(
            skill_name,
            format_precondition_failure(assessment),
            skill_info=skill_info,
            skills_root=skills_root,
        )

    if demo and not skill_info.get("demo_args", ["--demo"]):
        return _err(
            skill_name,
            f"`{skill_name}` does not support --demo (consensus skills run on "
            "real preprocessed data via the consensus runtime); provide "
            "--input <preprocessed.h5ad>.",
            skill_info=skill_info,
            skills_root=skills_root,
        )
    if not demo and not resolved_input and not resolved_input_paths:
        return _err(
            skill_name,
            "No --input, --demo, or --session provided.",
            skill_info=skill_info,
            skills_root=skills_root,
        )

    # ``extra_args`` is an untrusted request surface. Method identity must come
    # from the argv that will actually reach the Skill, never from a raw flag
    # that the allow-list removed. An explicit unsupported method is a semantic
    # request, so fail visibly instead of silently running a Skill default.
    filtered = filter_forwarded_args(
        extra_args,
        allowed_extra_flags=skill_info.get("allowed_extra_flags", set()),
    )
    raw_method_flag = any(
        token == "--method" or token.startswith("--method=")
        for token in (extra_args or [])
    )
    raw_requested_method = extract_flag_value(extra_args, "--method")
    requested_method = extract_flag_value(filtered, "--method")
    if raw_method_flag and not raw_requested_method:
        return _err(
            skill_name,
            f"Skill '{skill_name}' requires a non-empty value for --method.",
            skill_info=skill_info,
            skills_root=skills_root,
        )
    if raw_method_flag and requested_method != raw_requested_method:
        return _err(
            skill_name,
            f"Skill '{skill_name}' does not expose the unified --method flag; "
            "use only flags declared by that Skill's runtime contract.",
            skill_info=skill_info,
            skills_root=skills_root,
        )

    user_supplied_output_dir = output_dir is not None
    generated_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if output_dir:
        # Explicit ``--output`` wins and keeps the user's exact layout; the
        # project-scoped resolver is bypassed (and so is index/manifest finalize,
        # gated on a sibling ``project_meta.json`` in ``_finalize_skill_run``).
        out_dir = Path(output_dir).expanduser()
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

    # One fresh ownership gate serves sync, async, CLI, agent, Desktop, remote,
    # pipeline leaves, and candidate-plan leaves.  A durable exclusive marker
    # prevents concurrent adoption and makes stale result envelopes impossible
    # to confuse with evidence from this execution.
    try:
        out_dir = claim_fresh_output_directory(out_dir, owner=f"skill:{skill_name}")
    except OutputDirectoryClaimError as exc:
        revision = registry_skill_revision or {}
        return _err(
            skill_name,
            str(exc),
            skill_info=skill_info,
            skills_root=skills_root,
            evidence_kind="demo" if demo else "ordinary",
            skill_hash=revision.get("manifest_hash", "unknown"),
            source_hash=revision.get("source_hash", "unknown"),
            runtime_entry_path=script_path,
            environment_id="unknown",
        )

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
                f"`{skill_name}` does not support --demo (consensus skills run on "
                "real preprocessed data via the consensus runtime); provide "
                "--input <preprocessed.h5ad>.",
                skill_info=skill_info,
                skills_root=skills_root,
            )
        return _err(
            skill_name,
            "No --input, --demo, or --session provided.",
            skill_info=skill_info,
            skills_root=skills_root,
        )

    if log_banner:
        domain = skill_info.get("domain", "unknown")
        domain_display = snapshot.domains.get(domain, {}).get("name", domain.title())
        if demo:
            mode_str = f"{CYAN}demo mode{RESET}"
        elif resolved_input_paths:
            mode_str = f"inputs: {', '.join(resolved_input_paths)}"
        else:
            mode_str = f"input: {resolved_input}"
        print(
            f"\n{BOLD}Running {domain_display} skill:{RESET} {GREEN}{skill_name}{RESET} ({mode_str})"
        )
        print(f"{BOLD}Output:{RESET} {out_dir}\n")

    cmd.extend(filtered)

    env = scrub_internal_control_credentials(os.environ.copy())
    inherited_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(OMICSCLAW_DIR)
    if inherited_pythonpath:
        env["PYTHONPATH"] += os.pathsep + inherited_pythonpath
    # Isolate skill subprocesses from the user-site (``~/.local``) so a broken
    # or ABI-mismatched package there cannot shadow the analysis env's deps
    # (e.g. a stale ``~/.local`` torch breaking CellCharter). Operators can opt
    # out by exporting ``PYTHONNOUSERSITE=0`` before launch.
    env.setdefault("PYTHONNOUSERSITE", "1")

    # Candidate-plan resource leases may narrow GPU visibility, cap common
    # BLAS/OpenMP thread pools, and select per-step temporary storage. Accept
    # only this governed subset; arbitrary environment injection is not part of
    # the runner Interface.
    trusted_temp: Path | None = None
    if trusted_resource_temp_dir is not None:
        candidate = Path(trusted_resource_temp_dir).expanduser()
        run_root = out_dir.resolve(strict=True).parent
        if (
            out_dir.name != "artifacts"
            or candidate.name != ".tmp"
            or candidate.parent.resolve(strict=True) != run_root
        ):
            raise ValueError("trusted resource temp directory is outside the Run root")
        trusted_temp = candidate.resolve(strict=False)

    for key, raw_value in (resource_env or {}).items():
        if key not in _RESOURCE_ENV:
            continue
        value = str(raw_value)
        if key in _RESOURCE_THREAD_ENV:
            if not value.isdigit() or int(value) < 1:
                continue
        elif key == "CUDA_VISIBLE_DEVICES":
            if value and any(
                not re.fullmatch(r"[A-Za-z0-9_.:/-]+", token)
                for token in value.split(",")
            ):
                continue
        elif key == "TMPDIR":
            temp_dir = Path(value).expanduser().resolve()
            try:
                temp_dir.relative_to(out_dir)
            except ValueError:
                if trusted_temp is None or temp_dir != trusted_temp:
                    continue
            temp_dir.mkdir(parents=True, exist_ok=True)
            value = str(temp_dir)
        env[key] = value

    # Adaptive environment resolution is Python-only. Resource governance must
    # already be present because its probes and any selected runtime inherit
    # this exact environment. Canonical V1 Run execution disables adaptive
    # provisioning entirely: a resource-ready Skill runs in the bound base
    # runtime rather than starting unowned setup/install subprocesses.
    runtime_language = (
        str(skill_info.get("runtime_language") or "python").strip().lower()
    )
    runtime_source = (
        "base" if runtime_language == "python" else f"base/{runtime_language}"
    )
    if runtime_language == "python" and allow_adaptive_environment:
        try:
            runtime = resolve_skill_runtime(
                skill_info,
                method=requested_method,
                base_python=cmd[0],
                base_env=env,
                cwd=str(script_path.parent),
                status_cb=status_callback,
            )
            runtime_source = runtime.source
            if runtime.python != cmd[0]:
                cmd[0] = runtime.python
            if runtime.env_overlay:
                env.update(runtime.env_overlay)
        except Exception:  # pragma: no cover - resolver already degrades safely
            pass

    from .evolution import capture_skill_execution_identity, compute_environment_id

    if (
        skills_root is None
    ):  # pragma: no cover - a loaded Registry always binds its root
        raise RuntimeError("loaded Skill Registry has no canonical skills root")
    if registry_skill_revision is not None:
        skill_hash = registry_skill_revision["manifest_hash"]
        source_hash = registry_skill_revision["source_hash"]
    else:
        skill_hash, source_hash = capture_skill_execution_identity(
            script_path,
            skills_root=skills_root,
            directory_name=(
                str(skill_info.get("directory_name") or "") if requires_manifest else ""
            ),
        )
    try:
        environment_id = compute_environment_id(
            skill_info,
            runtime_source=runtime_source,
            runtime_executable=cmd[0],
            runtime_env=env,
            runtime_cwd=script_path.parent,
            runtime_language=runtime_language,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _err(
            skill_name,
            f"Skill runtime evidence probe failed: {type(exc).__name__}.",
            runtime_source=runtime_source,
            skill_info=skill_info,
            skills_root=skills_root,
            evidence_kind="demo" if demo else "ordinary",
            skill_hash=skill_hash,
            source_hash=source_hash,
            runtime_entry_path=script_path,
            environment_id="unknown",
            error_kind="contract_validator_failed",
        )
    prepared = _PreparedSkillRun(
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
        skills_root=skills_root,
        skill_hash=skill_hash,
        source_hash=source_hash,
        environment_id=environment_id,
        requires_manifest=requires_manifest,
        runtime_source=runtime_source,
    )
    try:
        bind_output_claim_audit_identity(
            prepared.out_dir,
            owner=f"skill:{prepared.skill_name}",
            audit_identity=_prepared_audit_identity(prepared),
            runtime_source=prepared.runtime_source,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _prepared_framework_failure(
            prepared,
            duration=0.0,
            message=f"Skill run claim audit binding failed: {type(exc).__name__}.",
        )
    return prepared


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
    if not _prepared_revision_is_current(prepared):
        return _prepared_framework_failure(
            prepared,
            duration=duration,
            message=_PROVENANCE_DRIFT_MESSAGE,
        )

    final_out_dir = prepared.out_dir
    actual_method = prepared.requested_method
    readme_path = ""
    notebook_path = ""
    if proc.returncode == 0:
        # Process success is necessary but insufficient.  Verify the subset of
        # ``skill.yaml`` outputs that are true guarantees before generating a
        # success README/notebook or marking the Project Run completed.
        contract_error_kind = "contract_failure"
        try:
            contract_report = verify_skill_run_outputs(
                prepared.skill_info,
                prepared.out_dir,
                requested_method=prepared.requested_method,
                runtime_python=(
                    prepared.cmd[0]
                    if str(prepared.skill_info.get("runtime_language") or "python")
                    .strip()
                    .lower()
                    == "python"
                    else get_skill_runner_python()
                ),
                runtime_env=_anndata_validation_env(
                    prepared.env,
                    runtime_cwd=prepared.script_path.parent,
                ),
                runtime_cwd=prepared.script_path.parent,
            )
        except Exception as exc:  # malformed registry state must fail closed
            contract_error_kind = "contract_validator_failed"
            contract_failure = (
                "Skill output contract failed:\n"
                "- [contract_validator_failed] output verification could not complete: "
                f"{type(exc).__name__}"
            )
            contract_method = prepared.requested_method
        else:
            contract_failure = contract_report.format_failure()
            contract_method = contract_report.actual_method

        if contract_failure:
            if not _prepared_revision_is_current(prepared):
                return _prepared_framework_failure(
                    prepared,
                    duration=duration,
                    message=_PROVENANCE_DRIFT_MESSAGE,
                )
            output_files = (
                _visible_output_files(prepared.out_dir)
                if prepared.out_dir.exists()
                else []
            )
            stderr_parts = [
                part for part in (proc.stderr.strip(), contract_failure) if part
            ]
            result = build_skill_run_result(
                skill=prepared.skill_name,
                success=False,
                exit_code=1,
                output_dir=prepared.out_dir,
                files=output_files,
                stdout=proc.stdout,
                stderr="\n".join(stderr_parts),
                duration_seconds=duration,
                method=contract_method,
                runtime_source=prepared.runtime_source,
                error_kind=contract_error_kind,
                audit_identity=_prepared_audit_identity(prepared),
            )
            _record_skill_run_event(
                result,
                skill_info=prepared.skill_info,
                evidence_kind="demo" if prepared.demo else "ordinary",
                skill_hash=prepared.skill_hash,
                source_hash=prepared.source_hash,
                runtime_entry_path=prepared.script_path,
                environment_id=prepared.environment_id,
            )
            return result

        user_command = build_user_run_command(
            skill_name=prepared.skill_name,
            demo=prepared.demo,
            input_path=prepared.resolved_input,
            output_dir=prepared.out_dir,
            forwarded_args=prepared.filtered_extra_args,
        )
        try:
            (
                final_out_dir,
                actual_method,
                readme_path,
                notebook_path,
                _,
            ) = finalize_output_directory(
                prepared.out_dir,
                skill_name=prepared.skill_name,
                skill_info=prepared.skill_info,
                timestamp=prepared.generated_ts,
                user_supplied_output_dir=prepared.user_supplied_output_dir,
                preferred_method=prepared.requested_method,
                actual_command=user_command,
            )
        except Exception as exc:
            if not _prepared_revision_is_current(prepared):
                return _prepared_framework_failure(
                    prepared,
                    duration=duration,
                    message=_PROVENANCE_DRIFT_MESSAGE,
                )
            return _prepared_framework_failure(
                prepared,
                duration=duration,
                message=f"Skill output finalization failed: {type(exc).__name__}.",
            )
        if not _prepared_revision_is_current(prepared):
            return _prepared_framework_failure(
                prepared,
                duration=duration,
                message=_PROVENANCE_DRIFT_MESSAGE,
            )
        # ADR 0035: record the Run in its Project (enrich manifest.json + append
        # the rebuildable index.jsonl). Gated on a sibling ``project_meta.json`` so
        # resolver-managed runs (CLI default + agent + channel) are indexed while an
        # explicit ``--output`` outside the output root is left untouched. Never let
        # bookkeeping failure break a successful run.
        try:
            from omicsclaw.common import run_paths

            project_meta = run_paths.read_project_meta(final_out_dir.parent)
            project_id = project_meta.get("project_id")
            if isinstance(project_id, str) and project_id.strip():
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
    elif not _prepared_revision_is_current(prepared):
        return _prepared_framework_failure(
            prepared,
            duration=duration,
            message=_PROVENANCE_DRIFT_MESSAGE,
        )

    output_files = (
        _visible_output_files(final_out_dir) if final_out_dir.exists() else []
    )

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
        runtime_source=prepared.runtime_source,
        audit_identity=_prepared_audit_identity(prepared),
    )
    _record_skill_run_event(
        result,
        skill_info=prepared.skill_info,
        evidence_kind="demo" if prepared.demo else "ordinary",
        skill_hash=prepared.skill_hash,
        source_hash=prepared.source_hash,
        runtime_entry_path=prepared.script_path,
        environment_id=prepared.environment_id,
    )

    if prepared.session_path and result.success:
        _store_result_in_session(
            prepared.session_path, prepared.skill_name, final_out_dir
        )

    return result


def resolve_skill_alias(
    skill_name: str,
    *,
    registry_snapshot: RegistrySnapshot | None = None,
) -> str:
    """Resolve a user-facing skill name or legacy alias to its canonical alias."""
    skills = (registry_snapshot or ensure_registry_loaded().snapshot()).skills
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
    status_callback: Callable[[str], None] | None = None,
) -> SkillRunResult:
    """Run one Skill through the public shared-runner contract."""
    return _run_skill_bound(
        skill_name,
        input_path=input_path,
        input_paths=input_paths,
        output_dir=output_dir,
        demo=demo,
        session_path=session_path,
        extra_args=extra_args,
        project_id=project_id,
        project_name=project_name,
        stdout_callback=stdout_callback,
        stderr_callback=stderr_callback,
        cancel_event=cancel_event,
        status_callback=status_callback,
    )


def _run_skill_bound(
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
    status_callback: Callable[[str], None] | None = None,
    _registry_snapshot: RegistrySnapshot | None = None,
    _expected_skill_revision: Mapping[str, str] | None = None,
) -> SkillRunResult:
    """Run a Skill against optional internal frozen-authority bindings.

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
    registry_snapshot = _registry_snapshot or ensure_registry_loaded().snapshot()
    skill_name = resolve_skill_alias(
        skill_name,
        registry_snapshot=registry_snapshot,
    )

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
            extra_args=extra_args,
            registry_snapshot=registry_snapshot,
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
        resource_env=None,
        trusted_resource_temp_dir=None,
        project_id=project_id,
        project_name=project_name,
        status_callback=status_callback,
        registry_snapshot=registry_snapshot,
        expected_skill_revision=_expected_skill_revision,
    )
    if isinstance(prepared, SkillRunResult):
        return prepared

    if _expected_skill_revision is not None and _prepared_skill_revision(
        prepared
    ) != dict(_expected_skill_revision):
        return _prepared_framework_failure(
            prepared,
            duration=0.0,
            message="Skill execution provenance does not match the bound authority.",
        )

    if not _prepared_revision_is_current(prepared):
        return _prepared_framework_failure(
            prepared,
            duration=0.0,
            message=_PROVENANCE_DRIFT_MESSAGE,
        )

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
    except FileNotFoundError:
        duration = time.time() - t0
        if not _prepared_revision_is_current(prepared):
            return _prepared_framework_failure(
                prepared,
                duration=duration,
                message=_PROVENANCE_DRIFT_MESSAGE,
            )
        return _prepared_missing_runtime_failure(prepared, duration=duration)
    except ProcessTreeStopUnconfirmed:
        # This is an ownership/integrity outcome, not an ordinary Skill
        # failure. The Run Runtime must quarantine the Lease and Dispatcher.
        raise
    except Exception as exc:
        duration = time.time() - t0
        if not _prepared_revision_is_current(prepared):
            return _prepared_framework_failure(
                prepared,
                duration=duration,
                message=_PROVENANCE_DRIFT_MESSAGE,
            )
        return _err(
            skill_name,
            str(exc),
            duration=duration,
            runtime_source=prepared.runtime_source,
            skill_info=prepared.skill_info,
            evidence_kind="demo" if prepared.demo else "ordinary",
            skill_hash=prepared.skill_hash,
            source_hash=prepared.source_hash,
            runtime_entry_path=prepared.script_path,
            environment_id=prepared.environment_id,
        )

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
    resource_env: Mapping[str, str] | None = None,
    project_id: str = "",
    project_name: str = "",
    status_callback: Callable[[str], None] | None = None,
    _registry_snapshot: RegistrySnapshot | None = None,
    _expected_skill_revision: Mapping[str, str] | None = None,
    _trusted_resource_temp_dir: str | None = None,
    _allow_adaptive_environment: bool = True,
    _require_process_tree_proof: bool = False,
    _governed_execution_reference: str | None = None,
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
    registry_snapshot = _registry_snapshot or ensure_registry_loaded().snapshot()
    skill_name = resolve_skill_alias(
        skill_name,
        registry_snapshot=registry_snapshot,
    )

    # ``_prepare_skill_run`` is synchronous and now runs the adaptive-env probe
    # (and, in Phase 2, venv provisioning), which can take seconds. Offload it to a
    # worker thread so the desktop/jobs event loop is never blocked before the
    # first await (Codex Phase 1 review).
    prepare_task = asyncio.create_task(
        asyncio.to_thread(
            _prepare_skill_run,
            skill_name,
            input_path=input_path,
            input_paths=input_paths,
            output_dir=output_dir,
            demo=demo,
            session_path=session_path,
            extra_args=extra_args,
            resource_env=resource_env,
            trusted_resource_temp_dir=_trusted_resource_temp_dir,
            allow_adaptive_environment=_allow_adaptive_environment,
            project_id=project_id,
            project_name=project_name,
            log_banner=False,
            status_callback=status_callback,
            registry_snapshot=registry_snapshot,
            expected_skill_revision=_expected_skill_revision,
        ),
        name=f"omicsclaw-skill-prepare-{skill_name}",
    )
    try:
        prepared = await asyncio.shield(prepare_task)
    except asyncio.CancelledError:
        # Cancelling ``to_thread`` only detaches the awaiter. The adaptive
        # runtime probe and output claim may still be live, so wait until that
        # preparation thread has actually stopped before releasing a caller's
        # Resource Lease or acknowledging cancellation.
        while not prepare_task.done():
            try:
                await asyncio.shield(prepare_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if prepare_task.done() and not prepare_task.cancelled():
            try:
                canceled_prepared = prepare_task.result()
            except Exception:
                canceled_prepared = None
            if isinstance(canceled_prepared, _PreparedSkillRun):
                _record_prepared_cancellation(canceled_prepared, duration=0.0)
        raise
    if isinstance(prepared, SkillRunResult):
        return prepared
    if _expected_skill_revision is not None and _prepared_skill_revision(
        prepared
    ) != dict(_expected_skill_revision):
        return _prepared_framework_failure(
            prepared,
            duration=0.0,
            message="Skill execution provenance does not match the bound authority.",
        )
    if not _prepared_revision_is_current(prepared):
        return _prepared_framework_failure(
            prepared,
            duration=0.0,
            message=_PROVENANCE_DRIFT_MESSAGE,
        )

    t0 = time.time()
    try:
        proc = await adrive_subprocess(
            prepared.cmd,
            cwd=prepared.script_path.parent,
            env=prepared.env,
            out_dir=prepared.out_dir,
            require_process_tree_proof=_require_process_tree_proof,
            governed_execution_reference=_governed_execution_reference,
        )
    except asyncio.CancelledError:
        # Re-raise so the awaiting task sees the cancellation. The driver
        # already SIGTERM/SIGKILL'd the process group on its way out, so
        # no child is left behind.
        _record_prepared_cancellation(
            prepared,
            duration=time.time() - t0,
        )
        raise
    except FileNotFoundError:
        duration = time.time() - t0
        if not _prepared_revision_is_current(prepared):
            return _prepared_framework_failure(
                prepared,
                duration=duration,
                message=_PROVENANCE_DRIFT_MESSAGE,
            )
        return _prepared_missing_runtime_failure(prepared, duration=duration)
    except ProcessTreeStopUnconfirmed:
        # This is an ownership/integrity outcome, not an ordinary Skill
        # failure. The Run Runtime must quarantine the Lease and Dispatcher.
        raise
    except Exception as exc:
        duration = time.time() - t0
        if not _prepared_revision_is_current(prepared):
            return _prepared_framework_failure(
                prepared,
                duration=duration,
                message=_PROVENANCE_DRIFT_MESSAGE,
            )
        return _err(
            skill_name,
            str(exc),
            duration=duration,
            runtime_source=prepared.runtime_source,
            skill_info=prepared.skill_info,
            evidence_kind="demo" if prepared.demo else "ordinary",
            skill_hash=prepared.skill_hash,
            source_hash=prepared.source_hash,
            runtime_entry_path=prepared.script_path,
            environment_id=prepared.environment_id,
        )

    duration = time.time() - t0
    finalize_task = asyncio.create_task(
        asyncio.to_thread(_finalize_skill_run, prepared, proc, duration),
        name=f"omicsclaw-skill-finalize-{skill_name}",
    )
    caller_canceled = False
    while True:
        try:
            result = await asyncio.shield(finalize_task)
            break
        except asyncio.CancelledError:
            if finalize_task.done():
                result = finalize_task.result()
                caller_canceled = True
                break
            caller_canceled = True
    if caller_canceled:
        raise asyncio.CancelledError
    return result


def _store_result_in_session(session_path: str, skill_name: str, out_dir: Path) -> None:
    """Store skill result back into the session JSON."""
    try:
        from omicsclaw.common.session import SpatialSession

        result_json = out_dir / "result.json"
        claim_identities = collect_output_claim_identities(out_dir)
        if not is_scientific_output_file(
            result_json,
            output_root=out_dir,
            claim_identities=claim_identities,
        ):
            return
        session = SpatialSession.load(session_path)
        result_data = json.loads(result_json.read_text(encoding="utf-8"))
        session.add_skill_result(skill_name, result_data, output_dir=str(out_dir))

        processed = out_dir / "processed.h5ad"
        if is_scientific_output_file(
            processed,
            output_root=out_dir,
            claim_identities=claim_identities,
        ):
            session.primary_data_path = str(processed)
            session.mark_step(skill_name)

        session.save(session_path)
    except Exception:
        pass


def _err(
    skill: str,
    msg: str,
    duration: float = 0,
    runtime_source: str = "base",
    *,
    skill_info: Mapping[str, Any] | None = None,
    skills_root: str | Path | None = None,
    evidence_kind: str = "ordinary",
    skill_hash: str | None = None,
    source_hash: str | None = None,
    runtime_entry_path: str | Path | None = None,
    environment_id: str | None = None,
    error_kind: str = "",
) -> SkillRunResult:
    result = build_skill_run_result(
        skill=skill,
        success=False,
        exit_code=-1,
        output_dir=None,
        stderr=msg,
        duration_seconds=duration,
        runtime_source=runtime_source,
        error_kind=error_kind,
        audit_identity=_result_audit_identity(
            skill=skill,
            skill_info=skill_info,
            skill_hash=skill_hash,
            source_hash=source_hash,
            environment_id=environment_id,
        ),
    )
    _record_skill_run_event(
        result,
        skill_info=skill_info,
        skills_root=skills_root,
        evidence_kind=evidence_kind,
        skill_hash=skill_hash,
        source_hash=source_hash,
        runtime_entry_path=runtime_entry_path,
        environment_id=environment_id,
    )
    return result


def _record_skill_run_event(
    result: SkillRunResult,
    *,
    skill_info: Mapping[str, Any] | None = None,
    skills_root: str | Path | None = None,
    evidence_kind: str = "ordinary",
    skill_hash: str | None = None,
    source_hash: str | None = None,
    runtime_entry_path: str | Path | None = None,
    environment_id: str | None = None,
) -> None:
    """Best-effort audit persistence must never change execution semantics."""
    try:
        from .evolution import record_skill_run_result

        record_skill_run_result(
            result,
            skill_info=skill_info,
            skills_root=skills_root,
            evidence_kind=evidence_kind,
            skill_hash=skill_hash,
            source_hash=source_hash,
            runtime_entry_path=runtime_entry_path,
            environment_id=environment_id,
        )
    except Exception as exc:  # pragma: no cover - filesystem/audit degradation
        logger.warning("failed to record skill health event: %s", exc)


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "OMICSCLAW_DIR",
    "PYTHON",
    "SPATIAL_PIPELINE",
    "resolve_skill_alias",
    "run_skill",
]
