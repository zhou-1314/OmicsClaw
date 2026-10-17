"""Delegate bounded tasks to sub-agents.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from .definition import TASK_TOOL_NAME, InvalidDefinition, SubAgentDefinition
from .delegate import SUBAGENT_VALUE_KEY, Delegate
from .frontmatter import parse_agent_file
from .loader import LoadErrorSink, load_agents
from .prompt import ChildPrompt, SkillLoader
from .registry import SubAgentRegistry
from .task_tool import TASK_TOOL_POLICY, RecursionRefused, TaskTool

__all__ = [
    "SUBAGENT_VALUE_KEY",
    "TASK_TOOL_NAME",
    "TASK_TOOL_POLICY",
    "ChildPrompt",
    "Delegate",
    "InvalidDefinition",
    "LoadErrorSink",
    "RecursionRefused",
    "SkillLoader",
    "SubAgentDefinition",
    "SubAgentRegistry",
    "TaskTool",
    "load_agents",
    "parse_agent_file",
]
