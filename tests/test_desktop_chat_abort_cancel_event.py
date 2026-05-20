"""ADR 0009 L3 — ``/chat/abort`` sets the envelope's ``cancel_event``
*before* cancelling the dispatch task.

This is the bug fix that closes the "/chat/abort断链" finding in ADR
0009 §Context Finding 2: ``task.cancel()`` alone only raises
``CancelledError`` at the outermost ``await`` in dispatch(); it never
reaches subprocess_driver._cancel_watcher, so the skill subprocess
keeps running in its detached process group even though the user
pressed the abort button.

The fix is to also signal ``envelope.cancel_event.set()`` so the SIGTERM
propagates through tool_runtime_context → run_skill → subprocess driver.
"""

from __future__ import annotations

import asyncio
import threading

import pytest


@pytest.mark.asyncio
async def test_chat_abort_sets_envelope_cancel_event_before_cancelling_task():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from omicsclaw.runtime.agent.envelope import MessageEnvelope
    from omicsclaw.surfaces.desktop import server as server_mod

    session_id = "test-session-abort-1"
    cancel_event = threading.Event()
    envelope = MessageEnvelope(
        chat_id=session_id,
        content="long running task",
        cancel_event=cancel_event,
    )

    # Stand-in for the dispatch() task — a coroutine parked indefinitely.
    async def _parked():
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(_parked())

    # Register both in the module-level dicts the way the SSE handler does.
    server_mod._active_sessions[session_id] = task
    server_mod._active_envelopes[session_id] = envelope

    # Build an AbortRequest pydantic model and hit the endpoint handler.
    abort_req = server_mod.AbortRequest(session_id=session_id)
    response = await server_mod.chat_abort(abort_req)

    assert response == {"status": "aborted", "session_id": session_id}
    # The event must be set so subprocess_driver._cancel_watcher would
    # SIGTERM the process group — this is the load-bearing assertion.
    assert cancel_event.is_set() is True
    # The task must also be cancelled (preserves the prior behaviour).
    assert task.cancelled() or task.done() or task._must_cancel  # type: ignore[attr-defined]

    # Both registries are cleaned up.
    assert session_id not in server_mod._active_sessions
    assert session_id not in server_mod._active_envelopes

    # Drain the cancelled task so the event loop doesn't warn.
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_chat_abort_returns_404_when_session_unknown():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from omicsclaw.surfaces.desktop import server as server_mod

    abort_req = server_mod.AbortRequest(session_id="not-a-real-session-id-xyz")
    with pytest.raises(HTTPException) as exc_info:
        await server_mod.chat_abort(abort_req)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_chat_abort_handles_session_without_envelope_gracefully():
    """If a session was registered before ADR 0009 wiring (or by a code
    path that doesn't construct envelopes), abort must still cancel the
    task and return cleanly — no AttributeError on ``envelope.cancel_event``."""
    pytest.importorskip("fastapi")

    from omicsclaw.surfaces.desktop import server as server_mod

    session_id = "test-session-no-envelope-1"

    async def _parked():
        await asyncio.sleep(60.0)

    task = asyncio.create_task(_parked())
    server_mod._active_sessions[session_id] = task
    # Note: no entry in _active_envelopes — simulates a legacy path.

    abort_req = server_mod.AbortRequest(session_id=session_id)
    response = await server_mod.chat_abort(abort_req)

    assert response == {"status": "aborted", "session_id": session_id}
    assert session_id not in server_mod._active_sessions
    with pytest.raises(asyncio.CancelledError):
        await task
