"""Finite execution wrapper; writes only the original process exit evidence.

Uploaded as a standalone program and run by the explicitly selected Python.
The original session leader stays alive while its children are observed.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

MAX_PROCESSES = 65536
MAX_MEMBERS = 4096


class DeadlineError(ValueError):
    """A fixed supervisor diagnostic, never arbitrary child output."""


def read_owned(path: str, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
        ):
            raise DeadlineError("deadline_evidence_owner")
        data = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(data) > maximum
            or any(getattr(before, key) != getattr(after, key) for key in fields)
            or len(data) != before.st_size
        ):
            raise DeadlineError("deadline_evidence_unstable")
        return data
    finally:
        os.close(descriptor)


def process(pid: int) -> tuple[int, int, int, int, str]:
    path = Path("/proc") / str(pid)
    with (path / "stat").open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise DeadlineError("deadline_process_budget")
    fields = raw.rsplit(b") ", 1)[1].split()
    return (
        path.stat().st_uid,
        int(fields[2]),
        int(fields[3]),
        int(fields[19]),
        fields[0].decode("ascii"),
    )


def own_identity() -> tuple[int, int, int, int, str]:
    proof = json.loads(read_owned("_omicsclaw_process.json", 4096))
    nonce = read_owned("_omicsclaw_nonce", 64).decode().strip()
    identity = process(os.getpid())
    uid, pgid, sid, ticks, _ = identity
    if (
        proof
        != {
            "uid": uid,
            "host_id": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid": os.getpid(),
            "start_ticks": ticks,
            "pgid": pgid,
            "sid": sid,
            "nonce": nonce,
        }
        or uid != os.getuid()
        or pgid != os.getpid()
        or sid != os.getpid()
    ):
        raise DeadlineError("deadline_identity_unconfirmed")
    return identity


def members(owner: tuple[int, int, int, int, str]) -> list[tuple[int, tuple]]:
    found = []
    count = 0
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            count += 1
            if count > MAX_PROCESSES:
                raise DeadlineError("deadline_process_budget")
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                identity = process(pid)
            except FileNotFoundError:
                continue
            if identity[1:3] == owner[1:3] and identity[4] != "Z":
                if identity[0] != owner[0]:
                    raise DeadlineError("deadline_group_owner_conflict")
                found.append((pid, identity))
                if len(found) > MAX_MEMBERS:
                    raise DeadlineError("deadline_group_budget")
    return found


def signal_members(owner, signum, evidence):
    if (
        read_owned("_omicsclaw_process.json", 4096),
        read_owned("_omicsclaw_nonce", 64),
    ) != evidence:
        raise DeadlineError("deadline_evidence_changed")
    if process(os.getpid())[:4] != owner[:4]:
        raise DeadlineError("deadline_leader_changed")
    for pid, before in members(owner):
        try:
            descriptor = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            continue
        try:
            try:
                after = process(pid)
            except FileNotFoundError:
                continue
            if after[:4] != before[:4]:
                raise DeadlineError("deadline_member_changed")
            signal.pidfd_send_signal(descriptor, signum)
        except ProcessLookupError:
            continue
        finally:
            os.close(descriptor)


def reap(child):
    if child.poll() is None:
        return
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return


def publish(code: int, canceled: bool):
    content = (f"__OMICSCLAW_CANCELED__:{code}" if canceled else str(code)).encode()
    descriptor = os.open(
        "_omicsclaw_exit_code.tmp",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if (
        Path("_omicsclaw_exit_code").exists()
        or Path("_omicsclaw_exit_code").is_symlink()
    ):
        raise DeadlineError("deadline_exit_conflict")
    os.rename("_omicsclaw_exit_code.tmp", "_omicsclaw_exit_code")
    descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def supervise(seconds: float) -> int:
    if not math.isfinite(seconds) or not 0 < seconds <= 604800:
        raise DeadlineError("invalid_hard_timeout")
    evidence = (
        read_owned("_omicsclaw_process.json", 4096),
        read_owned("_omicsclaw_nonce", 64),
    )
    owner = own_identity()
    if (
        read_owned("_omicsclaw_process.json", 4096),
        read_owned("_omicsclaw_nonce", 64),
    ) != evidence:
        raise DeadlineError("deadline_evidence_changed")
    descriptor = os.pidfd_open(os.getpid(), 0)
    try:
        signal.pidfd_send_signal(descriptor, 0)
    finally:
        os.close(descriptor)
    # Adopt orphan descendants so their zombies cannot retain this session.
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "deadline_subreaper_unavailable")
    canceled = False

    def request_cancel(_signum, _frame):
        nonlocal canceled
        canceled = True

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    child = subprocess.Popen(["/bin/bash", "./cmd.sh"])
    expires = time.monotonic() + seconds
    expired = False
    while child.poll() is None or members(owner):
        if canceled or time.monotonic() >= expires:
            expired = not canceled
            signal_members(owner, signal.SIGTERM, evidence)
            grace = time.monotonic() + 0.5
            while time.monotonic() < grace and members(owner):
                reap(child)
                time.sleep(0.01)
            signal_members(owner, signal.SIGKILL, evidence)
            cleanup = time.monotonic() + 2
            while members(owner) and time.monotonic() < cleanup:
                reap(child)
                time.sleep(0.01)
            if members(owner) or child.poll() is None:
                raise DeadlineError("deadline_exit_unconfirmed")
            break
        reap(child)
        time.sleep(0.01)
    reap(child)
    code = child.returncode
    if code is None:
        raise DeadlineError("deadline_child_exit_unconfirmed")
    code = 128 - code if code < 0 else code
    if code == 0 and expired:
        code = 124
    elif code == 0 and canceled:
        code = 143
    publish(code, canceled)
    return code


if __name__ == "__main__":
    try:
        result = supervise(float(sys.argv[1]))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
    ) as error:
        # No exit file: the existing observer retains unknown, not completion.
        print(
            "execution_supervisor_unconfirmed:"
            + (
                str(error) if isinstance(error, DeadlineError) else type(error).__name__
            ),
            file=sys.stderr,
        )
        result = 125
    sys.exit(result)
