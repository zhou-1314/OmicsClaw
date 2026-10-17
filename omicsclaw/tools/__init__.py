"""Expose tool interfaces, dispatch and per-call execution context.

ToolRegistry supplies model definitions and executes calls as observations.
It satisfies the engine's executor protocol without importing that layer.
Composition supplies tools, policy wrappers and deployment services.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from ._workspace import Workspace
from .base import ApprovalMode, RiskLevel, Tool, ToolPolicy
from .builtin import (
    AskUserTool,
    BashTool,
    EditTool,
    WebFetchTool,
    WebSearchTool,
    WriteTool,
    read_tool,
)
from .context import (
    AnswerStatus,
    ApprovalChannel,
    ApprovalDecision,
    ApprovalDenied,
    ApprovalRequest,
    ApprovalUnavailable,
    ProgressSink,
    ProgressUpdate,
    QuestionAnswer,
    QuestionChannel,
    QuestionOption,
    QuestionRequest,
    QuestionUnavailable,
    TimeoutPause,
    ToolContext,
    ask_every_time,
    ask_question,
    context_value,
    current_context,
    effective_policy,
    pause_tool_timeout,
    report_progress,
    require_approval,
    reset_tool_context,
    set_tool_context,
    use_effective_policy,
    use_timeout_pause,
    use_tool_context,
)
from .function_tool import FunctionTool, ToolArgumentError
from .mcp_tool import MCPCaller, MCPTool, mcp_tool_name, sanitize_mcp_name
from .registry import (
    ToolAlreadyRegistered,
    ToolNameMismatch,
    ToolRegistrationError,
    ToolRegistry,
)

__all__ = [
    "AnswerStatus",
    "ApprovalChannel",
    "ApprovalDecision",
    "ApprovalDenied",
    "ApprovalMode",
    "ApprovalRequest",
    "ApprovalUnavailable",
    "AskUserTool",
    "BashTool",
    "EditTool",
    "FunctionTool",
    "MCPCaller",
    "MCPTool",
    "ProgressSink",
    "ProgressUpdate",
    "QuestionAnswer",
    "QuestionChannel",
    "QuestionOption",
    "QuestionRequest",
    "QuestionUnavailable",
    "RiskLevel",
    "TimeoutPause",
    "Tool",
    "ToolAlreadyRegistered",
    "ToolArgumentError",
    "ToolContext",
    "ToolNameMismatch",
    "ToolPolicy",
    "ToolRegistrationError",
    "ToolRegistry",
    "WebFetchTool",
    "WebSearchTool",
    "WriteTool",
    "ask_every_time",
    "ask_question",
    "context_value",
    "current_context",
    "effective_policy",
    "mcp_tool_name",
    "pause_tool_timeout",
    "read_tool",
    "report_progress",
    "require_approval",
    "reset_tool_context",
    "sanitize_mcp_name",
    "set_tool_context",
    "use_effective_policy",
    "use_timeout_pause",
    "use_tool_context",
]
