"""MessageEnvelope: the request DTO consumed by ``dispatch()``.

Per ADR 0006 Q1/Q7. Every Surface — Channel, Desktop, CLI — constructs
one of these per inbound turn and hands it off to
``omicsclaw.runtime.agent.dispatcher.dispatch``.

Fields mirror the kwargs ``omicsclaw.runtime.agent.loop.llm_tool_loop``
currently accepts. They are kept as a frozen dataclass so misuse (e.g.
mutating an envelope after dispatch starts) fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MessageEnvelope:
    chat_id: int | str
    content: str | list

    user_id: str | None = None
    platform: str | None = None

    workspace: str = ""
    pipeline_workspace: str = ""
    scoped_memory_scope: str = ""
    mcp_servers: tuple[str, ...] | None = None
    output_style: str = ""
    plan_context: str = ""

    model_override: str = ""
    extra_api_params: dict | None = None
    max_tokens_override: int = 0
    system_prompt_append: str = ""
    mode: str = ""

    usage_accumulator: Any = None
    request_tool_approval: Any = None
    policy_state: Any = None
