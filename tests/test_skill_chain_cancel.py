"""ADR 0009 L2 — ``run_skill_via_shared_runner`` honours a caller-supplied
``cancel_event``.

The chain.py wrapper previously created a local ``threading.Event`` and
only ``set()`` it inside its own ``asyncio.CancelledError`` bridge.
Per ADR 0009, when a Surface threads ``envelope.cancel_event`` down
through ``tool_runtime_context["cancel_event"]`` and the executor
forwards it as a kwarg, the caller's event must be the one given to
``skill.runner.run_skill`` so that ``surfaces/desktop/server.py``'s
``/chat/abort`` can ``set()`` it from another task and the underlying
``subprocess_driver._cancel_watcher`` will SIGTERM the process group.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_run_skill_via_shared_runner_forwards_caller_cancel_event(
    tmp_path, monkeypatch
):
    """Caller-supplied ``cancel_event`` is the exact object the runner sees."""
    import omicsclaw.skill.chain as chain_mod
    import omicsclaw.skill.runner as runner_mod

    caller_event = threading.Event()
    captured: dict[str, object] = {}

    class _FakeResult:
        stdout = ""
        stderr = ""
        adapter_exit_code = 0
        output_path = tmp_path
        success = True

    def fake_run_skill(*args, **kwargs):
        captured["cancel_event"] = kwargs.get("cancel_event")
        return _FakeResult()

    monkeypatch.setattr(runner_mod, "run_skill", fake_run_skill)
    monkeypatch.setattr(
        chain_mod,
        "lookup_skill_info",
        lambda key: {"alias": key},
    )

    result = await chain_mod.run_skill_via_shared_runner(
        skill_key="fake-skill",
        input_path=None,
        session_path=None,
        mode="demo",
        out_dir=tmp_path,
        cancel_event=caller_event,
    )

    assert result["success"] is True
    assert captured["cancel_event"] is caller_event


@pytest.mark.asyncio
async def test_run_skill_via_shared_runner_falls_back_to_local_event_when_none(
    tmp_path, monkeypatch
):
    """Legacy callers that pass no ``cancel_event`` still get a local one
    so the ``asyncio.CancelledError → cancel_event.set()`` bridge keeps
    working."""
    import omicsclaw.skill.chain as chain_mod
    import omicsclaw.skill.runner as runner_mod

    captured: dict[str, object] = {}

    class _FakeResult:
        stdout = ""
        stderr = ""
        adapter_exit_code = 0
        output_path = tmp_path
        success = True

    def fake_run_skill(*args, **kwargs):
        captured["cancel_event"] = kwargs.get("cancel_event")
        return _FakeResult()

    monkeypatch.setattr(runner_mod, "run_skill", fake_run_skill)
    monkeypatch.setattr(
        chain_mod,
        "lookup_skill_info",
        lambda key: {"alias": key},
    )

    await chain_mod.run_skill_via_shared_runner(
        skill_key="fake-skill",
        input_path=None,
        session_path=None,
        mode="demo",
        out_dir=tmp_path,
    )

    assert isinstance(captured["cancel_event"], threading.Event)
    assert captured["cancel_event"].is_set() is False


@pytest.mark.asyncio
async def test_run_skill_via_shared_runner_set_caller_event_on_asyncio_cancel(
    tmp_path, monkeypatch
):
    """When the outer awaiter is cancelled mid-skill, the chain must
    propagate the abort by setting the caller's event so the subprocess
    driver's ``_cancel_watcher`` wakes up and SIGTERMs the process group."""
    import omicsclaw.skill.chain as chain_mod
    import omicsclaw.skill.runner as runner_mod

    caller_event = threading.Event()
    started = threading.Event()

    def fake_run_skill(*args, **kwargs):
        # Simulate a long-running subprocess that asyncio decides to cancel.
        started.set()
        # Block until the caller event is set — this mirrors what the real
        # subprocess driver does (popen.wait() inside threading.Thread).
        kwargs["cancel_event"].wait(timeout=5.0)
        raise RuntimeError("subprocess was killed")

    monkeypatch.setattr(runner_mod, "run_skill", fake_run_skill)
    monkeypatch.setattr(
        chain_mod,
        "lookup_skill_info",
        lambda key: {"alias": key},
    )

    async def run_and_cancel():
        task = asyncio.create_task(
            chain_mod.run_skill_via_shared_runner(
                skill_key="fake-skill",
                input_path=None,
                session_path=None,
                mode="demo",
                out_dir=tmp_path,
                cancel_event=caller_event,
            )
        )
        # Wait until the subprocess thread is parked on cancel_event.wait().
        await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await task

    await run_and_cancel()

    # The chain.py bridge must set the caller's event so the real
    # subprocess driver's _cancel_watcher would SIGTERM the process group.
    assert caller_event.is_set() is True
