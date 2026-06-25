"""Data contracts for the first-class autonomous code runner boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


AUTONOMOUS_CODE_RUNNER_SOURCE = "autonomous_code_runner"
AUTONOMOUS_WORKSPACE_PURPOSE = "autonomous_code"
AUTONOMOUS_RUN_DIR_PREFIX = "autonomous-code"


class PermissionTier(StrEnum):
    """Coarse-grained permission tier for autonomous commands."""

    READ_ONLY_PROBE = "read_only_probe"
    ANALYSIS_WRITE = "analysis_write"
    SYSTEM_MUTATION = "system_mutation"


class AutonomousRunStatus(StrEnum):
    """Lifecycle status for an autonomous code run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class AutonomousRunRequest:
    """Request to create an autonomous code runner workspace."""

    goal: str
    output_root: str | Path
    input_paths: list[str | Path] = field(default_factory=list)
    upstream_paths: list[str | Path] = field(default_factory=list)
    run_id: str = ""
    # ADR 0035: when set, the run workspace nests under this Project (the active
    # Bench thread); empty keeps the legacy ``<output_root>/autonomous-code__…`` shape.
    project_id: str = ""
    project_name: str = ""
    timeout_seconds: int = 300
    language: str = "python"
    max_repair_attempts: int = 2
    context: str = ""
    web_context: str = ""
    data_schema: str = ""
    analysis_plan: str = ""
    model_override: str = ""
    provider_override: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AutonomousWorkspace:
    """Concrete workspace allocated for one autonomous code run."""

    run_id: str
    root: Path
    scripts_dir: Path
    logs_dir: Path
    figures_dir: Path
    tables_dir: Path
    artifacts_dir: Path
    inputs_dir: Path
    upstream_dir: Path
    created_at: str = field(default_factory=utcnow_iso)


@dataclass(slots=True)
class AutonomousAttempt:
    """One command execution attempt inside an autonomous run workspace."""

    attempt_index: int
    argv: list[str]
    permission_tier: PermissionTier
    status: AutonomousRunStatus
    started_at: str
    finished_at: str = ""
    exit_code: int | None = None
    stdout_log: str = ""
    stderr_log: str = ""
    timed_out: bool = False
    error: str = ""
    approval_required: bool = False
    approval_granted: bool = False
    policy_decision: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_index": self.attempt_index,
            "argv": list(self.argv),
            "permission_tier": self.permission_tier.value,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
            "timed_out": self.timed_out,
            "error": self.error,
            "approval_required": self.approval_required,
            "approval_granted": self.approval_granted,
            "policy_decision": dict(self.policy_decision),
        }


@dataclass(slots=True)
class AutonomousRunResult:
    """Summary returned by the non-LLM autonomous runner foundation."""

    run_id: str
    workspace_root: str
    status: AutonomousRunStatus
    attempts: list[AutonomousAttempt] = field(default_factory=list)
    manifest_path: str = ""
    completion_report_path: str = ""
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == AutonomousRunStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace_root": self.workspace_root,
            "status": self.status.value,
            "ok": self.ok,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "manifest_path": self.manifest_path,
            "completion_report_path": self.completion_report_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "metadata": dict(self.metadata),
        }
