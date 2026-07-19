"""Desktop compatibility abort ownership and cancellation ordering."""

from __future__ import annotations

import threading

import omicsclaw.surfaces.desktop.server as server
import pytest


class _Task:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_abort_sets_cancel_event_and_cancels_then_cleans_up():
    task = _Task()
    cancel_event = threading.Event()
    owner = server._ActiveDesktopExecution(task, cancel_event, "")
    server._active_sessions["sX"] = owner
    try:
        assert server._abort_active_session("sX") == server._ABORTED
        assert (
            cancel_event.is_set()
        ), "cancel_event must be set so the subprocess is killed"
        assert task.cancelled is True
        assert "sX" not in server._active_sessions
    finally:
        server._active_sessions.pop("sX", None)


def test_abort_no_active_session_returns_false():
    assert server._abort_active_session("does-not-exist") == server._ABORT_NOT_FOUND


class _OrderEvent:
    def __init__(self, order):
        self._order = order

    def set(self):
        self._order.append("set")


class _OrderTask:
    def __init__(self, order):
        self._order = order

    def cancel(self):
        self._order.append("cancel")


def test_cancel_event_is_set_before_task_is_cancelled():
    """ADR 0009 ordering: the cancel_event MUST be set before task.cancel(),
    else the SIGTERM doesn't reach the detached subprocess in time."""
    order: list[str] = []
    server._active_sessions["sO"] = server._ActiveDesktopExecution(
        _OrderTask(order),  # type: ignore[arg-type]
        _OrderEvent(order),  # type: ignore[arg-type]
        "",
    )
    try:
        server._abort_active_session("sO")
        assert order == ["set", "cancel"]
    finally:
        server._active_sessions.pop("sO", None)


def test_predecessor_release_and_disconnect_cannot_touch_replacement():
    old_task, new_task = _Task(), _Task()
    old_event, new_event = threading.Event(), threading.Event()
    old = server._ActiveDesktopExecution(old_task, old_event, "a" * 32)
    new = server._ActiveDesktopExecution(new_task, new_event, "b" * 32)

    server._active_sessions["s-generation"] = old
    try:
        server._replace_active_execution("s-generation", new)

        assert not old_event.is_set()
        assert old_task.cancelled is False
        assert server._active_sessions["s-generation"] is new

        assert server._release_active_execution("s-generation", old) is False
        server._cancel_active_execution(old)
        assert server._active_sessions["s-generation"] is new
        assert not new_event.is_set()
        assert new_task.cancelled is False
    finally:
        server._active_sessions.pop("s-generation", None)


def test_matching_retry_abort_cancels_canonical_original_turn(monkeypatch):
    canceled_turns: list[str] = []
    runtime = type(
        "Runtime",
        (),
        {
            "cancel": staticmethod(canceled_turns.append),
            "lookup_ingress_turn_id": staticmethod(lambda **_kwargs: None),
        },
    )()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)

    old_task, retry_task = _Task(), _Task()
    old = server._ActiveDesktopExecution(
        old_task, threading.Event(), "a" * 32,
        turn_id="f" * 32,
        source_namespace="desktop/v1/" + "3" * 32 + "/owner",
    )
    retry = server._ActiveDesktopExecution(
        retry_task, threading.Event(), "a" * 32,
        source_namespace="desktop/v1/" + "3" * 32 + "/owner",
    )
    server._active_sessions["matching-retry"] = old
    try:
        server._replace_active_execution("matching-retry", retry)
        assert server._abort_active_session("matching-retry", "a" * 32) == (
            server._ABORTED
        )
        assert canceled_turns == ["f" * 32]
        assert old_task.cancelled is False
        assert retry_task.cancelled is False
    finally:
        server._active_sessions.pop("matching-retry", None)


def test_authoritative_abort_reports_unresolved_canonical_turn(monkeypatch):
    runtime = type(
        "Runtime",
        (),
        {
            "cancel": staticmethod(lambda _turn_id: pytest.fail("cancel must not run")),
            "lookup_ingress_turn_id": staticmethod(lambda **_kwargs: None),
        },
    )()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    task = _Task()
    owner = server._ActiveDesktopExecution(
        task,
        threading.Event(),
        "b" * 32,
        source_namespace="desktop/v1/" + "4" * 32 + "/owner",
    )
    server._active_sessions["unresolved-turn"] = owner
    try:
        assert server._abort_active_session("unresolved-turn", "b" * 32) == (
            server._ABORT_TURN_UNRESOLVED
        )
        assert task.cancelled is False
        assert server._active_sessions["unresolved-turn"] is owner
    finally:
        server._active_sessions.pop("unresolved-turn", None)


@pytest.mark.asyncio
async def test_chat_abort_route_returns_conflict_until_canonical_turn_is_bound(
    monkeypatch,
):
    from fastapi import HTTPException

    runtime = type(
        "Runtime",
        (),
        {
            "cancel": staticmethod(lambda _turn_id: pytest.fail("cancel must not run")),
            "lookup_ingress_turn_id": staticmethod(lambda **_kwargs: None),
        },
    )()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    owner = server._ActiveDesktopExecution(
        _Task(),
        threading.Event(),
        "c" * 32,
        source_namespace="desktop/v1/" + "5" * 32 + "/owner",
    )
    server._active_sessions["route-unresolved"] = owner
    try:
        with pytest.raises(HTTPException) as exc_info:
            await server.chat_abort(
                server.AbortRequest(
                    session_id="route-unresolved",
                    source_request_id="c" * 32,
                )
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "canonical_turn_not_yet_bound"
    finally:
        server._active_sessions.pop("route-unresolved", None)
