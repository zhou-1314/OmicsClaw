"""Integration tests for the ADR 0032 replay validation gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.autonomous.budget import MiniAgentBudget
from omicsclaw.autonomous.kernel_envelope import envelope_available
from omicsclaw.autonomous.kernel_session import kernel_ipc_available
from omicsclaw.autonomous.replay import REPLAY_SCRIPT, validate_replay

SANDBOX = envelope_available()
IPC_AVAILABLE = kernel_ipc_available()
BUDGET = MiniAgentBudget(wall_clock_seconds=120)


def test_replay_passes_for_reproducible_cells(tmp_path: Path):
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    result = validate_replay(
        workspace=tmp_path,
        accepted_cells=["x = 10\nprint('x is', x)", "ReturnAnswer('val=%d' % x)"],
        input_paths=[],
        budget=BUDGET,
        sandbox=SANDBOX,
    )
    assert result.ok is True
    assert result.answer == "val=10"
    assert (tmp_path / REPLAY_SCRIPT).exists()
    assert "accepted step" in (tmp_path / REPLAY_SCRIPT).read_text()


def test_replay_fails_when_cell_raises(tmp_path: Path):
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    result = validate_replay(
        workspace=tmp_path,
        accepted_cells=["raise ValueError('boom')"],
        input_paths=[],
        budget=BUDGET,
        sandbox=SANDBOX,
    )
    assert result.ok is False
    assert "ValueError" in result.error or "boom" in result.error


def test_replay_rejects_empty_run(tmp_path: Path):
    result = validate_replay(
        workspace=tmp_path,
        accepted_cells=[],
        input_paths=[],
        budget=BUDGET,
        sandbox=SANDBOX,
    )
    assert result.ok is False
    assert "no accepted cells" in result.error


def test_replay_rejects_script_that_never_calls_return_answer(tmp_path: Path, monkeypatch):
    class _FakeSession:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def execute(self, _code: str, *, timeout: float):
            class _Cell:
                ok = True
                stdout = "ran without sentinel"
                stderr = ""
                error_summary = ""

            return _Cell()

        def shutdown(self):
            pass

    monkeypatch.setattr("omicsclaw.autonomous.replay.KernelSession", _FakeSession)
    result = validate_replay(
        workspace=tmp_path,
        accepted_cells=["x = 1\nprint(x)"],
        input_paths=[],
        budget=BUDGET,
        sandbox=False,
    )
    assert result.ok is False
    assert "ReturnAnswer" in result.error
