"""SSE client-disconnect must kill the run like /chat/abort (audit E).

The disconnect handler used to only `task.cancel()`, which raises CancelledError
at the outermost await but leaves a skill subprocess running in its detached
process group. Both /chat/abort and the disconnect handler now route through
`_abort_active_session`, which sets the envelope's `cancel_event` BEFORE
cancelling the task (ADR 0009) so the subprocess is actually SIGTERM'd.
"""

from __future__ import annotations

import threading

import omicsclaw.surfaces.desktop.server as server


class _Task:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Envelope:
    def __init__(self):
        self.cancel_event = threading.Event()


def test_abort_sets_cancel_event_and_cancels_then_cleans_up():
    task, env = _Task(), _Envelope()
    server._active_sessions["sX"] = task
    server._active_envelopes["sX"] = env
    try:
        assert server._abort_active_session("sX") is True
        assert env.cancel_event.is_set(), "cancel_event must be set so the subprocess is killed"
        assert task.cancelled is True
        assert "sX" not in server._active_sessions
        assert "sX" not in server._active_envelopes
    finally:
        server._active_sessions.pop("sX", None)
        server._active_envelopes.pop("sX", None)


def test_abort_no_active_session_returns_false():
    assert server._abort_active_session("does-not-exist") is False


class _OrderEnv:
    """An envelope whose cancel_event records call order."""

    def __init__(self, order):
        self._order = order
        self.cancel_event = self

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
    server._active_sessions["sO"] = _OrderTask(order)
    server._active_envelopes["sO"] = _OrderEnv(order)
    try:
        server._abort_active_session("sO")
        assert order == ["set", "cancel"]
    finally:
        server._active_sessions.pop("sO", None)
        server._active_envelopes.pop("sO", None)
