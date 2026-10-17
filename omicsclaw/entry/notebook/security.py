"""Local ownership evidence and locks shared by the controller and Notebook owner."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import stat
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


def private_directory(path: Path) -> Path:
    for candidate in (path.parent, path):
        if candidate.is_symlink():
            raise ValueError("notebook_unsafe_state")
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = candidate.stat()
        if info.st_uid != os.getuid():
            raise ValueError("notebook_unsafe_state")
        if info.st_mode & 0o022:
            if candidate == path or candidate.name == ".omicsclaw":
                try:
                    os.chmod(candidate, 0o700)
                    info = candidate.stat()
                except OSError:
                    pass
            if info.st_mode & 0o022:
                raise ValueError("notebook_unsafe_state")
    return path


def private_file(path: Path) -> int:
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise ValueError("notebook_unsafe_state")
    if info.st_mode & 0o077:
        try:
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
        except OSError:
            pass
        if info.st_mode & 0o077:
            os.close(fd)
            raise ValueError("notebook_unsafe_state")
    return fd


def read_private(path: Path, limit: int = 16384):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise ValueError("notebook_unsafe_state")
        if info.st_mode & 0o077:
            try:
                os.fchmod(fd, 0o600)
                info = os.fstat(fd)
            except OSError:
                pass
            if info.st_mode & 0o077:
                raise ValueError("notebook_unsafe_state")
        return json.loads(os.read(fd, limit + 1))
    finally:
        os.close(fd)


def write_private(path: Path, document):
    fd = private_file(path)
    try:
        encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
        os.ftruncate(fd, 0)
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def process_evidence(pid: int):
    """A PID is usable only while its boot, UID and birth ticks still match."""
    try:
        root = Path(f"/proc/{pid}")
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "uid": root.stat().st_uid,
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start": fields[19],
        }
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def same_process(evidence):
    return bool(
        isinstance(evidence, dict)
        and type(evidence.get("pid")) is int
        and process_evidence(evidence["pid"]) == evidence
    )


def rss_bytes(pid):
    """Current Linux RSS without importing a scientific or monitoring package."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return 0


def tree_rss(pid):
    pending, seen, total = [pid], set(), 0
    while pending and len(seen) < 65536:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += rss_bytes(current)
        try:
            pending.extend(
                int(p)
                for p in Path(f"/proc/{current}/task/{current}/children")
                .read_text()
                .split()
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return total


def gate_file(workspace: Path):
    state = private_directory(workspace / ".omicsclaw")
    return state / "notebook-environments.lock"


@asynccontextmanager
async def environment_gate(workspace: Path, timeout=30):
    fd = private_file(gate_file(workspace))
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("environment_busy")
                await asyncio.sleep(0.02)
        yield
    finally:
        os.close(fd)


@contextmanager
def environment_gate_sync(workspace: Path, timeout=30):
    fd = private_file(gate_file(workspace))
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("environment_busy")
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)
