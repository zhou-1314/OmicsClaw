"""Diagnosability of the Control Database lifetime lock.

The lock itself is covered in ``test_repository.py``; these tests pin the part
an operator actually reads when a second Backend refuses to start — whether the
error names the process that owns the Control Database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omicsclaw.control import (
    ControlDatabaseOwnedError,
    ControlStateRepository,
    probe_control_lock,
    read_control_lock_holder,
)
from omicsclaw.control.locking import ControlDatabaseLock

ROOT = Path(__file__).resolve().parents[2]

_HOLDER_SOURCE = """
import sys, time
from omicsclaw.control import ControlStateRepository

repository = ControlStateRepository(sys.argv[1])
print("HOLDING", flush=True)
time.sleep(120)
"""


class _Holder:
    """A separate process owning one Control Database for a test's duration."""

    def __init__(self, state_root: Path) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_SOURCE, str(state_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(ROOT),
        )
        assert self._process.stdout is not None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = self._process.stdout.readline()
            if "HOLDING" in line:
                self.pid = self._process.pid
                return
            if self._process.poll() is not None:
                raise AssertionError(
                    f"holder exited early: {line}{self._process.stdout.read()}"
                )
        raise AssertionError("holder never acquired the Control Database lock")

    def close(self) -> None:
        self._process.kill()
        self._process.wait()


@pytest.fixture
def holder(tmp_path):
    owner = _Holder(tmp_path)
    try:
        yield owner
    finally:
        owner.close()


def test_owned_error_names_the_holding_process(tmp_path, holder):
    with pytest.raises(ControlDatabaseOwnedError) as caught:
        ControlStateRepository(tmp_path)

    message = str(caught.value)
    assert str(holder.pid) in message
    assert "OMICSCLAW_CONTROL_STATE_ROOT" in message
    # Deleting the lock file is the intuitive-but-corrupting move; the message
    # has to talk the operator out of it.
    assert "does not release the lock" in message


def test_holder_record_identifies_the_owner(tmp_path, holder):
    recorded = read_control_lock_holder(tmp_path / "control.lock")

    assert recorded is not None
    assert recorded.pid == holder.pid
    assert recorded.is_running is True
    assert str(holder.pid) in recorded.describe()


def test_probe_reports_the_holder_without_taking_ownership(tmp_path, holder):
    probed = probe_control_lock(tmp_path / "control.lock")

    assert probed is not None
    assert probed.pid == holder.pid
    # Probing must not have claimed the lock for this process either.
    with pytest.raises(ControlDatabaseOwnedError):
        ControlStateRepository(tmp_path)


def test_probe_returns_none_when_the_control_database_is_free(tmp_path):
    lock_path = tmp_path / "control.lock"

    assert probe_control_lock(lock_path) is None  # never created

    repository = ControlStateRepository(tmp_path)
    repository.close()

    assert probe_control_lock(lock_path) is None  # created, released
    # A probe of a free lock must leave it acquirable.
    reopened = ControlStateRepository(tmp_path)
    reopened.close()


def test_probe_does_not_disturb_a_lock_this_process_holds(tmp_path):
    repository = ControlStateRepository(tmp_path)
    try:
        probed = probe_control_lock(tmp_path / "control.lock")

        assert probed is not None
        assert probed.pid == os.getpid()
        # The probe borrows a second descriptor for the same file; flock treats
        # those independently, so the original lock must survive it.
        with pytest.raises(ControlDatabaseOwnedError):
            ControlStateRepository(tmp_path)
    finally:
        repository.close()


def test_reopening_in_the_same_process_says_so(tmp_path):
    repository = ControlStateRepository(tmp_path)
    try:
        with pytest.raises(ControlDatabaseOwnedError) as caught:
            ControlStateRepository(tmp_path)

        message = str(caught.value)
        assert "already owned by this process" in message
        assert str(os.getpid()) in message
    finally:
        repository.close()


def test_release_clears_the_holder_record(tmp_path):
    lock_path = tmp_path / "control.lock"
    repository = ControlStateRepository(tmp_path)
    assert read_control_lock_holder(lock_path) is not None

    repository.close()

    assert read_control_lock_holder(lock_path) is None


def test_a_corrupt_holder_record_never_blocks_acquisition(tmp_path):
    lock_path = tmp_path / "control.lock"
    lock_path.write_bytes(b"not json at all \xff\xfe")

    lock = ControlDatabaseLock(lock_path)
    lock.acquire()
    try:
        # The record is advisory: a corrupt one is replaced, never trusted.
        recorded = read_control_lock_holder(lock_path)
        assert recorded is not None
        assert recorded.pid == os.getpid()
    finally:
        lock.release()


def test_a_holder_record_for_a_dead_process_is_flagged(tmp_path):
    lock_path = tmp_path / "control.lock"
    dead_pid = _find_unused_pid()
    lock_path.write_text(
        f'{{"pid": {dead_pid}, "hostname": "{os.uname().nodename}", '
        '"command": "oc desktop-server", "acquired_at": "2026-01-01T00:00:00+00:00"}'
    )

    recorded = read_control_lock_holder(lock_path)

    assert recorded is not None
    assert recorded.is_running is False
    assert "no longer running" in recorded.describe()


def _find_unused_pid() -> int:
    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise AssertionError("could not find an unused PID")
