"""Live birth evidence and closure receipts for managed Linux process trees."""

from __future__ import annotations

import os
from pathlib import Path

from .notebook.security import process_evidence, same_process


def namespace(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def capture_containment(root):
    """A configuration name alone is never proof of process containment."""
    if not same_process(root):
        return None
    proc = Path("/proc") / str(root["pid"])
    try:
        executable = (proc / "exe").resolve(strict=True)
        argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        if executable.name != "unshare":
            return capture_supervisor(root, argv)
        if not {b"--pid", b"--fork", b"--kill-child=SIGKILL", b"--"} <= set(argv):
            return None
        selected = namespace(proc / "ns/pid_for_children")
        if selected == namespace(proc / "ns/pid"):
            return None
        children = (proc / "task" / str(root["pid"]) / "children").read_text().split()
        if len(children) != 1:
            return None
        child_path = Path("/proc") / children[0]
        birth = process_evidence(int(children[0]))
        status = (child_path / "status").read_text().splitlines()
        nspid = next(line.split()[1:] for line in status if line.startswith("NSpid:"))
        if (
            not birth
            or birth["uid"] != os.getuid()
            or nspid[-1] != "1"
            or namespace(child_path / "ns/pid") != selected
            or not same_process(root)
        ):
            return None
        binary = executable.stat()
        return {
            "version": 1,
            "kind": "linux_unshare_pid_namespace",
            "root": root,
            "init": birth,
            "namespace": selected,
            "launcher": {
                "path": str(executable),
                "device": binary.st_dev,
                "inode": binary.st_ino,
            },
            "killChild": "SIGKILL",
        }
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def contained_exit(proof, root):
    """Confirm namespace destruction or an authenticated subreaper closure."""
    if isinstance(proof, dict) and proof.get("kind") == "linux_subreaper":
        from .notebook.security import read_private

        if proof.get("version") != 1 or proof.get("root") != root or same_process(root):
            return False
        try:
            closed = read_private(Path(proof["directory"]) / "closed.json")
            return bool(
                closed.get("version") == 1
                and closed.get("kind") == "linux_subreaper"
                and type(closed.get("exitCode")) is int
                and closed.get("root") == root
                and closed.get("nonce") == proof["nonce"]
                and closed.get("echild") is True
                and closed.get("childrenEmpty") is True
                and closed.get("subreaper") is True
            )
        except (OSError, ValueError, KeyError):
            return False
    return bool(
        isinstance(proof, dict)
        and proof.get("version") == 1
        and proof.get("kind") == "linux_unshare_pid_namespace"
        and proof.get("root") == root
        and proof.get("killChild") == "SIGKILL"
        and proof.get("init")
        and not same_process(root)
        and not same_process(proof["init"])
    )


def gated_argv(argv, directory, *, python):
    """The fixed child cannot execute caller code before its host opens the gate."""
    import json
    import uuid

    token = uuid.uuid4().hex
    gate = Path(directory) / (".launch-" + token)
    worker = Path(__file__).resolve().parents[1] / "skillenv/contained_exec.py"
    request = {"gate": str(gate), "token": token, "argv": list(argv)}
    return [python, "-I", "-B", str(worker), json.dumps(request)], gate, token


def open_gate(gate, token):
    descriptor = os.open(gate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as writer:
        writer.write(token)
        writer.flush()
        os.fsync(writer.fileno())


async def wait_containment(root, *, timeout=5):
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while same_process(root) and asyncio.get_running_loop().time() < deadline:
        proof = capture_containment(root)
        if proof:
            return proof
        await asyncio.sleep(0.01)
    return None


def capture_supervisor(root, argv):
    import hashlib
    import json
    import re

    from .notebook.security import read_private

    worker = Path(__file__).resolve().parents[1] / "skillenv/process_supervisor.py"
    try:
        if (
            len(argv) != 5
            or argv[1:3] != [b"-I", b"-B"]
            or Path(os.fsdecode(argv[3])).resolve() != worker
        ):
            return None
        request = json.loads(argv[4])
        if set(request) != {"directory", "nonce", "argv"} or not re.fullmatch(
            "[a-f0-9]{32}", request["nonce"]
        ):
            return None
        directory = Path(request["directory"])
        info = directory.lstat()
        if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        value = read_private(directory / "ready.json")
        if value != {
            "version": 1,
            "kind": "linux_subreaper",
            "root": root,
            "nonce": request["nonce"],
            "subreaper": True,
        }:
            return None
        launched = read_private(directory / "launched.json")
        if {
            k: v for k, v in launched.items() if k != "child"
        } != value or not same_process(launched.get("child")):
            return None
        return {
            "version": 1,
            "kind": "linux_subreaper",
            "child": launched["child"],
            "root": root,
            "directory": str(directory),
            "nonce": request["nonce"],
            "sourceSha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


class SubreaperLifecycleWrapper:
    """Host-PID lifecycle supervision; underlying sandbox policy stays explicit."""

    kernel_transport = "ipc"

    def __init__(self, directory, *, inner=None):
        self.directory, self.inner = Path(directory), inner

    def wrap(self, argv, *, cwd, env, extra_read_paths=(), extra_write_paths=()):
        import json
        import sys
        import uuid

        from .notebook.security import private_directory

        if self.inner:
            argv, env = self.inner.wrap(
                argv,
                cwd=cwd,
                env=env,
                extra_read_paths=extra_read_paths,
                extra_write_paths=extra_write_paths,
            )
        nonce = uuid.uuid4().hex
        directory = private_directory(self.directory / nonce)
        worker = Path(__file__).resolve().parents[1] / "skillenv/process_supervisor.py"
        request = {"directory": str(directory), "nonce": nonce, "argv": list(argv)}
        return [sys.executable, "-I", "-B", str(worker), json.dumps(request)], dict(env)

    def terminate(self, pid, guard):
        import signal
        import time

        if not guard or not guard(pid):
            return False
        root = process_evidence(pid)
        if root is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 4
        while same_process(root) and time.monotonic() < deadline:
            time.sleep(0.01)
        return not same_process(root)
