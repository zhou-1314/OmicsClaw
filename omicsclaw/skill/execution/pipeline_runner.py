"""Run a multi-step skill chain described by a ``PipelineConfig``.

OMI-12 P2.7: the spatial chain used to be a hard-coded
``SPATIAL_PIPELINE = [...]`` list with a dedicated ``_run_spatial_pipeline``
function. New pipelines now live as ``pipelines/<name>.yaml`` files (see
``pipeline_config``); this module is the generic runner that consumes one.

For backward compatibility ``SPATIAL_PIPELINE`` is still exposed — it's
derived from ``pipelines/spatial-pipeline.yaml`` so a config edit is the
single source of truth.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from omicsclaw.common.output_claim import atomic_write_owned_output_text

from ..preconditions import format_precondition_failure, preflight_skill_execution
from ..registry import RegistrySnapshot, is_skill_automatically_routable
from ..result import SkillRunAuditIdentity, SkillRunResult, build_skill_run_result

from .output_finalize import write_pipeline_readme
from .output_ownership import (
    OutputDirectoryClaimError,
    claim_fresh_output_directory,
    collect_output_claim_identities,
    is_scientific_output_file,
)
from .pipeline_config import (
    PipelineConfig,
    PipelineConfigError,
    load_pipeline_config,
    validate_pipeline_config,
)


_REVISION_FIELDS = (
    "skill_id",
    "skill_version",
    "manifest_hash",
    "source_hash",
)


def _load_spatial_pipeline_skill_names() -> list[str]:
    """Resolve the spatial chain at import time, falling back to a stable list.

    The YAML is the source of truth — but we don't want a missing file
    (e.g. an incomplete install or a wheel that didn't ship ``pipelines/``)
    to break callers that just want the constant. The fallback mirrors
    the historical hard-coded order so behaviour stays unchanged.
    """
    try:
        config = load_pipeline_config("spatial-pipeline")
    except PipelineConfigError:
        config = None
    if config is None:
        return [
            "spatial-preprocess",
            "spatial-domains",
            "spatial-de",
            "spatial-genes",
            "spatial-statistics",
        ]
    return list(config.skill_names)


SPATIAL_PIPELINE: list[str] = _load_spatial_pipeline_skill_names()


def _invalid_pipeline_step(
    config: PipelineConfig,
    registry_snapshot: RegistrySnapshot,
) -> str | None:
    """Return the first step not bound to a canonical routable Skill."""
    for step in config.skill_names:
        info = registry_snapshot.skills.get(step)
        if (
            not isinstance(info, Mapping)
            or str(info.get("alias") or "") != step
            or not is_skill_automatically_routable(info)
        ):
            return step
    return None


def _normalize_bound_revisions(
    revisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Return the deterministic, JSON-safe execution authority projection."""
    return {
        skill: {
            field: str(revisions[skill][field])
            for field in _REVISION_FIELDS
        }
        for skill in sorted(revisions)
    }


def _pipeline_authority_digest(
    config: PipelineConfig,
    revisions: Mapping[str, Mapping[str, str]],
) -> str:
    payload = {
        "pipeline": config.name,
        "chain_output_basename": config.chain_output_basename,
        "steps": list(config.skill_names),
        "skill_revisions": revisions,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _audit_identity_matches_revision(
    identity: SkillRunAuditIdentity | None,
    revision: Mapping[str, str],
) -> bool:
    if identity is None:
        return False
    return (
        identity.skill_id == revision.get("skill_id")
        and identity.skill_version == revision.get("skill_version")
        and identity.skill_hash == revision.get("manifest_hash")
        and identity.source_hash == revision.get("source_hash")
    )


def _unsafe_existing_step_output(
    out_dir: Path,
    skill_names: list[str],
) -> str | None:
    """Reject stale or ambiguous step targets before the first subprocess."""
    for skill_name in skill_names:
        skill_out = out_dir / skill_name
        if skill_out.is_symlink():
            return f"Pipeline step output '{skill_out}' is a symbolic link."
        if not skill_out.exists():
            continue
        if not skill_out.is_dir():
            return f"Pipeline step output '{skill_out}' already exists and is not a directory."
        try:
            next(skill_out.iterdir())
        except StopIteration:
            return (
                f"Pipeline step output '{skill_out}' already exists; "
                "pipeline steps require fresh output directories."
            )
        except OSError as exc:
            return f"Pipeline step output '{skill_out}' cannot be inspected: {exc}."
        return f"Pipeline step output '{skill_out}' already exists and is not empty."
    return None


def run_pipeline(
    config: PipelineConfig,
    *,
    default_output_root: Path,
    err_factory,
    input_path: str | None = None,
    output_dir: str | None = None,
    demo: bool = False,
    session_path: str | None = None,
    registry_snapshot: RegistrySnapshot | None = None,
    project_id: str = "",
    project_name: str = "",
) -> SkillRunResult:
    """Run an arbitrary skill chain described by ``config`` end-to-end.

    ``err_factory`` is the runner's ``_err`` helper, injected to avoid an
    import cycle. ``default_output_root`` is also injected so tests that
    monkeypatch ``skill_runner.DEFAULT_OUTPUT_ROOT`` for the regular
    ``run_skill`` path do not need to learn about this module.

    Returns a ``SkillRunResult`` natively (OMI-12 P1.6); callers that need
    the legacy dict shape should call ``.to_legacy_dict()`` themselves.
    """
    config_name = str(getattr(config, "name", "pipeline") or "pipeline")
    try:
        validate_pipeline_config(config)
    except PipelineConfigError as exc:
        return err_factory(config_name, f"Pipeline config invalid: {exc}")

    if not input_path and not session_path and not demo:
        return err_factory(config.name, "Requires --input, --demo, or --session.")

    # Late import keeps this module a leaf in the dependency DAG: skill_runner
    # imports pipeline_runner, not the other way around.
    from ..runner import _run_skill_bound, ensure_registry_loaded

    bound_snapshot = registry_snapshot or ensure_registry_loaded().snapshot()
    invalid_step = _invalid_pipeline_step(config, bound_snapshot)
    if invalid_step is not None:
        return err_factory(
            config.name,
            f"Pipeline '{config.name}' references unknown, noncanonical, or "
            f"non-routable Skill '{invalid_step}'.",
        )
    try:
        expected_revisions = _normalize_bound_revisions(
            bound_snapshot.skill_revisions(list(config.skill_names))
        )
    except (KeyError, TypeError, ValueError, RuntimeError):
        return err_factory(
            config.name,
            "Pipeline Skill authority no longer matches its initial Registry "
            "snapshot; reload before execution.",
        )

    first_assessment = preflight_skill_execution(
        config.skill_names[0],
        input_path=input_path,
        demo=demo,
        session_path=session_path,
        registry=bound_snapshot,
    )
    if first_assessment is not None and not first_assessment.execution_ready:
        return err_factory(
            config.name,
            format_precondition_failure(first_assessment),
        )

    if output_dir:
        out_dir = Path(output_dir)
    else:
        # ADR 0035: the pipeline run is a composite Run placed under its Project.
        # Step subdirs use explicit output paths (out_dir/<skill>), so they nest
        # inside this run rather than registering as top-level Runs.
        from omicsclaw.common import run_paths

        out_dir = run_paths.resolve_run_dir(
            output_root=default_output_root,
            skill=config.name,
            project_id=project_id,
            project_name=project_name,
            input_path=input_path,
            demo=demo,
        ).run_dir

    try:
        out_dir = claim_fresh_output_directory(
            out_dir,
            owner=f"pipeline:{config.name}",
        )
    except OutputDirectoryClaimError as exc:
        return err_factory(config.name, str(exc))

    skill_names = list(config.skill_names)
    unsafe_step_output = _unsafe_existing_step_output(out_dir, skill_names)
    if unsafe_step_output is not None:
        return err_factory(config.name, unsafe_step_output)

    all_results: dict[str, Any] = {}
    current_input = input_path
    chain_basename = config.chain_output_basename
    failure_stderr = ""
    authority_digest = _pipeline_authority_digest(config, expected_revisions)

    for step_index, skill_name in enumerate(skill_names):
        skill_out = out_dir / skill_name
        print(f"  Running {skill_name}...")
        result = _run_skill_bound(
            skill_name=skill_name,
            input_path=current_input,
            output_dir=str(skill_out),
            demo=demo and current_input is None,
            session_path=session_path,
            _registry_snapshot=bound_snapshot,
            _expected_skill_revision=expected_revisions.get(skill_name),
        )
        audit_identity = (
            result.audit_identity.to_dict()
            if result.audit_identity is not None
            else None
        )
        all_results[skill_name] = {
            "success": result.success,
            "duration": result.duration_seconds,
            "method": result.method,
            "output_dir": result.output_dir or "",
            "readme_path": result.readme_path,
            "notebook_path": result.notebook_path,
            "stderr": result.stderr if not result.success else "",
            "audit_identity": audit_identity,
        }
        if result.success and not _audit_identity_matches_revision(
            result.audit_identity,
            expected_revisions[skill_name],
        ):
            failure_stderr = (
                f"Skill '{skill_name}' reported success without a bound audit "
                "identity matching the pipeline's initial Registry revision."
            )
            all_results[skill_name]["success"] = False
            all_results[skill_name]["stderr"] = failure_stderr
            print(f"FAILED: {skill_name}")
            break
        if not result.success:
            failure_stderr = result.stderr
            print(f"FAILED: {skill_name}")
            if result.stderr:
                print(f"    {result.stderr[:200]}")
            break

        if step_index < len(skill_names) - 1:
            baton = skill_out / chain_basename
            try:
                baton_is_contained = (
                    baton.resolve(strict=False).parent
                    == skill_out.resolve(strict=True)
                )
            except (OSError, RuntimeError):
                baton_is_contained = False
            if (
                not baton_is_contained
                or baton.is_symlink()
                or not is_scientific_output_file(
                    baton,
                    output_root=skill_out,
                )
            ):
                failure_stderr = (
                    f"Skill '{skill_name}' reported success but is missing required "
                    f"chain output '{chain_basename}'."
                )
                all_results[skill_name]["success"] = False
                all_results[skill_name]["stderr"] = failure_stderr
                print(f"FAILED: {skill_name}")
                break
            current_input = str(baton)

    completed_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "schema_version": 1,
        "pipeline": skill_names,
        "chain_output_basename": chain_basename,
        "bound_skill_revisions": expected_revisions,
        "pipeline_authority_digest": authority_digest,
        "results": all_results,
        "completed_at": completed_at,
    }
    summary_path = out_dir / "pipeline_summary.json"
    try:
        atomic_write_owned_output_text(
            summary_path,
            output_root=out_dir,
            text=json.dumps(summary, indent=2, default=str) + "\n",
            label="pipeline summary",
        )
        pipeline_readme = write_pipeline_readme(
            out_dir,
            pipeline_name=config.name,
            results=all_results,
            completed_at=completed_at,
        )
    except (OSError, RuntimeError) as exc:
        # A child Skill shares the same OS identity today and can attempt to
        # plant an alias in the composite parent.  Metadata is authoritative
        # only after the Backend-owned atomic publisher accepts the target.
        publication_error = f"Pipeline metadata publication failed: {exc}"
        return build_skill_run_result(
            skill=config.name,
            success=False,
            exit_code=1,
            output_dir=out_dir,
            files=[],
            stdout="",
            stderr=publication_error,
            duration_seconds=sum(
                result["duration"] for result in all_results.values()
            ),
            readme_path="",
            notebook_path="",
        )

    succeeded = sum(1 for result in all_results.values() if result["success"])
    # ADR 0035: record the composite pipeline run in its Project (manifest + index),
    # gated on a sibling project_meta.json so an explicit --output is left untouched.
    try:
        from omicsclaw.common import run_paths

        project_meta = run_paths.read_project_meta(out_dir.parent)
        project_id = project_meta.get("project_id")
        if isinstance(project_id, str) and project_id.strip():
            run_paths.finalize_run(
                out_dir,
                skill=config.name,
                status="completed" if succeeded == len(skill_names) else "failed",
                input_path=input_path,
                surface="pipeline",
            )
    except Exception:  # pragma: no cover - defensive
        pass
    claim_identities = collect_output_claim_identities(out_dir)
    return build_skill_run_result(
        skill=config.name,
        success=succeeded == len(skill_names),
        exit_code=0 if succeeded == len(skill_names) else 1,
        output_dir=out_dir,
        files=[
            path.name
            for path in out_dir.rglob("*")
            if is_scientific_output_file(
                path,
                output_root=out_dir,
                claim_identities=claim_identities,
            )
        ],
        stdout=f"Pipeline: {succeeded}/{len(skill_names)} skills succeeded.",
        stderr=failure_stderr,
        duration_seconds=sum(result["duration"] for result in all_results.values()),
        readme_path=pipeline_readme,
        notebook_path="",
    )


def run_spatial_pipeline(
    *,
    default_output_root: Path,
    err_factory,
    input_path: str | None = None,
    output_dir: str | None = None,
    demo: bool = False,
    session_path: str | None = None,
    project_id: str = "",
    project_name: str = "",
) -> SkillRunResult:
    """Thin wrapper preserved for backward compatibility.

    Loads ``pipelines/spatial-pipeline.yaml`` and delegates to
    :func:`run_pipeline`. New callers should use :func:`run_pipeline_by_name`
    or :func:`run_pipeline` directly.
    """
    try:
        config = load_pipeline_config("spatial-pipeline")
    except PipelineConfigError as exc:
        return err_factory("spatial-pipeline", f"Pipeline config invalid: {exc}")
    if config is None:
        return err_factory(
            "spatial-pipeline",
            "pipelines/spatial-pipeline.yaml not found",
        )
    from ..runner import ensure_registry_loaded

    registry_snapshot = ensure_registry_loaded().snapshot()
    invalid_step = _invalid_pipeline_step(config, registry_snapshot)
    if invalid_step is not None:
        return err_factory(
            config.name,
            f"Pipeline '{config.name}' references unknown, noncanonical, or "
            f"non-routable Skill '{invalid_step}'.",
        )
    return run_pipeline(
        config,
        default_output_root=default_output_root,
        err_factory=err_factory,
        input_path=input_path,
        output_dir=output_dir,
        demo=demo,
        session_path=session_path,
        registry_snapshot=registry_snapshot,
        project_id=project_id,
        project_name=project_name,
    )


def run_pipeline_by_name(
    name: str,
    *,
    default_output_root: Path,
    err_factory,
    input_path: str | None = None,
    output_dir: str | None = None,
    demo: bool = False,
    session_path: str | None = None,
    extra_args: list[str] | None = None,
    registry_snapshot: RegistrySnapshot | None = None,
    project_id: str = "",
    project_name: str = "",
) -> SkillRunResult | None:
    """Load ``pipelines/<name>.yaml`` and run it; return ``None`` when absent.

    ``None`` lets the dispatcher fall through to the regular skill registry
    lookup instead of fabricating an error result the caller didn't ask for
    — a missing YAML for a name ending in ``-pipeline`` is just "no pipeline
    by that alias", not "the user's request was broken".
    """
    try:
        config = load_pipeline_config(name)
    except PipelineConfigError as exc:
        return err_factory(name, f"Pipeline config invalid: {exc}")
    if config is None:
        return None
    if extra_args:
        # Pipeline YAML currently has no typed pipeline-level argument schema
        # or per-step binding.  Silently dropping a caller's scientific flags
        # would execute a different request; broadcasting them to every step
        # would be equally unsafe.  Fail closed before preflight/output work.
        return err_factory(
            name,
            f"Pipeline '{name}' does not accept forwarded arguments; "
            "declare typed pipeline-to-step bindings before exposing them.",
        )
    if registry_snapshot is None:
        from ..runner import ensure_registry_loaded

        registry_snapshot = ensure_registry_loaded().snapshot()
    invalid_step = _invalid_pipeline_step(config, registry_snapshot)
    if invalid_step is not None:
        return err_factory(
            config.name,
            f"Pipeline '{config.name}' references unknown, noncanonical, or "
            f"non-routable Skill '{invalid_step}'.",
        )
    return run_pipeline(
        config,
        default_output_root=default_output_root,
        err_factory=err_factory,
        input_path=input_path,
        output_dir=output_dir,
        demo=demo,
        session_path=session_path,
        registry_snapshot=registry_snapshot,
        project_id=project_id,
        project_name=project_name,
    )
