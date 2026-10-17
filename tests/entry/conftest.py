"""Opt-in isolation for owners embedded inside the pytest process."""

import os

import pytest


@pytest.fixture
def notebook_control_rss(monkeypatch):
    """Supply control RSS for behavior tests; scientific process sampling stays real."""
    from omicsclaw.entry.notebook import runtime

    original = runtime.rss_bytes
    runner = os.getpid()
    monkeypatch.setattr(
        runtime,
        "rss_bytes",
        lambda pid: 64 * 1024 * 1024 if pid == runner else original(pid),
    )
