"""Trusted Linux launcher that ties scope publication to its Backend parent."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import signal
import sys


_PR_SET_PDEATHSIG = 1
_LAUNCH_FAILURE = 125


def _arm_parent_death_signal(expected_parent_pid: int) -> None:
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("governed launcher parent changed before arming")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error_number = ctypes.get_errno() or errno.EPERM
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("governed launcher parent changed while arming")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        return _LAUNCH_FAILURE
    try:
        expected_parent_pid = int(args[0])
        if expected_parent_pid <= 1:
            return _LAUNCH_FAILURE
        _arm_parent_death_signal(expected_parent_pid)
        executable = str(Path(args[2]).resolve(strict=True))
        os.execve(executable, [executable, *args[3:]], os.environ)
    except (OSError, RuntimeError, ValueError):
        return _LAUNCH_FAILURE
    return _LAUNCH_FAILURE


if __name__ == "__main__":  # pragma: no cover - exercised through the driver
    raise SystemExit(main())
