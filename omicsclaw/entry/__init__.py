"""Expose application composition and user-facing session entry points.

This layer binds providers, tools, context and persistence to runtime services.
Callers resolve configuration, open an application, attach sessions and close
the application when its work ends.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from .approval import ApprovalBroker
from .assembly import AgentApp, build_app, build_skill_index, open_app
from .config import (
    AppConfig,
    AppConfigError,
    SandboxMode,
    SkillsIndex,
    fields_set_by_argv,
    resolve_app_config,
)
from .display import inert_prose
from .events import Terminal, TurnEvent, TurnEventType
from .ingress import Acceptance, DeliveryResult, InboundMessage, SenderPolicy
from .memory import MemoryBinding, open_memory, prepare_memory, session_store
from .render import TextRenderer, to_wire
from .planning import build_injector, build_plan_book
from .sandbox import SandboxBinding, open_sandbox
from .session import (
    InMemorySessionStore,
    QueueFull,
    RegistryClosed,
    Session,
    SessionRegistry,
    SessionStore,
    SubmissionRefused,
    attach_sessions,
)
from .notebook_app import execute_cell_live
from .stream import (
    EventObserverDetached,
    ObserverCapacityError,
    TurnObservation,
    TurnStream,
)
from .subagent import ChildRunner, build_subagent_registry
from .turn import (
    TurnHandle,
    TurnOutcome,
    TurnRunner,
    at_least,
    compose,
    prepare,
    run_turn,
    stream_turn,
)

__all__ = [
    "Acceptance",
    "AgentApp",
    "AppConfig",
    "AppConfigError",
    "ApprovalBroker",
    "ChildRunner",
    "DeliveryResult",
    "EventObserverDetached",
    "InMemorySessionStore",
    "InboundMessage",
    "MemoryBinding",
    "ObserverCapacityError",
    "QueueFull",
    "RegistryClosed",
    "SandboxBinding",
    "SandboxMode",
    "SenderPolicy",
    "Session",
    "SessionRegistry",
    "SessionStore",
    "SkillsIndex",
    "SubmissionRefused",
    "Terminal",
    "TextRenderer",
    "TurnEvent",
    "TurnEventType",
    "TurnHandle",
    "TurnObservation",
    "TurnOutcome",
    "TurnRunner",
    "TurnStream",
    "at_least",
    "attach_sessions",
    "build_app",
    "build_injector",
    "build_plan_book",
    "build_skill_index",
    "build_subagent_registry",
    "compose",
    "execute_cell_live",
    "fields_set_by_argv",
    "inert_prose",
    "open_app",
    "open_memory",
    "open_sandbox",
    "prepare",
    "prepare_memory",
    "resolve_app_config",
    "run_turn",
    "session_store",
    "stream_turn",
    "to_wire",
]
