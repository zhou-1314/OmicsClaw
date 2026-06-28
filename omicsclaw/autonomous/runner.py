"""Non-LLM foundation for autonomous code runner execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omicsclaw.common import run_paths
from omicsclaw.common.manifest import StepRecord
from omicsclaw.runtime.policy.verification import (
    ARTIFACT_KIND_DIR,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_COMPLETE,
    WORKSPACE_KIND_ANALYSIS_RUN,
    ArtifactRequirement,
    build_completion_report,
    update_workspace_manifest,
    write_completion_report,
)

from .contracts import (
    AUTONOMOUS_CODE_RUNNER_SOURCE,
    AUTONOMOUS_WORKSPACE_PURPOSE,
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
    AutonomousWorkspace,
    utcnow_iso,
)


AUTONOMOUS_CODE_RUNNER_VERSION = "0.1.0"


def autonomous_requirements() -> list[ArtifactRequirement]:
    """Artifact contract for mini-agent run workspaces — derived from ``run_layout``.

    The run-dir schema's deliverable entries (``run_layout.contract_entries``)
    mapped onto the verification contract. Only ``result_summary.md`` is required;
    everything else is optional, and the sentinel / provenance / rerun paths are
    excluded by the schema's roles. Because this list and ``create_workspace``'s
    eager set come from the *same* declaration, they can never drift apart (the
    clutter bug, where required dirs the engine never wrote shipped empty).
    """
    from . import run_layout

    requirements: list[ArtifactRequirement] = []
    for spec in run_layout.contract_entries():
        if spec.kind is run_layout.Kind.DIR:
            requirements.append(
                ArtifactRequirement(spec.key, spec.relpath, kind=ARTIFACT_KIND_DIR, required=spec.required)
            )
        else:
            requirements.append(
                ArtifactRequirement(spec.key, spec.relpath, required=spec.required)
            )
    return requirements


def write_run_records(
    workspace: AutonomousWorkspace,
    *,
    request: AutonomousRunRequest,
    result: AutonomousRunResult,
) -> tuple[Path, Path]:
    """Write manifest and completion report for an autonomous run."""
    summary_path = _write_result_summary(workspace, request=request, result=result)
    requirements = autonomous_requirements()
    metadata: dict[str, Any] = {
        "source": AUTONOMOUS_CODE_RUNNER_SOURCE,
        "goal": request.goal,
        "result_summary_path": str(summary_path),
        "run_id": workspace.run_id,
        "status": result.status.value,
        "attempts": [attempt.to_dict() for attempt in result.attempts],
        "input_paths": [str(item) for item in request.input_paths],
        "upstream_paths": [str(item) for item in request.upstream_paths],
        "language": request.language,
        "max_repair_attempts": request.max_repair_attempts,
        "model_override": request.model_override,
        "provider_override": request.provider_override,
        **dict(request.metadata),
        **dict(result.metadata),
    }
    manifest_path = update_workspace_manifest(
        workspace.root,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose=AUTONOMOUS_WORKSPACE_PURPOSE,
        requirements=requirements,
        step=StepRecord(
            skill=AUTONOMOUS_CODE_RUNNER_SOURCE,
            version=AUTONOMOUS_CODE_RUNNER_VERSION,
            input_file=";".join(str(item) for item in request.input_paths),
            output_file=str(workspace.root),
            params={
                "goal": request.goal,
                "run_id": workspace.run_id,
                "timeout_seconds": request.timeout_seconds,
                "language": request.language,
            },
        ),
        isolation_mode="workspace_dir",
        metadata=metadata,
    )
    completion_status = (
        COMPLETION_STATUS_COMPLETE
        if result.status == AutonomousRunStatus.SUCCEEDED
        else COMPLETION_STATUS_FAILED
    )
    report = build_completion_report(
        workspace.root,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose=AUTONOMOUS_WORKSPACE_PURPOSE,
        requirements=requirements,
        status=completion_status,
        errors=[result.error] if result.error else None,
        manifest_path=str(manifest_path),
        metadata=metadata,
        completed=result.status == AutonomousRunStatus.SUCCEEDED,
    )
    completion_report_path = write_completion_report(workspace.root, report)
    update_workspace_manifest(
        workspace.root,
        workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
        workspace_purpose=AUTONOMOUS_WORKSPACE_PURPOSE,
        requirements=requirements,
        completion_report=report,
        isolation_mode="workspace_dir",
        metadata=metadata,
        append_step=False,
    )
    if (workspace.root.parent / run_paths.PROJECT_META_FILENAME).exists():
        run_paths.finalize_run(
            workspace.root,
            skill="autonomous-code",
            status=(
                "completed"
                if result.status == AutonomousRunStatus.SUCCEEDED
                else "failed"
            ),
            method="mini-agent",
            dataset=run_paths.dataset_slug(
                input_paths=[str(item) for item in request.input_paths],
                dataset_label=str(metadata.get("dataset", "") or ""),
            ),
            surface=str(metadata.get("surface", "") or "autonomous"),
            input_path=(
                str(request.input_paths[0])
                if len(request.input_paths) == 1
                else None
            ),
        )
    # Write result.json LAST: the desktop /outputs reader treats it as the
    # completion marker (its mtime drives the freshness gate), so it must land
    # after the manifest / completion report / index finalization (codex review).
    _write_result_json(workspace, request=request, result=result)
    return manifest_path, completion_report_path


def _write_result_json(
    workspace: AutonomousWorkspace,
    *,
    request: AutonomousRunRequest,
    result: AutonomousRunResult,
) -> Path:
    """Emit the ``result.json`` completion envelope the desktop /outputs reader
    keys on (audit A-2).

    The autonomous runner natively writes ``completion_report.json`` /
    ``result_summary.md``, but the desktop output 看板 (``_read_result_json``)
    recognises a finished Run only by ``result.json``. Without this, every
    successful autonomous run was mis-reported running→failed and never linked
    back to its conversation. We map the autonomous status onto the same
    envelope shape skills use (``status`` / ``completed_at`` / ``summary`` /
    ``output_dir``) so autonomous runs are first-class in the outputs system.
    """
    status = "completed" if result.status == AutonomousRunStatus.SUCCEEDED else "failed"
    summary = (
        str(result.metadata.get("answer", "") or "").strip()
        or str(result.metadata.get("computed_results", "") or "").strip()
        or (request.goal or "").strip()
        or f"Autonomous run {status}"
    )
    payload: dict[str, Any] = {
        "skill": AUTONOMOUS_CODE_RUNNER_SOURCE,
        "status": status,
        "summary": summary[:500],
        "run_id": workspace.run_id,
        "output_dir": str(workspace.root),
        "completed_at": utcnow_iso(),
    }
    if result.error:
        payload["error"] = str(result.error)
    result_path = workspace.root / "result.json"
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return result_path


def _write_result_summary(
    workspace: AutonomousWorkspace,
    *,
    request: AutonomousRunRequest,
    result: AutonomousRunResult,
) -> Path:
    """Write a minimal summary for output-shape parity.

    The later LLM code loop will replace the body with computed and
    interpretive sections. The foundation still writes a useful summary so
    failed command-only runs are inspectable.
    """
    summary_path = workspace.paths.result_summary
    lines = [
        "# Autonomous Code Runner Summary",
        "",
        "## Goal",
        "",
        request.goal or "No goal provided.",
        "",
        "## Status",
        "",
        f"- Run id: `{workspace.run_id}`",
        f"- Status: `{result.status.value}`",
        f"- Attempts: `{len(result.attempts)}`",
    ]
    if result.error:
        lines.append(f"- Error: {result.error}")
    analysis_plan = str(result.metadata.get("analysis_plan", "") or "").strip()
    if analysis_plan:
        lines.extend(["", "## Analysis Plan", "", analysis_plan])
    if result.attempts:
        lines.extend(["", "## Attempts", ""])
        for attempt in result.attempts:
            lines.append(
                f"- Attempt {attempt.attempt_index}: `{attempt.status.value}`"
                f" / exit `{attempt.exit_code}` / tier `{attempt.permission_tier.value}`"
            )
            lines.append(f"  - stdout: `{attempt.stdout_log}`")
            lines.append(f"  - stderr: `{attempt.stderr_log}`")
            if attempt.approval_required:
                lines.append(
                    f"  - approval: `{'granted' if attempt.approval_granted else 'required'}"
                )
            if attempt.error:
                lines.append(f"  - error: {attempt.error}")
    computed_results = str(result.metadata.get("computed_results", "") or "").strip()
    interpretive_notes = str(result.metadata.get("interpretive_notes", "") or "").strip()
    if computed_results:
        lines.extend(["", "## Computed Results", "", computed_results])
    if interpretive_notes:
        lines.extend(["", "## Interpretive Notes", "", interpretive_notes])
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- OmicsClaw is a research and educational tool for multi-omics analysis. It is not a medical device and does not provide clinical diagnoses. Consult a domain expert before making decisions based on these results.",
        ]
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path
