"""Autonomous analysis path — the single persistent-kernel mini-agent engine.

ADR 0032 (revised 2026-06-22): the legacy one-shot subprocess engine and its
support modules (executor / permissions / policy) were removed in the
single-engine consolidation. The mini-agent (`mini_agent_runner`) is reached via
:func:`run_autonomous_code_loop_async`; it is imported lazily there so importing
this package does not pull in jupyter_client.
"""

from .code_loop import (
    ProviderChatClient,
    run_autonomous_code_loop,
    run_autonomous_code_loop_async,
)
from .contracts import (
    AUTONOMOUS_CODE_RUNNER_SOURCE,
    AUTONOMOUS_WORKSPACE_PURPOSE,
    AutonomousAttempt,
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
    AutonomousWorkspace,
    PermissionTier,
)
from .runner import (
    AUTONOMOUS_CODE_RUNNER_VERSION,
    autonomous_requirements,
    write_run_records,
)
from .workspace import WORKSPACE_SUBDIRS, build_run_dir_name, create_workspace

__all__ = [
    "AUTONOMOUS_CODE_RUNNER_SOURCE",
    "AUTONOMOUS_CODE_RUNNER_VERSION",
    "AUTONOMOUS_WORKSPACE_PURPOSE",
    "AutonomousAttempt",
    "AutonomousRunRequest",
    "AutonomousRunResult",
    "AutonomousRunStatus",
    "AutonomousWorkspace",
    "PermissionTier",
    "ProviderChatClient",
    "WORKSPACE_SUBDIRS",
    "autonomous_requirements",
    "build_run_dir_name",
    "create_workspace",
    "run_autonomous_code_loop",
    "run_autonomous_code_loop_async",
    "write_run_records",
]
