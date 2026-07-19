"""Lifetime ownership lock for one local Control Database."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
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
                    f"Control Database is already owned: {self.path}"
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

    def _acquire_os_lock(self, handle: BinaryIO) -> None:
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
            raise ControlDatabaseOwnedError(
                f"Control Database is already owned: {self.path}"
            ) from exc
        raise ControlDatabaseOwnedError(
            "No supported cross-process file-lock implementation is available"
        )

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        with _PROCESS_LOCK:
            try:
                if fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    with contextlib.suppress(OSError):
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()
                self._handle = None
                _PROCESS_OWNED_PATHS.discard(self.path)

    def __enter__(self) -> "ControlDatabaseLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["ControlDatabaseLock"]
