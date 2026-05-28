"""First-class autonomous code runner boundary."""

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
from .executor import execute_command
from .code_loop import (
    ProviderChatClient,
    run_autonomous_code_loop,
    run_autonomous_code_loop_async,
)
from .permissions import classify_command
from .policy import (
    AUTONOMOUS_ANALYSIS_WRITE_TOOL,
    AUTONOMOUS_SYSTEM_MUTATION_TOOL,
    autonomous_tool_spec,
    request_autonomous_approval,
)
from .runner import (
    AUTONOMOUS_CODE_RUNNER_VERSION,
    autonomous_requirements,
    run_commands,
    run_commands_with_approval,
    write_run_records,
)
from .workspace import WORKSPACE_SUBDIRS, build_run_dir_name, create_workspace

__all__ = [
    "AUTONOMOUS_CODE_RUNNER_SOURCE",
    "AUTONOMOUS_WORKSPACE_PURPOSE",
    "AUTONOMOUS_CODE_RUNNER_VERSION",
    "AutonomousAttempt",
    "AutonomousRunRequest",
    "AutonomousRunResult",
    "AutonomousRunStatus",
    "AutonomousWorkspace",
    "PermissionTier",
    "WORKSPACE_SUBDIRS",
    "AUTONOMOUS_ANALYSIS_WRITE_TOOL",
    "AUTONOMOUS_SYSTEM_MUTATION_TOOL",
    "ProviderChatClient",
    "autonomous_tool_spec",
    "build_run_dir_name",
    "classify_command",
    "create_workspace",
    "execute_command",
    "request_autonomous_approval",
    "autonomous_requirements",
    "run_autonomous_code_loop",
    "run_autonomous_code_loop_async",
    "run_commands",
    "run_commands_with_approval",
    "write_run_records",
]
