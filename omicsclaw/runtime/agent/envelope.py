"""MessageEnvelope: the request DTO consumed by ``dispatch()``.

Per ADR 0006 Q1/Q7. Every Surface — Channel, Desktop, CLI — constructs
one of these per inbound turn and hands it off to
``omicsclaw.runtime.agent.dispatcher.dispatch``.

Fields mirror the kwargs ``omicsclaw.runtime.agent.loop.llm_tool_loop``
currently accepts. They are kept as a frozen dataclass so misuse (e.g.
mutating an envelope after dispatch starts) fails loudly.
"""

from __future__ import annotations

import threading
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
    # Bench — investigation thread (ADR 0018) + lifecycle stage lens (ADR 0020).
    # Mirrors the ``scoped_memory_scope`` defaulted-field pattern in this
    # dataclass. Empty = legacy (Phase 0: accepted but inert; consumers in 1A/2).
    thread_id: str = ""
    stage: str = ""

    usage_accumulator: Any = None
    request_tool_approval: Any = None
    policy_state: Any = None

    cancel_event: threading.Event | None = None
    """Set by the Surface to request mid-flight cancellation (ADR 0009).

    The dataclass is frozen, but ``threading.Event`` is a mutable
    object — only its internal flag changes via ``.set()``, never
    the field's reference. This preserves the frozen contract
    (no reassignment) while allowing the live cancel signal.

    ``None`` means cancellation is not wired by the calling Surface
    (today: Channel Surface). The dispatch chain treats ``None`` as
    "never cancelled" and skips all cancel-check logic.
    """
