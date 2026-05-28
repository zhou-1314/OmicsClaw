"""Command execution helpers for autonomous code runner workspaces."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Mapping

from .contracts import (
    AutonomousAttempt,
    AutonomousRunRequest,
    AutonomousRunStatus,
    AutonomousWorkspace,
    PermissionTier,
    utcnow_iso,
)
from .permissions import classify_command
from .policy import request_autonomous_approval
from .runtime_guard import build_python_runtime_guard


def execute_command(
    workspace: AutonomousWorkspace,
    argv: list[str],
    *,
    attempt_index: int,
    timeout_seconds: int = 300,
) -> AutonomousAttempt:
    """Run ``argv`` inside ``workspace`` and capture stdout/stderr logs."""
    permission_tier = classify_command(argv, workspace_root=workspace.root)
    stdout_log = workspace.logs_dir / f"attempt_{attempt_index}.stdout.log"
    stderr_log = workspace.logs_dir / f"attempt_{attempt_index}.stderr.log"
    attempt = AutonomousAttempt(
        attempt_index=attempt_index,
        argv=list(argv),
        permission_tier=permission_tier,
        status=AutonomousRunStatus.RUNNING,
        started_at=utcnow_iso(),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )

    if permission_tier in {PermissionTier.ANALYSIS_WRITE, PermissionTier.SYSTEM_MUTATION}:
        message = (
            f"Command requires {permission_tier.value} approval and was not executed."
        )
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text(message + "\n", encoding="utf-8")
        attempt.status = AutonomousRunStatus.FAILED
        attempt.exit_code = None
        attempt.error = message
        attempt.finished_at = utcnow_iso()
        return attempt

    try:
        completed = subprocess.run(
            argv,
            cwd=workspace.root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_log.write_text(exc.stdout or "", encoding="utf-8")
        stderr_log.write_text(exc.stderr or "Command timed out.\n", encoding="utf-8")
        attempt.status = AutonomousRunStatus.TIMED_OUT
        attempt.timed_out = True
        attempt.exit_code = None
        attempt.error = f"Command timed out after {timeout_seconds} seconds."
    except OSError as exc:
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text(str(exc) + "\n", encoding="utf-8")
        attempt.status = AutonomousRunStatus.FAILED
        attempt.exit_code = None
        attempt.error = str(exc)
    else:
        stdout_log.write_text(completed.stdout or "", encoding="utf-8")
        stderr_log.write_text(completed.stderr or "", encoding="utf-8")
        attempt.exit_code = completed.returncode
        attempt.status = (
            AutonomousRunStatus.SUCCEEDED
            if completed.returncode == 0
            else AutonomousRunStatus.FAILED
        )
    attempt.finished_at = utcnow_iso()
    return attempt


async def execute_command_with_approval(
    workspace: AutonomousWorkspace,
    argv: list[str],
    *,
    attempt_index: int,
    request: AutonomousRunRequest | None = None,
    timeout_seconds: int = 300,
    request_tool_approval: Any = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> AutonomousAttempt:
    """Run a command, using shared approval for autonomous write tiers."""
    permission_tier = classify_command(argv, workspace_root=workspace.root)
    if permission_tier == PermissionTier.READ_ONLY_PROBE:
        return _execute_argv(
            workspace,
            argv,
            attempt_index=attempt_index,
            timeout_seconds=timeout_seconds,
            permission_tier=permission_tier,
        )

    attempt = _build_attempt(
        workspace,
        argv,
        attempt_index=attempt_index,
        permission_tier=permission_tier,
    )
    attempt.approval_required = True

    approval = await request_autonomous_approval(
        permission_tier=permission_tier,
        argv=argv,
        workspace_root=str(workspace.root),
        attempt_index=attempt_index,
        request_tool_approval=request_tool_approval,
        runtime_context=runtime_context,
    )
    if approval.policy_decision is not None:
        attempt.policy_decision = approval.policy_decision.to_dict()
    if not approval.allowed:
        message = approval.message or (
            f"Command requires {permission_tier.value} approval and was not executed."
        )
        _write_attempt_logs(attempt, stdout="", stderr=message + "\n")
        attempt.status = AutonomousRunStatus.FAILED
        attempt.error = message
        attempt.finished_at = utcnow_iso()
        return attempt

    attempt.approval_granted = True
    if permission_tier == PermissionTier.SYSTEM_MUTATION:
        message = (
            "Autonomous system_mutation commands remain disabled in this runtime "
            "even after approval."
        )
        _write_attempt_logs(attempt, stdout="", stderr=message + "\n")
        attempt.status = AutonomousRunStatus.FAILED
        attempt.error = message
        attempt.finished_at = utcnow_iso()
        return attempt

    guarded_argv = _guarded_analysis_argv(
        workspace,
        argv,
        request=request,
    )
    guarded_attempt = _execute_argv(
        workspace,
        guarded_argv,
        attempt_index=attempt_index,
        timeout_seconds=timeout_seconds,
        permission_tier=permission_tier,
        original_argv=argv,
    )
    guarded_attempt.approval_required = True
    guarded_attempt.approval_granted = True
    guarded_attempt.policy_decision = dict(attempt.policy_decision)
    return guarded_attempt


def _build_attempt(
    workspace: AutonomousWorkspace,
    argv: list[str],
    *,
    attempt_index: int,
    permission_tier: PermissionTier,
) -> AutonomousAttempt:
    stdout_log = workspace.logs_dir / f"attempt_{attempt_index}.stdout.log"
    stderr_log = workspace.logs_dir / f"attempt_{attempt_index}.stderr.log"
    return AutonomousAttempt(
        attempt_index=attempt_index,
        argv=list(argv),
        permission_tier=permission_tier,
        status=AutonomousRunStatus.RUNNING,
        started_at=utcnow_iso(),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )


def _write_attempt_logs(attempt: AutonomousAttempt, *, stdout: str, stderr: str) -> None:
    Path(attempt.stdout_log).write_text(stdout or "", encoding="utf-8")
    Path(attempt.stderr_log).write_text(stderr or "", encoding="utf-8")


def _execute_argv(
    workspace: AutonomousWorkspace,
    argv: list[str],
    *,
    attempt_index: int,
    timeout_seconds: int,
    permission_tier: PermissionTier,
    original_argv: list[str] | None = None,
) -> AutonomousAttempt:
    attempt = _build_attempt(
        workspace,
        original_argv or argv,
        attempt_index=attempt_index,
        permission_tier=permission_tier,
    )
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace.root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_attempt_logs(
            attempt,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "Command timed out.\n",
        )
        attempt.status = AutonomousRunStatus.TIMED_OUT
        attempt.timed_out = True
        attempt.exit_code = None
        attempt.error = f"Command timed out after {timeout_seconds} seconds."
    except OSError as exc:
        _write_attempt_logs(attempt, stdout="", stderr=str(exc) + "\n")
        attempt.status = AutonomousRunStatus.FAILED
        attempt.exit_code = None
        attempt.error = str(exc)
    else:
        _write_attempt_logs(
            attempt,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        attempt.exit_code = completed.returncode
        attempt.status = (
            AutonomousRunStatus.SUCCEEDED
            if completed.returncode == 0
            else AutonomousRunStatus.FAILED
        )
    attempt.finished_at = utcnow_iso()
    return attempt


def _guarded_analysis_argv(
    workspace: AutonomousWorkspace,
    argv: list[str],
    *,
    request: AutonomousRunRequest | None,
) -> list[str]:
    executable = Path(str(argv[0])).name.lower() if argv else ""
    if not _is_python_executable(executable):
        return argv
    if len(argv) < 2 or str(argv[1]).startswith("-"):
        return argv
    script_path = Path(argv[1])
    if not script_path.is_absolute():
        script_path = workspace.root / script_path
    guard_path = workspace.scripts_dir / f"_guarded_attempt_{len(list(workspace.scripts_dir.glob('_guarded_attempt_*.py')))}.py"
    guard_code = build_python_runtime_guard(
        workspace_root=workspace.root,
        input_paths=list(request.input_paths if request is not None else []),
        upstream_paths=list(request.upstream_paths if request is not None else []),
        goal=request.goal if request is not None else "",
        context=request.context if request is not None else "",
        web_context=request.web_context if request is not None else "",
    )
    source = script_path.read_text(encoding="utf-8")
    guard_path.write_text(guard_code + "\n\n" + source, encoding="utf-8")
    return [argv[0], str(guard_path), *argv[2:]]


def _is_python_executable(executable: str) -> bool:
    return executable == "python" or executable.startswith("python3")
