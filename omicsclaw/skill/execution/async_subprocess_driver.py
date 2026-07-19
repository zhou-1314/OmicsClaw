"""Async sibling of :mod:`subprocess_driver` for the executor path.

OMI-12 audit P1 #4: ``SkillRunnerExecutor`` used to wrap the blocking
``run_skill`` call in ``asyncio.to_thread``, which parked one
ThreadPoolExecutor worker per active skill. With the default 32-worker
pool, a busy desktop-server running long skills (velocity, pseudotime, …)
could exhaust the pool and stall unrelated async work.

This module replaces that thread-blocking path with a native
``asyncio.create_subprocess_exec`` driver that exposes the same outcome
shape as the sync ``drive_subprocess`` (a :class:`subprocess.CompletedProcess`
with the status-field-aware return code logic from P1 #2). The sync
driver stays in place for the CLI / bot / pipeline-runner paths that
already run inside their own threads or processes; this driver is the
async-native counterpart for callers that already live in an asyncio
event loop.

Behaviour parity with the sync driver:

- Compatibility execution uses a POSIX session or Windows process group.
- Strict canonical Run execution uses a Linux user-systemd scope, whose cgroup
  still owns descendants that call ``setsid()``. Other platforms fail closed
  before the target is spawned until an equivalent ownership Adapter exists.
- Cancel path: ``asyncio.CancelledError`` raised on the awaiting task
  flows into SIGTERM → wait ``_CANCEL_GRACE_SECONDS`` → SIGKILL → re-raise.
- Status field / ``-9 → 0`` heuristic from :func:`read_result_status`
  applies identically.

Behaviour deltas vs the sync driver (small + documented):

- Cancellation comes from ``asyncio.CancelledError`` rather than a
  ``threading.Event`` poll loop. asyncio cancellation is decisive; we
  never reclassify a cancelled run as success regardless of any partial
  ``status: ok`` left on disk.
- Per-line ``stdout_callback`` / ``stderr_callback`` are not supported
  here — the executor sink writes the aggregated output to
  ``ctx.stdout_log`` after the process exits. The bot path that needs
  per-line streaming continues to use the sync ``run_skill`` directly.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
from typing import Literal
from pathlib import Path, PurePosixPath

from omicsclaw.common.report import load_result_json, read_result_status
from omicsclaw.skill.execution.environment import scrub_internal_control_credentials


_CANCEL_GRACE_SECONDS = 5.0
_SYSTEMD_HELPER_TIMEOUT_SECONDS = 5.0
_NORMAL_SCOPE_SETTLE_SECONDS = 1.0
_TREE_CONFIRM_SECONDS = 5.0
_TREE_POLL_SECONDS = 0.05
_CGROUP_ROOT = next(
    (
        candidate
        for candidate in (
            Path("/sys/fs/cgroup"),
            Path("/sys/fs/cgroup/unified"),
        )
        if (candidate / "cgroup.controllers").is_file()
    ),
    Path("/sys/fs/cgroup"),
)
SYSTEMD_USER_SCOPE_REFERENCE_TYPE = "linux-user-systemd-bwrap-v1"
_SCOPE_UNIT_RE = re.compile(r"omicsclaw-run-[0-9a-f]{24}\.scope\Z")


class ProcessTreeStopUnconfirmed(RuntimeError):
    """The executor cannot prove that every owned descendant has stopped."""


def new_governed_process_tree_reference() -> tuple[str, str]:
    """Create the opaque ownership reference persisted with an Assignment."""

    if not governed_process_tree_supported():
        raise RuntimeError(
            "governed process-tree ownership is unavailable on this platform"
        )
    return (
        SYSTEMD_USER_SCOPE_REFERENCE_TYPE,
        f"omicsclaw-run-{secrets.token_hex(12)}.scope",
    )


def _validate_scope_unit(unit: str) -> str:
    if not isinstance(unit, str) or _SCOPE_UNIT_RE.fullmatch(unit) is None:
        raise ProcessTreeStopUnconfirmed(
            "persisted governed process-tree reference is invalid"
        )
    return unit


def governed_process_tree_supported() -> bool:
    """Whether this host has a confirmable async executor ownership Adapter."""

    if not sys.platform.startswith("linux"):
        return False
    if (
        shutil.which("systemd-run") is None
        or shutil.which("systemctl") is None
        or shutil.which("bwrap") is None
        or not (_CGROUP_ROOT / "cgroup.controllers").is_file()
    ):
        return False
    runtime_dir = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    session_bus = str(os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "").strip()
    return bool(session_bus or (runtime_dir and (Path(runtime_dir) / "bus").exists()))


def _systemd_scope_terminal(state: str | None) -> bool:
    return state is None or state in {"inactive", "failed"}


async def _communicate_systemd_helper(
    helper: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(
            helper.communicate(),
            timeout=_SYSTEMD_HELPER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        try:
            helper.kill()
        except ProcessLookupError:
            pass
        await helper.wait()
        raise ProcessTreeStopUnconfirmed("systemd ownership helper timed out") from exc
    except asyncio.CancelledError:
        try:
            helper.kill()
        except ProcessLookupError:
            pass
        cleanup = asyncio.create_task(helper.wait())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise


async def _systemd_scope_state(unit: str) -> str | None:
    value = await _systemd_scope_property(unit, "ActiveState")
    if value is None:
        return None
    state = value.lower()
    if state:
        return state
    raise ProcessTreeStopUnconfirmed("systemd scope returned no active state")


async def _systemd_scope_property(unit: str, property_name: str) -> str | None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise ProcessTreeStopUnconfirmed("systemctl disappeared during execution")
    helper = await asyncio.create_subprocess_exec(
        systemctl,
        "--user",
        "show",
        unit,
        f"--property={property_name}",
        "--value",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=scrub_internal_control_credentials(os.environ),
    )
    stdout, stderr = await _communicate_systemd_helper(helper)
    if helper.returncode == 0:
        return stdout.decode("utf-8", errors="replace").strip()
    detail = stderr.decode("utf-8", errors="replace").lower()
    if "could not be found" in detail or "not found" in detail:
        return None
    raise ProcessTreeStopUnconfirmed("systemd scope state could not be observed")


async def _systemd_scope_empty(unit: str, state: str | None = None) -> bool:
    """Require both terminal unit state and an unpopulated cgroup."""

    observed_state = await _systemd_scope_state(unit) if state is None else state
    if observed_state is None:
        # The parent-death launcher prevents a dead Backend's helper from
        # publishing this pre-generated unit after recovery observes absence.
        return True
    if not _systemd_scope_terminal(observed_state):
        return False
    control_group = await _systemd_scope_property(unit, "ControlGroup")
    if control_group is None or not control_group:
        return True
    relative_group = PurePosixPath(control_group)
    if not relative_group.is_absolute() or ".." in relative_group.parts:
        raise ProcessTreeStopUnconfirmed("governed systemd cgroup path is invalid")
    events_path = _CGROUP_ROOT.joinpath(*relative_group.parts[1:]) / "cgroup.events"
    try:
        fields = dict(
            line.split(maxsplit=1)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except (OSError, ValueError) as exc:
        raise ProcessTreeStopUnconfirmed(
            "governed systemd cgroup occupancy could not be observed"
        ) from exc
    populated = fields.get("populated")
    if populated not in {"0", "1"}:
        raise ProcessTreeStopUnconfirmed(
            "governed systemd cgroup returned invalid occupancy"
        )
    return populated == "0"


async def _wait_for_systemd_scope_exit(unit: str, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await _systemd_scope_empty(unit):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_TREE_POLL_SECONDS)


async def _signal_systemd_scope(unit: str, *, signal_name: str) -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise ProcessTreeStopUnconfirmed("systemctl disappeared during execution")
    helper = await asyncio.create_subprocess_exec(
        systemctl,
        "--user",
        "kill",
        "--kill-who=all",
        f"--signal={signal_name}",
        unit,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=scrub_internal_control_credentials(os.environ),
    )
    await _communicate_systemd_helper(helper)
    outcome = helper.returncode
    if outcome != 0 and not await _systemd_scope_empty(unit):
        raise ProcessTreeStopUnconfirmed("systemd scope could not be signaled")


async def _terminate_systemd_scope_unit(unit: str) -> bool:
    """Stop one persisted scope and prove that the user manager released it."""

    unit = _validate_scope_unit(unit)
    state = await _systemd_scope_state(unit)
    if await _systemd_scope_empty(unit, state):
        return False
    await _signal_systemd_scope(unit, signal_name="SIGTERM")
    if not await _wait_for_systemd_scope_exit(unit, timeout=_CANCEL_GRACE_SECONDS):
        await _signal_systemd_scope(unit, signal_name="SIGKILL")
        if not await _wait_for_systemd_scope_exit(unit, timeout=_TREE_CONFIRM_SECONDS):
            raise ProcessTreeStopUnconfirmed(
                "governed systemd scope did not become empty"
            )
    return True


async def reconcile_governed_process_tree(
    reference_type: str,
    reference: str,
) -> bool:
    """Stop and verify a durable execution owner during close or restart."""

    if reference_type != SYSTEMD_USER_SCOPE_REFERENCE_TYPE:
        raise ProcessTreeStopUnconfirmed(
            "persisted governed process-tree reference type is unsupported"
        )
    if not governed_process_tree_supported():
        raise ProcessTreeStopUnconfirmed(
            "governed process-tree ownership cannot be observed on this host"
        )
    return await _terminate_systemd_scope_unit(reference)


async def _terminate_systemd_scope(
    proc: asyncio.subprocess.Process,
    unit: str,
) -> bool:
    """Stop and confirm one cgroup-backed user-systemd scope."""

    state = await _systemd_scope_state(unit)
    if state is None and proc.returncode is None:
        # Cancellation can arrive between create_subprocess_exec() and the
        # user manager publishing the new scope. Wait briefly for either fact
        # before deciding that the target never started.
        deadline = asyncio.get_running_loop().time() + 1.0
        while state is None and proc.returncode is None:
            if asyncio.get_running_loop().time() >= deadline:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (PermissionError, OSError) as exc:
                    raise ProcessTreeStopUnconfirmed(
                        "systemd scope registration could not be contained"
                    ) from exc
                await proc.wait()
                state = await _systemd_scope_state(unit)
                break
            await asyncio.sleep(_TREE_POLL_SECONDS)
            state = await _systemd_scope_state(unit)
    if await _systemd_scope_empty(unit, state):
        if proc.returncode is None:
            await proc.wait()
        return False
    await _terminate_systemd_scope_unit(unit)
    if proc.returncode is None:
        await proc.wait()
    return True


async def _read_stream(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    return await stream.read()


def _posix_process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def _wait_for_posix_group_exit(pgid: int, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _posix_process_group_exists(pgid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_TREE_POLL_SECONDS)
    return True


async def _terminate_posix_process_group(
    proc: asyncio.subprocess.Process,
) -> bool:
    """Stop and confirm the whole POSIX process group, not just its leader."""

    pgid = proc.pid
    if not _posix_process_group_exists(pgid):
        if proc.returncode is None:
            await proc.wait()
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        if proc.returncode is None:
            await proc.wait()
        return False
    except (PermissionError, OSError) as exc:
        raise RuntimeError("cannot terminate governed POSIX process group") from exc

    if not await _wait_for_posix_group_exit(pgid, timeout=_CANCEL_GRACE_SECONDS):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as exc:
            raise RuntimeError("cannot kill governed POSIX process group") from exc
        if proc.returncode is None:
            await proc.wait()
        if not await _wait_for_posix_group_exit(pgid, timeout=_TREE_CONFIRM_SECONDS):
            raise RuntimeError("governed POSIX process group did not stop")
    elif proc.returncode is None:
        await proc.wait()
    return True


async def _taskkill_windows_tree(pid: int, *, force: bool) -> int:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    helper = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=scrub_internal_control_credentials(os.environ),
    )
    return await helper.wait()


async def _terminate_windows_process_tree(
    proc: asyncio.subprocess.Process,
) -> bool:
    """Use Windows' tree-aware taskkill command before releasing ownership."""

    if proc.returncode is not None:
        return False
    try:
        await _taskkill_windows_tree(proc.pid, force=False)
    except (FileNotFoundError, OSError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_CANCEL_GRACE_SECONDS)
    except TimeoutError:
        try:
            outcome = await _taskkill_windows_tree(proc.pid, force=True)
        except (FileNotFoundError, OSError):
            proc.kill()
            outcome = 0
        await proc.wait()
        if outcome not in {0, 128}:
            raise RuntimeError("governed Windows process tree did not stop")
    return True


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> bool:
    if os.name == "nt":
        return await _terminate_windows_process_tree(proc)
    return await _terminate_posix_process_group(proc)


async def _finish_cleanup_despite_cancellation(
    cleanup: asyncio.Task[bool],
) -> bool:
    """Repeated Task cancellation cannot interrupt process-tree cleanup."""

    while True:
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            if cleanup.done():
                return cleanup.result()


async def adrive_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    out_dir: Path,
    require_process_tree_proof: bool = False,
    governed_execution_reference: str | None = None,
    stdio: Literal["capture", "devnull"] = "capture",
) -> subprocess.CompletedProcess:
    """Spawn ``cmd`` via ``asyncio.create_subprocess_exec``, return a
    ``CompletedProcess`` with the same status-field / SIGKILL-heuristic
    semantics as the sync :func:`drive_subprocess`.

    Raises :class:`asyncio.CancelledError` when the awaiting task is
    cancelled; the child's process group is SIGTERM'd (then SIGKILL'd
    after the grace period) before the exception propagates.

    ``stdio="capture"`` preserves the legacy aggregate-output contract.
    Security-sensitive IPC workers may select ``stdio="devnull"`` so their
    non-protocol diagnostics can neither be retained without a byte bound nor
    escape into a caller-owned log/result.  The default intentionally remains
    capture for existing Skill executor callers.
    """
    if stdio not in {"capture", "devnull"}:
        raise ValueError("unsupported async subprocess stdio mode")
    if governed_execution_reference is not None and not require_process_tree_proof:
        raise ValueError(
            "governed execution reference requires process-tree proof mode"
        )
    if require_process_tree_proof and not governed_process_tree_supported():
        raise RuntimeError(
            "governed process-tree ownership is unavailable on this platform"
        )

    process_options: dict[str, int | bool] = {}
    scope_unit: str | None = None
    spawn_cmd = list(cmd)
    if require_process_tree_proof:
        systemd_run = shutil.which("systemd-run")
        bubblewrap = shutil.which("bwrap")
        if (
            systemd_run is None or bubblewrap is None
        ):  # guarded above, defensive against PATH mutation
            raise RuntimeError(
                "governed process-tree ownership is unavailable on this platform"
            )
        if governed_execution_reference is None:
            _, scope_unit = new_governed_process_tree_reference()
        else:
            scope_unit = _validate_scope_unit(governed_execution_reference)
        scope_name = scope_unit.removesuffix(".scope")
        sandbox_cmd = [
            bubblewrap,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/run/user",
            "--bind",
            str(out_dir.resolve(strict=True)),
            str(out_dir.resolve(strict=True)),
        ]
        raw_temp = str(env.get("TMPDIR") or "").strip()
        if raw_temp:
            temp_dir = Path(raw_temp).resolve(strict=True)
            if temp_dir != out_dir.resolve(strict=True):
                sandbox_cmd.extend(["--bind", str(temp_dir), str(temp_dir)])
        sandbox_cmd.extend(
            [
                "--unsetenv",
                "DBUS_SESSION_BUS_ADDRESS",
                "--unsetenv",
                "XDG_RUNTIME_DIR",
                "--chdir",
                str(cwd.resolve(strict=True)),
                "--",
                *cmd,
            ]
        )
        governed_command = [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            f"--unit={scope_name}",
            "--",
            *sandbox_cmd,
        ]
        launcher = Path(__file__).with_name("governed_launcher.py").resolve(strict=True)
        spawn_cmd = [
            sys.executable,
            str(launcher),
            str(os.getpid()),
            "--",
            *governed_command,
        ]
    if os.name == "nt":
        process_options["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        process_options["start_new_session"] = True
    stdio_target = (
        asyncio.subprocess.PIPE
        if stdio == "capture"
        else asyncio.subprocess.DEVNULL
    )
    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *spawn_cmd,
            stdout=stdio_target,
            stderr=stdio_target,
            cwd=str(cwd),
            env=scrub_internal_control_credentials(env),
            **process_options,
        )
    )
    spawn_canceled = False
    try:
        while True:
            try:
                proc = await asyncio.shield(spawn_task)
                break
            except asyncio.CancelledError:
                spawn_canceled = True
                if spawn_task.done():
                    proc = spawn_task.result()
                    break
    except BaseException:
        # The unit identity exists before the spawn syscall. If the spawn
        # awaiter fails after systemd accepted the scope but before Python
        # receives a process handle, the durable owner still gives us a
        # cleanup target; never let that narrow window escape reconciliation.
        if scope_unit is not None:
            await _finish_cleanup_despite_cancellation(
                asyncio.create_task(_terminate_systemd_scope_unit(scope_unit))
            )
        if spawn_canceled:
            raise asyncio.CancelledError
        raise

    stdout_reader = asyncio.create_task(_read_stream(proc.stdout))
    stderr_reader = asyncio.create_task(_read_stream(proc.stderr))
    cancelled = False
    process_tree_violation = False
    try:
        if spawn_canceled:
            raise asyncio.CancelledError
        await proc.wait()
        if scope_unit is not None:
            if not await _wait_for_systemd_scope_exit(
                scope_unit,
                timeout=_NORMAL_SCOPE_SETTLE_SECONDS,
            ):
                process_tree_violation = await _terminate_systemd_scope(
                    proc, scope_unit
                )
        elif os.name != "nt" and _posix_process_group_exists(proc.pid):
            process_tree_violation = await _terminate_process_tree(proc)
        stdout_bytes, stderr_bytes = await asyncio.gather(
            stdout_reader,
            stderr_reader,
        )
    except asyncio.CancelledError:
        cancelled = True

        async def stop_and_drain() -> bool:
            try:
                if scope_unit is not None:
                    return await _terminate_systemd_scope(proc, scope_unit)
                return await _terminate_process_tree(proc)
            finally:
                await asyncio.gather(
                    stdout_reader,
                    stderr_reader,
                    return_exceptions=True,
                )

        await _finish_cleanup_despite_cancellation(
            asyncio.create_task(stop_and_drain())
        )
        raise
    finally:
        if not cancelled and proc.returncode is None:
            # Defensive: any other exit path should still reap the child
            # so a half-spawned process doesn't outlive the executor.
            cleanup = (
                _terminate_systemd_scope(proc, scope_unit)
                if scope_unit is not None
                else _terminate_process_tree(proc)
            )
            await _finish_cleanup_despite_cancellation(asyncio.create_task(cleanup))
        if not cancelled:
            pending_readers = tuple(
                reader for reader in (stdout_reader, stderr_reader) if not reader.done()
            )
            for reader in pending_readers:
                reader.cancel()
            if pending_readers:
                await asyncio.gather(*pending_readers, return_exceptions=True)

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return_code = proc.returncode if proc.returncode is not None else -1

    if process_tree_violation:
        stderr = (
            stderr + "\n" if stderr else ""
        ) + "Skill process tree outlived its leader and was terminated."
        return subprocess.CompletedProcess(cmd, 1, stdout, stderr)

    # Status field + ``-9 → 0`` fallback — same semantics as the sync
    # driver. Cancellation never gets here (we re-raised above).
    status = read_result_status(out_dir)
    if status == "ok":
        return_code = 0
    elif status in ("partial", "failed"):
        if return_code == 0:
            return_code = 1
    elif return_code == -9 and load_result_json(out_dir) is not None:
        return_code = 0

    return subprocess.CompletedProcess(cmd, return_code, stdout, stderr)


__all__ = [
    "ProcessTreeStopUnconfirmed",
    "SYSTEMD_USER_SCOPE_REFERENCE_TYPE",
    "adrive_subprocess",
    "governed_process_tree_supported",
    "new_governed_process_tree_reference",
    "reconcile_governed_process_tree",
]
