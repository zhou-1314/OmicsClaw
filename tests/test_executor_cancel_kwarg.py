"""ADR 0009 L2 — ``cancel_event`` in ``runtime_context`` reaches the
executor as a kwarg.

The path proved here is the seam ``ToolSpec.context_params`` →
``build_executor_kwargs`` → executor. After ADR 0009 added
``"cancel_event"`` to the ``omicsclaw`` tool's ``context_params``, the
executor framework should forward
``runtime_context["cancel_event"]`` as a kwarg whenever the executor
declares a ``cancel_event`` parameter.

This test bypasses the full LLM/dispatcher loop so it remains a small
unit test against the executor framework — the dispatcher kwarg-
forwarding is covered in ``test_agent_dispatcher.py`` and the
subprocess-level SIGTERM is covered in
``test_skill_runner_contract.py``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from omicsclaw.runtime.tools.executor import build_executor_kwargs, invoke_tool
from omicsclaw.runtime.tools.spec import ToolSpec


def _spec_with_cancel_event() -> ToolSpec:
    return ToolSpec(
        name="fake",
        description="A fake tool for testing.",
        parameters={"type": "object", "properties": {}},
        context_params=("cancel_event",),
    )


def test_build_executor_kwargs_forwards_cancel_event_from_runtime_context():
    event = threading.Event()
    kwargs = build_executor_kwargs(
        _spec_with_cancel_event(),
        runtime_context={"cancel_event": event, "session_id": "s1"},
    )
    assert kwargs == {"cancel_event": event}


def test_build_executor_kwargs_skips_cancel_event_when_none():
    """``runtime_context["cancel_event"] = None`` (the no-cancel Surface
    case) is silently dropped — the executor's default of ``None`` is
    honoured without a positional ``None`` injection."""
    kwargs = build_executor_kwargs(
        _spec_with_cancel_event(),
        runtime_context={"cancel_event": None, "session_id": "s1"},
    )
    assert kwargs == {}


@pytest.mark.asyncio
async def test_invoke_tool_passes_cancel_event_to_async_executor():
    event = threading.Event()
    seen: dict[str, object] = {}

    async def fake_executor(args: dict, cancel_event=None):
        seen["cancel_event"] = cancel_event
        return "ok"

    result = await invoke_tool(
        _spec_with_cancel_event(),
        fake_executor,
        {"x": 1},
        runtime_context={"cancel_event": event},
    )
    assert result == "ok"
    assert seen["cancel_event"] is event


@pytest.mark.asyncio
async def test_omicsclaw_toolspec_declares_cancel_event_in_context_params():
    """The ``omicsclaw`` ToolSpec must declare ``cancel_event`` so the
    executor framework forwards the per-request signal. Regression guard
    against future spec refactors silently dropping the kwarg."""
    from omicsclaw.runtime.tools.builders.agent import build_bot_tool_specs
    from omicsclaw.runtime.tools.builders.agent import BotToolContext

    specs = build_bot_tool_specs(BotToolContext(skill_names=()))
    omicsclaw_spec = next(s for s in specs if s.name == "omicsclaw")
    assert "cancel_event" in omicsclaw_spec.context_params
