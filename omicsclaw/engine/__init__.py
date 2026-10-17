"""Expose the agent loop, configuration and execution protocols.

The loop schedules tools, injects observations and decides when to stop.
Prompt assembly, persistence and tool implementations live in other layers.
Optional per-run compaction and augmentation arrive through protocols.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from .augmentor import TurnAugmentor
from .compactor import HistoryCompactor
from .config import EngineConfig
from .conversation import Conversation
from .executor import (
    ConcurrencyAwareExecutor,
    DeadlineAwareExecutor,
    TimeoutPause,
    ToolExecutor,
    execute_tool_calls,
    observations,
)
from .loop import AgentEngine
from .prompt import PromptSource, RenderedPrompt
from .types import EngineError, EngineEvent, EngineEventType, RunResult, StopReason

__all__ = [
    "AgentEngine",
    "ConcurrencyAwareExecutor",
    "Conversation",
    "DeadlineAwareExecutor",
    "EngineConfig",
    "EngineError",
    "EngineEvent",
    "EngineEventType",
    "HistoryCompactor",
    "PromptSource",
    "RenderedPrompt",
    "RunResult",
    "StopReason",
    "TimeoutPause",
    "ToolExecutor",
    "TurnAugmentor",
    "execute_tool_calls",
    "observations",
]
