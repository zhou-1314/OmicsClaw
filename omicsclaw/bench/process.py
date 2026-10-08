"""Running one agent process to its end, within a wall-clock budget."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from .outcome import ProcessExit
from .stage import utc_now

__all__ = ["MARKER_VARIABLE", "Interrupted", "run_process", "sweep"]

MARKER_VARIABLE = "OMICSCLAW_BENCH_RUN"
"""Set to a unique value in every run's environment and inherited by what
the run starts, which is how :func:`sweep` finds a run's processes."""

_POLL_S = 0.2


class Interrupted(RuntimeError):
    """The harness was asked to stop while a run was in progress."""


def run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    stdout: Path,
    stderr: Path,
    wall_clock_s: float,
    kill_grace_s: float,
    stop: threading.Event | None = None,
) -> ProcessExit:
    """Run *argv* to completion and report how it ended.

    The process leads a new session, reads end-of-file on standard input
    and writes its output to *stdout* and *stderr*. When *wall_clock_s*
    runs out its process group gets ``SIGTERM``, and ``SIGKILL`` if it is
    still alive *kill_grace_s* later. Whatever the run started that is
    still alive afterwards is killed too, wherever it was reparented to.

    :param env: The complete environment of the process.
    :param stop: Set from another thread to abandon the run.
    :returns: The exit; ``started`` is ``False`` when the process could not
        be started.
    :raises Interrupted: *stop* was set. The process and everything it
        started are killed first.
    """
    marker = uuid.uuid4().hex
    environment = {**env, MARKER_VARIABLE: marker}
    started_at = utc_now()
    began = time.monotonic()
    try:
        with open(stdout, "wb") as out, open(stderr, "wb") as err:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=environment,
                cwd=str(cwd),
                start_new_session=True,
            )
    except OSError as exc:
        return ProcessExit(
            started=False, started_at=started_at, ended_at=utc_now(), error=str(exc)
        )

    timed_out = killed = False
    try:
        if not _wait(process, began + wall_clock_s, stop):
            timed_out = True
            _signal_group(process, signal.SIGTERM)
            if not _wait(process, time.monotonic() + kill_grace_s, stop):
                killed = True
                _signal_group(process, signal.SIGKILL)
                process.wait()
    except Interrupted:
        _signal_group(process, signal.SIGKILL)
        process.wait()
        sweep(marker)
        raise
    wall_s = time.monotonic() - began
    return ProcessExit(
        started=True,
        returncode=process.returncode,
        timed_out=timed_out,
        killed=killed,
        strays=sweep(marker),
        wall_s=round(wall_s, 3),
        started_at=started_at,
        ended_at=utc_now(),
    )


def sweep(marker: str, proc: Path = Path("/proc")) -> int:
    """Kill every process whose environment carries *marker*.

    A shell command the agent started leads a session of its own, so it
    outlives a signal sent to the agent's process group, and a background
    job outlives the agent itself. Both inherited the marker.

    :returns: How many processes were killed. ``0`` where *proc* does not
        exist, which is every platform but Linux.
    """
    needle = f"{MARKER_VARIABLE}={marker}".encode()
    killed = 0
    for _ in range(3):
        found = _carrying(needle, proc)
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                continue
        time.sleep(0.05)
    return killed


def _carrying(needle: bytes, proc: Path) -> list[int]:
    own = os.getpid()
    found = []
    try:
        entries = os.listdir(proc)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit() or int(entry) == own:
            continue
        try:
            environ = (proc / entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(int(entry))
    return found


def _wait(
    process: subprocess.Popen[bytes], deadline: float, stop: threading.Event | None
) -> bool:
    """Whether *process* exited before *deadline*."""
    while True:
        if stop is not None and stop.is_set():
            raise Interrupted("the harness was stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return process.poll() is not None
        try:
            process.wait(timeout=min(_POLL_S, remaining))
            return True
        except subprocess.TimeoutExpired:
            continue


def _signal_group(process: subprocess.Popen[bytes], number: int) -> None:
    try:
        os.killpg(process.pid, number)
    except OSError:
        pass
