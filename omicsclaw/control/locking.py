"""Lifetime ownership lock for one local Control Database."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import socket
import sys
import threading
from typing import BinaryIO

from .errors import ControlDatabaseOwnedError

try:  # pragma: no cover - selected by platform
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - selected by platform
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


_PROCESS_LOCK = threading.Lock()
_PROCESS_OWNED_PATHS: set[Path] = set()

# The holder record is a single small JSON object written by whoever owns the
# lock. It is advisory diagnostics only — the OS lock, never the file content,
# decides ownership — so every read and write of it is best-effort.
_HOLDER_RECORD_LIMIT = 4096
_HOLDER_COMMAND_LIMIT = 500

_REMEDIATION = (
    "One Backend process owns exactly one Control Database. Either stop the "
    "owning process, or start this one against a different state root via "
    "OMICSCLAW_CONTROL_STATE_ROOT. Do NOT delete the lock file: that does not "
    "release the lock, and it lets two processes write one Control Database."
)


@dataclass(frozen=True)
class ControlLockHolder:
    """Best-effort identity of the process that owns a Control Database."""

    pid: int | None = None
    hostname: str | None = None
    command: str | None = None
    acquired_at: str | None = None

    @property
    def is_local(self) -> bool | None:
        """True/False when the recorded host is comparable, None when not."""

        host = _hostname()
        if not self.hostname or not host:
            return None
        return self.hostname == host

    @property
    def is_running(self) -> bool | None:
        """True/False when liveness is observable, None when it is not."""

        if self.pid is None or self.pid <= 0 or self.is_local is not True:
            return None
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # running, owned by another user
            return True
        except OSError:  # pragma: no cover - defensive
            return None
        return True

    def live_command(self) -> str | None:
        """Read the holder's current command line, when this host can see it."""

        if self.pid is None or self.is_local is not True:
            return None
        return _read_proc_cmdline(self.pid)

    def describe(self) -> str:
        if self.pid is None:
            return "owner identity unavailable (the lock carries no holder record)"
        fragments = [f"held by PID {self.pid}"]
        command = self.live_command() or self.command
        if command:
            fragments.append(f"({command})")
        if self.hostname:
            fragments.append(f"on host {self.hostname}")
        if self.acquired_at:
            fragments.append(f"since {self.acquired_at}")
        description = " ".join(fragments)
        if self.is_running is False:
            description += (
                " — but that PID is no longer running, so the lock is held by a "
                "different process: either this directory is shared with another "
                "host, or the record is stale while a live process still owns it"
            )
        return description


def read_control_lock_holder(path: str | Path) -> ControlLockHolder | None:
    """Return the recorded holder of ``path``, or ``None`` when unavailable.

    Never raises: a missing, empty, truncated, or corrupt record simply means
    the owner cannot be named.
    """

    try:
        with open(path, "rb") as handle:
            raw = handle.read(_HOLDER_RECORD_LIMIT)
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        record = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    return ControlLockHolder(
        pid=pid if isinstance(pid, int) and pid > 0 else None,
        hostname=_clean_field(record.get("hostname")),
        command=_clean_field(record.get("command")),
        acquired_at=_clean_field(record.get("acquired_at")),
    )


def _scan_proc_for_holder(path: Path) -> ControlLockHolder | None:
    """Find the owner by walking /proc when the lock carries no record.

    Covers the two cases the record cannot: a holder started before records
    existed, and a holder whose best-effort record write failed. Only sees
    processes this user may inspect, and never reports the caller itself —
    the caller's own probe descriptor is still open at this point.
    """

    proc = Path("/proc")
    if not proc.is_dir():
        return None
    target = str(path)
    self_pid = os.getpid()
    try:
        entries = list(proc.iterdir())
    except OSError:  # pragma: no cover - defensive
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) != target:
                    continue
            except OSError:
                continue
            return ControlLockHolder(
                pid=pid,
                hostname=_hostname(),
                command=_read_proc_cmdline(pid),
            )
    return None


def probe_control_lock(path: str | Path) -> ControlLockHolder | None:
    """Report the owner of ``path`` without taking ownership of it.

    Returns ``None`` when the Control Database is free. Surfaces use this to
    fail fast with an actionable message before booting a server; the lock
    taken in :meth:`ControlDatabaseLock.acquire` remains the only authority.
    """

    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:  # pragma: no cover - defensive
        pass
    if not resolved.exists():
        return None
    with _PROCESS_LOCK:
        if resolved in _PROCESS_OWNED_PATHS:
            return read_control_lock_holder(resolved) or ControlLockHolder(
                pid=os.getpid()
            )
    try:
        handle = open(resolved, "a+b")
    except OSError:
        return None
    try:
        try:
            _acquire_os_lock_for(handle, resolved)
        except ControlDatabaseOwnedError:
            return (
                read_control_lock_holder(resolved)
                or _scan_proc_for_holder(resolved)
                or ControlLockHolder()
            )
        _release_os_lock(handle)
        return None
    finally:
        handle.close()


class ControlDatabaseLock:
    """Non-blocking OS advisory lock held for repository lifetime."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK:
            if self.path in _PROCESS_OWNED_PATHS:
                raise ControlDatabaseOwnedError(
                    f"Control Database is already owned by this process "
                    f"(PID {os.getpid()}): {self.path}\n"
                    "  A process must not open one Control Database twice — "
                    "reuse the ControlRuntime that already owns it."
                )
            handle = self.path.open("a+b")
            try:
                self._acquire_os_lock(handle)
            except Exception:
                handle.close()
                raise
            self._handle = handle
            _PROCESS_OWNED_PATHS.add(self.path)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        _write_holder_record(handle)

    def _acquire_os_lock(self, handle: BinaryIO) -> None:
        _acquire_os_lock_for(handle, self.path)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        with _PROCESS_LOCK:
            try:
                _clear_holder_record(handle)
                _release_os_lock(handle)
            finally:
                handle.close()
                self._handle = None
                _PROCESS_OWNED_PATHS.discard(self.path)

    def __enter__(self) -> "ControlDatabaseLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _acquire_os_lock_for(handle: BinaryIO, path: Path) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        if msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
    except OSError as exc:
        raise _owned_error(path) from exc
    raise ControlDatabaseOwnedError(
        "No supported cross-process file-lock implementation is available"
    )


def _release_os_lock(handle: BinaryIO) -> None:
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _owned_error(path: Path) -> ControlDatabaseOwnedError:
    """Name the owner in the error, so the message is actionable on sight."""

    holder = read_control_lock_holder(path) or _scan_proc_for_holder(path)
    owner = holder.describe() if holder is not None else ControlLockHolder().describe()
    return ControlDatabaseOwnedError(
        f"Control Database is already owned: {path}\n"
        f"  {owner}\n"
        f"  {_REMEDIATION}"
    )


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - hostname lookup is not critical
        return ""


def _read_proc_cmdline(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [part for part in raw.split(b"\0") if part]
    if not parts:
        return None
    return _clean_field(" ".join(part.decode("utf-8", "replace") for part in parts))


def _clean_field(value: object) -> str | None:
    """Collapse to a single line so the owner always renders on one row."""

    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:_HOLDER_COMMAND_LIMIT]
    return cleaned or None


def _holder_payload() -> bytes:
    hostname = _hostname()
    command = _clean_field(" ".join(sys.argv)) or ""
    try:
        acquired_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError):  # pragma: no cover - defensive
        acquired_at = ""
    return json.dumps(
        {
            "pid": os.getpid(),
            "hostname": hostname,
            "command": command,
            "acquired_at": acquired_at,
        }
    ).encode("utf-8")


def _write_holder_record(handle: BinaryIO) -> None:
    """Stamp the owner into the lock file. Diagnostics only — never fatal."""

    with contextlib.suppress(OSError, ValueError):
        handle.seek(0)
        handle.truncate()
        handle.write(_holder_payload())
        handle.flush()


def _clear_holder_record(handle: BinaryIO) -> None:
    with contextlib.suppress(OSError, ValueError):
        handle.seek(0)
        handle.truncate()
        handle.flush()


__all__ = [
    "ControlDatabaseLock",
    "ControlLockHolder",
    "probe_control_lock",
    "read_control_lock_holder",
]
