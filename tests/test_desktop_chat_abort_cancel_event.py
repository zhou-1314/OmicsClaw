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
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_chat_abort_sets_envelope_cancel_event_before_cancelling_task():
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server as server_mod

    session_id = "test-session-abort-1"
    cancel_event = threading.Event()

    # Stand-in for the dispatch() task — a coroutine parked indefinitely.
    async def _parked():
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(_parked())

    source_request_id = "a" * 32
    server_mod._active_sessions[session_id] = server_mod._ActiveDesktopExecution(
        task=task,
        cancel_event=cancel_event,
        source_request_id=source_request_id,
    )

    # Build an AbortRequest pydantic model and hit the endpoint handler.
    abort_req = server_mod.AbortRequest(
        session_id=session_id,
        source_request_id=source_request_id,
    )
    response = await server_mod.chat_abort(abort_req)

    assert response == {"status": "aborted", "session_id": session_id}
    # The event must be set so subprocess_driver._cancel_watcher would
    # SIGTERM the process group — this is the load-bearing assertion.
    assert cancel_event.is_set() is True
    # The task must also be cancelled (preserves the prior behaviour).
    assert task.cancelled() or task.done() or task._must_cancel  # type: ignore[attr-defined]

    # The owner registry is cleaned up.
    assert session_id not in server_mod._active_sessions

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
async def test_chat_abort_routes_control_turn_through_opaque_turn_id(monkeypatch):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server as server_mod

    canceled_turns: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "_desktop_control_runtime",
        SimpleNamespace(cancel=canceled_turns.append),
    )

    async def _parked():
        await asyncio.sleep(60.0)

    task = asyncio.create_task(_parked())
    owner = server_mod._ActiveDesktopExecution(
        task=task,
        cancel_event=threading.Event(),
        source_request_id="a" * 32,
        turn_id="f" * 32,
    )
    server_mod._active_sessions["controlled-session"] = owner
    try:
        response = await server_mod.chat_abort(
            server_mod.AbortRequest(
                session_id="controlled-session",
                source_request_id="a" * 32,
            )
        )

        assert response["status"] == "aborted"
        assert canceled_turns == ["f" * 32]
        assert owner.cancel_event.is_set()
        assert not task.cancelled()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_chat_abort_keeps_legacy_empty_generation_compatible():
    """A genuinely legacy owner with no source id remains session-abortable."""
    pytest.importorskip("fastapi")

    from omicsclaw.surfaces.desktop import server as server_mod

    session_id = "test-session-no-envelope-1"

    async def _parked():
        await asyncio.sleep(60.0)

    task = asyncio.create_task(_parked())
    cancel_event = threading.Event()
    server_mod._active_sessions[session_id] = server_mod._ActiveDesktopExecution(
        task=task,
        cancel_event=cancel_event,
        source_request_id="",
    )

    abort_req = server_mod.AbortRequest(session_id=session_id)
    response = await server_mod.chat_abort(abort_req)

    assert response == {"status": "aborted", "session_id": session_id}
    assert cancel_event.is_set()
    assert session_id not in server_mod._active_sessions
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_chat_abort_rejects_stale_or_missing_generation_without_touching_owner():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from omicsclaw.surfaces.desktop import server as server_mod

    session_id = "test-session-generation-fence"
    cancel_event = threading.Event()

    async def _parked():
        await asyncio.sleep(60.0)

    task = asyncio.create_task(_parked())
    current_source = "b" * 32
    owner = server_mod._ActiveDesktopExecution(
        task=task,
        cancel_event=cancel_event,
        source_request_id=current_source,
    )
    server_mod._active_sessions[session_id] = owner

    try:
        for supplied_source in ("", "c" * 32):
            with pytest.raises(HTTPException) as exc_info:
                await server_mod.chat_abort(
                    server_mod.AbortRequest(
                        session_id=session_id,
                        source_request_id=supplied_source,
                    )
                )
            assert exc_info.value.status_code == 409
            assert server_mod._active_sessions.get(session_id) is owner
            assert not cancel_event.is_set()
            assert not task.cancelled()
    finally:
        server_mod._active_sessions.pop(session_id, None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
