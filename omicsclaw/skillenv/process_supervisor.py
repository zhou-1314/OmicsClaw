"""One Linux subreaper owns one scientific process and its adopted descendants."""

import ctypes
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

WALL = 0x40000000


def birth(pid):
    root = Path("/proc") / str(pid)
    try:
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "uid": root.stat().st_uid,
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start": fields[19],
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def children():
    return sorted(
        {
            int(v)
            for task in Path(f"/proc/{os.getpid()}/task").iterdir()
            for v in (task / "children").read_text().split()
        }
    )


def signal_child(libc, evidence, signum):
    # These 64-bit Linux ABIs share the asm-generic pidfd syscall numbers.
    if (
        platform.machine() not in {"x86_64", "aarch64", "riscv64"}
        or ctypes.sizeof(ctypes.c_void_p) != 8
    ):
        raise RuntimeError("pidfd ABI unsupported")
    descriptor = libc.syscall(434, evidence["pid"], 0)
    if descriptor < 0:
        if birth(evidence["pid"]) is None:
            return
        raise OSError(ctypes.get_errno(), "pidfd_open")
    try:
        if birth(evidence["pid"]) == evidence:
            result = libc.syscall(424, descriptor, signum, 0, 0)
            if result < 0 and ctypes.get_errno() != 3:
                raise OSError(ctypes.get_errno(), "pidfd_send_signal")
    finally:
        os.close(descriptor)


def write(directory, name, value):
    target = directory / name
    fd = os.open(
        target.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(target.with_suffix(".tmp"), target)
    fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    request = json.loads(sys.argv[1])
    directory = Path(request["directory"])
    libc = ctypes.CDLL(None, use_errno=True)
    enabled = ctypes.c_int()
    if (
        libc.prctl(36, 1, 0, 0, 0)
        or libc.prctl(37, ctypes.byref(enabled), 0, 0, 0)
        or enabled.value != 1
    ):
        raise SystemExit("subreaper unavailable")
    # Python's default disposition retains children for this exclusive waiter.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    stopping = False
    root_child = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    def interrupt(signum, frame):
        if root_child and birth(root_child["pid"]) == root_child:
            try:
                os.killpg(root_child["pid"], signal.SIGINT)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, interrupt)
    root = birth(os.getpid())
    document = {
        "version": 1,
        "kind": "linux_subreaper",
        "root": root,
        "nonce": request["nonce"],
        "subreaper": True,
    }
    write(directory, "ready.json", document)
    process = subprocess.Popen(request["argv"], start_new_session=True)
    root_child = birth(process.pid)
    write(directory, "launched.json", {**document, "child": root_child})
    code = None
    deadline = None
    while True:
        no_children = False
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | WALL)
            except ChildProcessError:
                no_children = True
                break
            if pid == 0:
                break
            if pid == process.pid:
                code = os.waitstatus_to_exitcode(status)
                process.returncode = code
                stopping = True
        if no_children and not children():
            if code is None:
                raise SystemExit("primary child outcome unknown")
            if libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) or enabled.value != 1:
                raise SystemExit("subreaper state changed")
            write(
                directory,
                "closed.json",
                {**document, "echild": True, "childrenEmpty": True, "exitCode": code},
            )
            raise SystemExit(code if code >= 0 else 128 - code)
        if stopping:
            if deadline is None:
                deadline = time.monotonic() + 3
            for pid in children():
                evidence = birth(pid)
                if evidence and birth(pid) == evidence:
                    signal_child(libc, evidence, signal.SIGKILL)
            if time.monotonic() >= deadline:
                # No closure receipt means the host must retain the scope.
                raise SystemExit("descendant exit unconfirmed")
        time.sleep(0.01)


if __name__ == "__main__":
    main()
