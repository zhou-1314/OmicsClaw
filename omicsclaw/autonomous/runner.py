"""Non-LLM foundation for autonomous code runner execution."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

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
    AutonomousAttempt,
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
    AutonomousWorkspace,
    utcnow_iso,
)
from .executor import execute_command, execute_command_with_approval
from .workspace import create_workspace


AUTONOMOUS_CODE_RUNNER_VERSION = "0.1.0"


def autonomous_requirements() -> list[ArtifactRequirement]:
    """Artifact contract shared by autonomous code runner workspaces."""
    return [
        ArtifactRequirement("result_summary", "result_summary.md"),
        ArtifactRequirement("scripts", "scripts", kind=ARTIFACT_KIND_DIR),
        ArtifactRequirement("logs", "logs", kind=ARTIFACT_KIND_DIR),
        ArtifactRequirement("figures", "figures", kind=ARTIFACT_KIND_DIR, required=False),
        ArtifactRequirement("tables", "tables", kind=ARTIFACT_KIND_DIR, required=False),
        ArtifactRequirement("artifacts", "artifacts", kind=ARTIFACT_KIND_DIR, required=False),
        ArtifactRequirement("inputs", "inputs", kind=ARTIFACT_KIND_DIR, required=False),
        ArtifactRequirement("upstream", "upstream", kind=ARTIFACT_KIND_DIR, required=False),
    ]


def run_commands(
    request: AutonomousRunRequest,
    commands: Sequence[Sequence[str]],
) -> AutonomousRunResult:
    """Create a workspace, run commands, and write manifest/completion reports."""
    workspace = create_workspace(request)
    attempts: list[AutonomousAttempt] = []
    started_at = utcnow_iso()

    for index, argv in enumerate(commands):
        attempt = execute_command(
            workspace,
            [str(item) for item in argv],
            attempt_index=index,
            timeout_seconds=request.timeout_seconds,
        )
        attempts.append(attempt)
        if attempt.status != AutonomousRunStatus.SUCCEEDED:
            break

    status = _run_status_from_attempts(attempts)
    error = _first_error(attempts) or "No commands were provided."

    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=status,
        attempts=attempts,
        started_at=started_at,
        finished_at=utcnow_iso(),
        error=error if status != AutonomousRunStatus.SUCCEEDED else "",
        metadata=dict(request.metadata),
    )
    manifest_path, completion_report_path = write_run_records(
        workspace,
        request=request,
        result=result,
    )
    result.manifest_path = str(manifest_path)
    result.completion_report_path = str(completion_report_path)
    return result


async def run_commands_with_approval(
    request: AutonomousRunRequest,
    commands: Sequence[Sequence[str]],
    *,
    request_tool_approval: Any = None,
    runtime_context: dict[str, Any] | None = None,
) -> AutonomousRunResult:
    """Run commands through autonomous approval-aware execution."""
    workspace = create_workspace(request)
    attempts: list[AutonomousAttempt] = []
    started_at = utcnow_iso()

    for index, argv in enumerate(commands):
        attempt = await execute_command_with_approval(
            workspace,
            [str(item) for item in argv],
            attempt_index=index,
            request=request,
            timeout_seconds=request.timeout_seconds,
            request_tool_approval=request_tool_approval,
            runtime_context=runtime_context,
        )
        attempts.append(attempt)
        if attempt.status != AutonomousRunStatus.SUCCEEDED:
            break

    status = _run_status_from_attempts(attempts)
    error = _first_error(attempts) or "No commands were provided."
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=status,
        attempts=attempts,
        started_at=started_at,
        finished_at=utcnow_iso(),
        error=error if status != AutonomousRunStatus.SUCCEEDED else "",
        metadata={
            **dict(request.metadata),
            "approval_aware": True,
            "language": request.language,
            "max_repair_attempts": request.max_repair_attempts,
        },
    )
    manifest_path, completion_report_path = write_run_records(
        workspace,
        request=request,
        result=result,
    )
    result.manifest_path = str(manifest_path)
    result.completion_report_path = str(completion_report_path)
    return result


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
    return manifest_path, completion_report_path


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
    summary_path = workspace.root / "result_summary.md"
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


def _first_error(attempts: list[AutonomousAttempt]) -> str:
    for attempt in attempts:
        if attempt.status != AutonomousRunStatus.SUCCEEDED:
            return attempt.error or f"Command exited with code {attempt.exit_code}."
    return ""


def _run_status_from_attempts(attempts: list[AutonomousAttempt]) -> AutonomousRunStatus:
    if not attempts:
        return AutonomousRunStatus.FAILED
    if all(attempt.status == AutonomousRunStatus.SUCCEEDED for attempt in attempts):
        return AutonomousRunStatus.SUCCEEDED
    if any(attempt.status == AutonomousRunStatus.TIMED_OUT for attempt in attempts):
        return AutonomousRunStatus.TIMED_OUT
    if any(attempt.status == AutonomousRunStatus.CANCELLED for attempt in attempts):
        return AutonomousRunStatus.CANCELLED
    return AutonomousRunStatus.FAILED
