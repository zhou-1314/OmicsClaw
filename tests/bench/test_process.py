"""The process runner: it always returns, and leaves nothing running.

Every test here bounds its own duration, because a runner that hangs is
the failure being tested for and nothing else would stop it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from omicsclaw.bench.process import MARKER_VARIABLE, Interrupted, run_process

from ._support import FAKE_AGENT


def start(tmp_path: Path, spec: dict, **options):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    began = time.monotonic()
    exit = run_process(
        [sys.executable, str(FAKE_AGENT), str(spec_path)],
        env=dict(os.environ),
        cwd=workspace,
        stdout=tmp_path / "stdout.txt",
        stderr=tmp_path / "stderr.txt",
        wall_clock_s=options.pop("wall_clock_s", 20),
        kill_grace_s=options.pop("kill_grace_s", 5),
        **options,
    )
    return exit, time.monotonic() - began


def alive(pid: int) -> bool:
    """Whether *pid* is a running process (a zombie does not count)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def test_a_process_that_exits_is_reported_with_its_status(tmp_path):
    exit, _ = start(tmp_path, {"write": {"output/a.txt": "x"}, "exit": 3})

    assert exit.started and exit.returncode == 3
    assert not exit.timed_out and not exit.killed
    assert (tmp_path / "ws" / "output" / "a.txt").read_text() == "x"
    assert exit.started_at and exit.ended_at


def test_standard_input_is_at_its_end(tmp_path):
    """An agent that reads its input gets end-of-file at once. With an
    open pipe nobody writes to it would wait for the whole wall clock.
    """
    exit, elapsed = start(tmp_path, {"stdin": True}, wall_clock_s=10)

    assert exit.returncode == 0 and not exit.timed_out
    assert "stdin-bytes=0" in (tmp_path / "stdout.txt").read_text()
    assert elapsed < 5


def test_the_wall_clock_stops_a_run_that_does_not_end(tmp_path):
    exit, elapsed = start(tmp_path, {"sleep": 60}, wall_clock_s=1, kill_grace_s=5)

    assert exit.timed_out and not exit.killed
    assert exit.returncode == -15
    assert elapsed < 5


def test_a_run_that_ignores_the_polite_stop_is_killed(tmp_path):
    exit, elapsed = start(
        tmp_path, {"sleep": 60, "ignore_term": True}, wall_clock_s=1, kill_grace_s=1
    )

    assert exit.timed_out and exit.killed
    assert exit.returncode == -9
    assert elapsed < 6


def test_what_the_run_started_is_killed_even_in_another_session(tmp_path):
    """The stand-in starts a detached ``sleep`` leading its own session,
    which is what a shell command an agent runs looks like. A signal to
    the agent's process group does not reach it; it is found through the
    marker in its environment.
    """
    if not Path("/proc/self/environ").exists():
        pytest.skip("needs /proc")
    pid_file = tmp_path / "stray.pid"

    exit, _ = start(tmp_path, {"stray": str(pid_file)})

    pid = int(pid_file.read_text())
    assert exit.returncode == 0
    assert exit.strays == 1
    assert not alive(pid)


def test_a_stray_is_killed_after_a_timeout_too(tmp_path):
    if not Path("/proc/self/environ").exists():
        pytest.skip("needs /proc")
    pid_file = tmp_path / "stray.pid"

    exit, _ = start(
        tmp_path,
        {"stray": str(pid_file), "sleep": 60, "ignore_term": True},
        wall_clock_s=1,
        kill_grace_s=1,
    )

    assert exit.killed and exit.strays == 1
    assert not alive(int(pid_file.read_text()))


def test_a_command_that_cannot_start_is_not_an_exception(tmp_path):
    exit = run_process(
        [str(tmp_path / "no-such-program")],
        env={},
        cwd=tmp_path,
        stdout=tmp_path / "o",
        stderr=tmp_path / "e",
        wall_clock_s=5,
        kill_grace_s=1,
    )

    assert not exit.started and exit.returncode is None
    assert "no-such-program" in exit.error


def test_the_marker_reaches_the_process(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}")
    probe = tmp_path / "probe.py"
    probe.write_text(f"import os; print(os.environ[{MARKER_VARIABLE!r}])")

    run_process(
        [sys.executable, str(probe)],
        env=dict(os.environ),
        cwd=tmp_path,
        stdout=tmp_path / "o",
        stderr=tmp_path / "e",
        wall_clock_s=10,
        kill_grace_s=1,
    )

    assert len((tmp_path / "o").read_text().strip()) == 32


def test_stopping_the_harness_kills_the_run_and_says_so(tmp_path):
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    began = time.monotonic()

    with pytest.raises(Interrupted):
        start(tmp_path, {"sleep": 60}, wall_clock_s=30, stop=stop)

    assert time.monotonic() - began < 5
