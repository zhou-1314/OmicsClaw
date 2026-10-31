"""Manage one Python or R kernel and consume its output.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Final, Protocol, runtime_checkable

from queue import Empty

from .r_launch import RegisteredRLaunch

__all__ = [
    "CellResult",
    "KernelCallbacks",
    "KernelSession",
    "KERNEL_NAME",
    "OutputLimits",
    "SessionKernel",
    "SpawnWrapper",
    "figure_basename",
]

_log = logging.getLogger(__name__)

_KERNEL_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {
        "OMICSCLAW_REMOTE_AUTH_TOKEN",
        "OMICSCLAW_API_KEY",
        "LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_API_KEY",
        "MOONSHOT_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "SILICONFLOW_API_KEY",
        "VOLCENGINE_API_KEY",
        "ZHIPU_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "FEISHU_APP_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "DISCORD_BOT_TOKEN",
        "DINGTALK_CLIENT_SECRET",
        "QQ_APP_SECRET",
        "SSH_AUTH_SOCK",
    }
)


@runtime_checkable
class SpawnWrapper(Protocol):
    """Structural protocol for wrapping kernel processes in an OS sandbox."""

    @property
    def kernel_transport(self) -> str:
        """``"ipc"`` (POSIX Unix sockets) or ``"tcp"`` (Windows TCP loopback)."""
        ...

    def wrap(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        extra_read_paths: Sequence[Path] = (),
        extra_write_paths: Sequence[Path] = (),
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(wrapped_argv, child_env)``."""
        ...


KERNEL_NAME: Final = "omicsclaw-session"
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

STREAM_EMIT_MARKER: Final = (
    "...[stream output reached the 10 MB streaming cap; further output is suppressed]"
)
STREAM_IDLE_AFTER_S: Final = 30.0
"""Output silence that long, while a cell runs, is worth an idle notice."""
TIMEOUT_INTERRUPT_GRACE_S = 5.0


@dataclass(frozen=True, slots=True)
class OutputLimits:
    """The three ceilings, overridable as one object for tests."""

    stream_emit_max: int = 10 * 1024 * 1024
    cell_buffer_max: int = 1024 * 1024
    result_max: int = 1024 * 1024
    figure_count_max: int | None = None
    figure_encoded_max: int | None = None
    ordered_output_max: int = 24576


@dataclass(slots=True)
class FigureRef:
    """One display_data image the cell produced, already on disk."""

    path: str
    mime: str
    artifact_id: str = ""


@dataclass(slots=True)
class CellResult:
    """What one executed cell produced. Never raises on its own."""

    status: str = "ok"
    """``ok | error | interrupted | timeout | dead``."""

    stdout: str = ""
    stderr: str = ""
    error: dict[str, Any] | None = None
    figures: list[FigureRef] = field(default_factory=list)
    execution_count: int | None = None
    execution_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    notice: str = ""
    outputs: list[dict[str, Any]] = field(default_factory=list)
    files_written: list[dict[str, Any]] = field(default_factory=list)
    files_read: list[dict[str, Any]] = field(default_factory=list)
    skill_calls: list[dict[str, Any]] = field(default_factory=list)
    skills_loaded: list[dict[str, Any]] = field(default_factory=list)
    object_versions: list[dict[str, Any]] = field(default_factory=list)
    """A kernel-restart handover notice, prepended to stderr by the manager."""

    @property
    def writes(self) -> list[str]:
        return [str(w.get("path") if isinstance(w, dict) else w) for w in self.files_written if w]

    @property
    def reads(self) -> list[str]:
        return [str(r.get("path") if isinstance(r, dict) else r) for r in self.files_read if r]



@dataclass(slots=True)
class KernelCallbacks:
    """The sync audience of one cell. All fields optional, all sync."""

    on_stream: Callable[[str, str], None] | None = None
    """``(name, text)`` per iopub stream frame, after the emit ceiling."""

    on_display: Callable[[FigureRef], None] | None = None
    """Per captured figure, after it is on disk and registered."""

    on_idle: Callable[[float, str], None] | None = None
    """``(seconds_idle, last_label)`` once per silent gap per cell."""

    on_usage: Callable[[dict[str, Any]], None] | None = None
    """``{wall_s, cpu_s, peak_rss_kb}`` after the cell finished."""
    on_output: Callable[[dict[str, Any]], None] | None = None
    on_timeout_pending: Callable[[float], None] | None = None
    """A protected operation exceeded its interrupt grace and still owns the kernel."""


class _StreamTally:
    """One stream's three ceilings, applied in order."""

    def __init__(self, name: str, limits: OutputLimits) -> None:
        self.name = name
        self._limits = limits
        self._emitted = 0
        self._buffered = 0
        self._dropped = 0
        self._emit_stopped = False
        self._pieces: list[str] = []

    def feed(self, text: str, emit: Callable[[str], None]) -> None:
        if self._emitted < self._limits.stream_emit_max:
            room = self._limits.stream_emit_max - self._emitted
            piece = text if len(text) <= room else text[:room]
            self._emitted += len(piece)
            emit(piece)
            if self._emitted >= self._limits.stream_emit_max:
                emit("\n" + STREAM_EMIT_MARKER + "\n")
                self._emit_stopped = True
        if self._buffered < self._limits.cell_buffer_max:
            room = self._limits.cell_buffer_max - self._buffered
            keep = text if len(text) <= room else text[:room]
            overflow = len(text) - len(keep)
            self._pieces.append(keep)
            self._buffered += len(keep)
            self._dropped += max(0, overflow)
        else:
            self._dropped += len(text)

    @property
    def truncated(self) -> bool:
        return self._dropped > 0 or self._emit_stopped

    def text(self) -> str:
        out = "".join(self._pieces)
        marker = (
            f"\n...({self._dropped} further bytes dropped)\n" if self._dropped else ""
        )
        budget = self._limits.result_max
        if len(out) + len(marker) <= budget:
            return out + marker
        # The drop marker is the one line the caller must not lose to the
        # third ceiling, so it is budgeted for rather than sliced off.
        return out[: max(0, budget - len(marker))] + marker


def _session_slug(session_id: str) -> str:
    """The session id made filename-safe (shared by the basename and the
    seeding scan, so the two can never disagree)."""
    return re.sub(r"[^0-9A-Za-z_.-]", "-", session_id)[:32].strip("-") or "s"



_TRACKED_FILE_EXTS: Final = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".svg", ".pdf",
        ".csv", ".tsv", ".h5ad", ".rds", ".html",
        ".parquet", ".txt", ".json", ".xlsx",
    }
)
_IMAGE_MIMES: Final = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def _scan_workspace_files(
    workspace: Path, *, max_entries: int = 2000
) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    if not workspace.is_dir():
        return out
    seen = 0
    stack: list[tuple[Path, int]] = [(workspace, 0)]
    while stack and seen < max_entries:
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for entry in entries:
            if seen >= max_entries:
                break
            name = entry.name
            if name.startswith(".") or name in {"node_modules", "__pycache__", "venv", "provenance"}:
                continue
            seen += 1
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < 3:
                        stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    ext = Path(name).suffix.lower()
                    if ext in _TRACKED_FILE_EXTS:
                        st = entry.stat(follow_symlinks=False)
                        rel = Path(entry.path).relative_to(workspace).as_posix()
                        out[rel] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
    return out


def figure_basename(session_id: str, number: int) -> str:
    """``kernel_<session>_<N>.png``; *number* is a per-kernel monotonic
    sequence seeded past anything earlier kernels of this session wrote,
    so neither a second cell nor a lazy cold restart overwrites history."""
    return f"kernel_{_session_slug(session_id)}_{number}.png"


def _r_spec_manager() -> Any:
    """The default-dirs spec manager, which is where ``installspec()``
    put the ``ir`` spec (user or system location). Refused loudly when
    it is not there, with the enablement steps in the message."""
    from jupyter_client.kernelspec import KernelSpecManager

    manager = KernelSpecManager()
    if "ir" not in manager.find_kernel_specs():
        raise RuntimeError(
            "language 'r' needs the IRkernel kernelspec; enable it with "
            'R -e \'install.packages("IRkernel", '
            'repos="https://cloud.r-project.org")\' followed by '
            "R -e 'IRkernel::installspec()'"
        )
    return manager


@lru_cache(maxsize=1)
def r_available() -> bool:
    """Whether an ``ir`` kernelspec is installed (cached for /health)."""
    try:
        from jupyter_client.kernelspec import KernelSpecManager

        return "ir" in KernelSpecManager().find_kernel_specs()
    except Exception:  # noqa: BLE001 - health must never raise
        return False


class SessionKernel:
    """One persistent ipykernel owned by one session.

    Constructing is cheap (no process); :meth:`start` launches the kernel
    and blocks until it answers ``kernel_info``. Every blocking method
    expects to run on the manager's worker thread.
    """

    def __init__(
        self,
        session_id: str = "default",
        *,
        workspace: Path | str | None = None,
        cwd: Path | str | None = None,
        python: str | None = None,
        python_isolated: bool = False,
        limits: OutputLimits | None = None,
        on_figure: Callable[[Path, str], tuple[str, str]] | None = None,
        startup_timeout: float = 120.0,
        language: str = "python",
        r_launch: RegisteredRLaunch | None = None,
        spawn_wrapper: SpawnWrapper | None = None,
        env_scrubber: Callable[[Mapping[str, str]], dict[str, str]] | None = None,
        signal_guard_factory: Callable[[int], Callable[[int], bool]] | None = None,
    ) -> None:
        ws_val = workspace if workspace is not None else cwd
        if ws_val is None:
            raise TypeError("SessionKernel.__init__() missing required argument: 'workspace'")
        self.session_id = session_id
        self.workspace = Path(ws_val)
        self.python = python or sys.executable
        self._python_isolated = python_isolated
        self.limits = limits or OutputLimits()
        self._on_figure = on_figure
        self._startup_timeout = startup_timeout
        self.language = language
        self._r_launch = r_launch
        self._spawn_wrapper = spawn_wrapper
        self._env_scrubber = env_scrubber
        self._signal_guard_factory = signal_guard_factory
        self._signal_guard = None
        self._km: Any = None
        self._kc: Any = None
        self._scratch: Path | None = None
        self._ledger_path: Path | None = None
        self._ipc_dir: Path | None = None
        self.started_at = 0.0
        self.cells = 0
        self._interrupted = False
        # Per-kernel monotonic figure number; seeded in start() past
        # whatever earlier kernels of this session already wrote, so the
        # second cell (and the lazy cold restart) never overwrite the
        # first cell's figures — the collision the review proved.
        self._figure_seq = 0

    # ---- lifecycle ----

    @property
    def pid(self) -> int | None:
        provisioner = getattr(self._km, "provisioner", None) if self._km else None
        return getattr(provisioner, "pid", None)

    def is_alive(self) -> bool:
        try:
            return bool(self._km is not None and self._jupyter_sync(self._km.is_alive))
        except Exception:  # noqa: BLE001 - a dead manager object is a dead kernel
            return False

    def start(self) -> None:
        """Launch the kernel; block until it is ready. Idempotent-refusing.

        ``language="r"`` starts the installed ``ir`` (IRkernel) spec on
        the same client and the same polling loop — the primitive is
        language-agnostic; only the process differs.
        """
        if self._km is not None:
            raise RuntimeError("kernel already started")
        from jupyter_client.kernelspec import KernelSpecManager
        from jupyter_client.manager import KernelManager

        slug = (
            re.sub(r"[^0-9A-Za-z_.-]", "-", self.session_id)[:48].strip("-")
            or "session"
        )
        scratch = self.workspace / ".omicsclaw" / "kernel" / slug
        scratch.mkdir(parents=True, exist_ok=True)
        runtime_dir = scratch / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        if self._env_scrubber is not None:
            env = dict(self._env_scrubber(os.environ))
        else:
            env = {
                name: value
                for name, value in os.environ.items()
                if name.upper() not in _KERNEL_CREDENTIAL_NAMES
            }
        env.setdefault("KMP_AFFINITY", "disabled")
        env.setdefault("OMP_NUM_THREADS", "4")

        extra_write_paths: list[Path] = [scratch]
        ipc_prefix: str | None = None
        if (
            self._spawn_wrapper is not None
            and self._spawn_wrapper.kernel_transport == "ipc"
        ):
            candidate_ipc = runtime_dir / "ipc"
            if len(str(candidate_ipc)) <= 84:
                ipc_prefix = str(candidate_ipc)
            else:
                self._ipc_dir = Path(tempfile.mkdtemp(prefix="oc-kipc-"))
                extra_write_paths.append(self._ipc_dir)
                ipc_prefix = str(self._ipc_dir / "ipc")

        if self.language == "r" and self._r_launch is not None:
            # Registered SSH kernels never consult a user or system kernelspec.
            for name in tuple(env):
                if name.startswith(
                    ("R_LIBS", "R_ENVIRON", "R_PROFILE", "PYTHON")
                ) or name in {"R_HOME", "VIRTUAL_ENV", "CONDA_PREFIX"}:
                    env.pop(name)
            env.update(
                R_LIBS_USER=os.pathsep.join(self._r_launch.library_paths),
                R_LIBS_SITE="",
            )
            argv = self._r_launch.argv()
            if self._spawn_wrapper is not None:
                argv, env = self._spawn_wrapper.wrap(
                    argv,
                    cwd=self.workspace,
                    env=env,
                    extra_read_paths=(
                        Path(self._r_launch.executable).parent.parent,
                        *(Path(p) for p in self._r_launch.library_paths),
                    ),
                    extra_write_paths=tuple(extra_write_paths),
                )
            spec_dir = scratch / "kernels" / KERNEL_NAME
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "kernel.json").write_text(
                json.dumps(
                    {"argv": argv, "display_name": "OmicsClaw R", "language": "r"}
                ),
                encoding="utf-8",
            )
            manager = KernelManager(
                kernel_name=KERNEL_NAME,
                kernel_spec_manager=KernelSpecManager(
                    kernel_dirs=[str(scratch / "kernels")], ensure_native_kernel=False
                ),
                connection_file=str(runtime_dir / "kernel.json"),
            )
        elif self.language == "r":
            r_specs = _r_spec_manager()
            if self._spawn_wrapper is None:
                manager = KernelManager(
                    kernel_name="ir",
                    kernel_spec_manager=r_specs,
                    connection_file=str(runtime_dir / "kernel.json"),
                )
            else:
                ir_spec = r_specs.get_kernel_spec("ir")
                if getattr(ir_spec, "env", None):
                    env.update(ir_spec.env)
                wrapped_argv, env = self._spawn_wrapper.wrap(
                    list(ir_spec.argv),
                    cwd=self.workspace,
                    env=env,
                    extra_write_paths=tuple(extra_write_paths),
                )
                spec_dir = scratch / "kernels" / KERNEL_NAME
                spec_dir.mkdir(parents=True, exist_ok=True)
                (spec_dir / "kernel.json").write_text(
                    json.dumps(
                        {
                            "argv": wrapped_argv,
                            "display_name": getattr(ir_spec, "display_name", "R"),
                            "language": "r",
                        }
                    ),
                    encoding="utf-8",
                )
                spec_manager = KernelSpecManager(
                    kernel_dirs=[str(scratch / "kernels")],
                    ensure_native_kernel=False,
                )
                manager = KernelManager(
                    kernel_name=KERNEL_NAME,
                    kernel_spec_manager=spec_manager,
                    connection_file=str(runtime_dir / "kernel.json"),
                )
        else:
            isolated = {
                "IPYTHONDIR": str(scratch / "ipython"),
                "JUPYTER_RUNTIME_DIR": str(runtime_dir),
                "JUPYTER_DATA_DIR": str(scratch / "data"),
                "JUPYTER_CONFIG_DIR": str(scratch / "config"),
            }
            for folder in isolated.values():
                Path(folder).mkdir(parents=True, exist_ok=True)
            self._ledger_path = scratch / "kernel_ledger.jsonl"
            _write_ipython_ledger_startup(
                Path(isolated["IPYTHONDIR"]),
                workspace=self.workspace.resolve(),
                ledger_path=self._ledger_path.resolve(),
            )
            env.update(isolated)
            # A headless host may use Agg; notebook kernels must emit display_data.
            # This startup default still allows cells to select another backend.
            env["MPLBACKEND"] = "module://matplotlib_inline.backend_inline"

            base_argv: list[str] = [
                self.python,
                *(["-I"] if self._python_isolated else []),
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
                "--HistoryManager.enabled=False",
            ]
            if self._spawn_wrapper is not None:
                base_argv, env = self._spawn_wrapper.wrap(
                    base_argv,
                    cwd=self.workspace,
                    env=env,
                    extra_write_paths=tuple(extra_write_paths),
                )

            spec_dir = scratch / "kernels" / KERNEL_NAME
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "kernel.json").write_text(
                json.dumps(
                    {
                        "argv": base_argv,
                        "display_name": "OmicsClaw session",
                        "language": "python",
                    }
                ),
                encoding="utf-8",
            )
            spec_manager = KernelSpecManager(
                kernel_dirs=[str(scratch / "kernels")], ensure_native_kernel=False
            )
            manager = KernelManager(
                kernel_name=KERNEL_NAME,
                kernel_spec_manager=spec_manager,
                connection_file=str(runtime_dir / "kernel.json"),
            )

        if ipc_prefix is not None:
            manager.transport = "ipc"
            manager.ip = ipc_prefix

        # When wrapped inside a POSIX PID namespace (bwrap --unshare-pid /
        # unshare --pid), _bridge.py is PID 1 so os.getppid() == 1 inside the
        # kernel. Passing independent=True prevents jupyter_client from setting
        # the host's JPY_PARENT_PID, which would mismatch PID 1 and trigger
        # ipykernel's ParentPollerUnix immediate shutdown.
        if self.language == "r" and self._r_launch is not None:
            # Wrappers may add environment values; scrub at the last launch boundary.
            env = (
                dict(self._env_scrubber(env))
                if self._env_scrubber
                else {
                    k: v
                    for k, v in env.items()
                    if k.upper() not in _KERNEL_CREDENTIAL_NAMES
                }
            )
            for name in tuple(env):
                if name.startswith(
                    ("R_LIBS", "R_ENVIRON", "R_PROFILE", "PYTHON")
                ) or name in {"R_HOME", "VIRTUAL_ENV", "CONDA_PREFIX"}:
                    env.pop(name)
            env.update(
                R_LIBS_USER=os.pathsep.join(self._r_launch.library_paths),
                R_LIBS_SITE="",
            )
        if self.language == "python" and self._python_isolated:
            # Match the registered probe after wrappers finish composing the launch.
            env = (
                dict(self._env_scrubber(env))
                if self._env_scrubber
                else {
                    k: v
                    for k, v in env.items()
                    if k.upper() not in _KERNEL_CREDENTIAL_NAMES
                }
            )
            for name in tuple(env):
                if name.startswith("PYTHON") or name in {"VIRTUAL_ENV", "CONDA_PREFIX"}:
                    env.pop(name)
        env.pop("JPY_PARENT_PID", None)
        self._jupyter_sync(
            manager.start_kernel,
            env=env,
            cwd=str(self.workspace),
            independent=bool(self._spawn_wrapper is not None and os.name == "posix"),
        )
        lifecycle = getattr(self._spawn_wrapper, "terminate", None)
        if lifecycle is not None:
            manager._omicsclaw_terminate = lifecycle
        if self._signal_guard_factory is not None:
            # Even persistence failure during registration must fence cleanup.
            birth = _linux_birth(manager.provisioner.pid)
            self._signal_guard = lambda pid: (
                birth is not None and _linux_birth(pid) == birth
            )
        client = manager.client()
        client.start_channels()
        try:
            if self._signal_guard_factory is not None:
                self._signal_guard = self._signal_guard_factory(manager.provisioner.pid)
            self._jupyter_sync(client.wait_for_ready, timeout=self._startup_timeout)
        except Exception:
            self._shutdown_client(client)
            self._kill_manager(manager, guard=self._signal_guard)
            self._cleanup_ipc_dir()
            raise
        self._km = manager
        self._kc = client
        self._scratch = scratch
        self.started_at = time.time()
        self._seed_figure_sequence()

    def _cleanup_ipc_dir(self) -> None:
        if self._ipc_dir is not None:
            shutil.rmtree(self._ipc_dir, ignore_errors=True)
            self._ipc_dir = None

    def _seed_figure_sequence(self) -> None:
        """Start figure numbering past this session's earlier kernels.

        A dead kernel's figures stay on disk; a lazily cold-started
        replacement begins at zero like the first one did and would
        write ``kernel_<sess>_1.png`` over the history. Scanning the
        figures directory for the highest existing number makes the
        sequence monotonic across incarnations, not just within one.
        """
        prefix = f"kernel_{_session_slug(self.session_id)}_"
        best = 0
        directory = self.workspace / "figures"
        try:
            entries = list(directory.iterdir()) if directory.is_dir() else []
        except OSError:
            entries = []
        for entry in entries:
            name = entry.name
            if name.startswith(prefix) and name.endswith(".png"):
                tail = name[len(prefix) : -len(".png")]
                if tail.isdigit():
                    best = max(best, int(tail))
        self._figure_seq = best

    def interrupt(self) -> None:
        """Tier 1 of the cancel ladder. Safe from any thread."""
        if self._signal_guard is not None and not self._signal_guard(self.pid):
            return
        self._interrupted = True
        if self._km is not None:
            try:
                self._jupyter_sync(self._km.interrupt_kernel)
            except Exception as exc:  # noqa: BLE001 - interrupting a dead kernel
                _log.debug("interrupt failed for %s: %s", self.session_id, exc)

    @staticmethod
    def _jupyter_sync(operation, *args, **kwargs):
        """Isolate Jupyter's cached loop without changing caller authorization."""
        from jupyter_core.utils import ensure_event_loop

        def invoke():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                try:
                    previous = asyncio.get_event_loop()
                except RuntimeError:
                    previous = None
                loop = ensure_event_loop()
                try:
                    return operation(*args, **kwargs)
                finally:
                    loop.close()
                    asyncio.set_event_loop(previous)
            # Jupyter's own sync bridge handles calls made on a running loop.
            return operation(*args, **kwargs)

        # to_thread copies ContextVars, including Jupyter's cache of a loop
        # running on another thread. Guards and callbacks stay outside this
        # library-only scope, with the caller's authorization context intact.
        return contextvars.Context().run(invoke)

    def kill(self) -> None:
        """SIGKILL the kernel's process group. Safe from any thread."""
        if self._km is not None:
            self._kill_manager(self._km, guard=self._signal_guard)
        self._cleanup_ipc_dir()

    def shutdown(self) -> None:
        """Polite shutdown; falls back to kill."""
        if self._km is None:
            self._cleanup_ipc_dir()
            return
        if self._signal_guard is not None and not self._signal_guard(self.pid):
            self._shutdown_client(self._kc)
            return
        descendants = (
            _descendant_pids(self.pid)
            if (os.name == "posix" and self.pid and self.pid > 0)
            else []
        )
        # Capture each child's birth while the root still proves ownership.
        # A polite shutdown may reap that root before fallback cleanup runs.
        descendant_births = (
            {child: _linux_birth(child) for child in descendants}
            if self._signal_guard is not None
            else None
        )
        try:
            self._shutdown_client(self._kc)
        finally:
            try:
                if self._km.has_kernel and not getattr(
                    self._km, "_omicsclaw_terminate", None
                ):
                    self._jupyter_sync(self._km.shutdown_kernel, now=True)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._kill_manager(
                    self._km,
                    descendants=descendants,
                    descendant_births=descendant_births,
                    guard=self._signal_guard,
                )
                self._km = None
                self._kc = None
                self._cleanup_ipc_dir()

    @staticmethod
    def _shutdown_client(client: Any) -> None:
        try:
            client.stop_channels()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _kill_manager(
        manager: Any,
        *,
        descendants: Sequence[int] | None = None,
        descendant_births=None,
        guard=None,
    ) -> None:
        provisioner = getattr(manager, "provisioner", None)
        pid = getattr(provisioner, "pid", None)
        pgid = getattr(provisioner, "pgid", None)
        root_allowed = guard is None or guard(pid)
        lifecycle = getattr(manager, "_omicsclaw_terminate", None)
        if root_allowed and lifecycle is not None and lifecycle(pid, guard):
            process = getattr(provisioner, "process", None)
            if process is not None:
                with contextlib.suppress(Exception):
                    process.wait(timeout=1)
            return
        if not root_allowed and not descendant_births:
            return
        if os.name != "posix":
            if pid and pid > 0:
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3.0,
                        check=False,
                    )
            return

        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        # Collect descendant PIDs before killing the parent wrapper so
        # sandboxed children (bwrap/unshare -> bridge -> kernel) are all reached.
        if descendants is None:
            descendants = _descendant_pids(pid) if (pid and pid > 0) else []
        births = (
            descendant_births
            if descendant_births is not None
            else (
                {child: _linux_birth(child) for child in descendants} if guard else {}
            )
        )
        try:
            if root_allowed and pgid and pgid > 0 and pgid != os.getpgrp():
                os.killpg(pgid, sig)
            elif root_allowed and pid and pid > 0:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        for child_pid in reversed(descendants):
            if guard is not None and (
                not births.get(child_pid)
                or _linux_birth(child_pid) != births[child_pid]
            ):
                continue
            if child_pid != pid and child_pid > 0 and child_pid != os.getpid():
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(child_pid, sig)

    # ---- resource probes ----

    def peak_rss_kb(self) -> int | None:
        """The kernel tree's high-water RSS in KiB."""
        pid = self.pid
        if not pid or pid <= 0:
            return None
        if sys.platform == "win32":
            return _win_peak_rss_kb(pid)
        pids = _descendant_pids(pid)
        best: int | None = None
        for target_pid in pids:
            try:
                for line in Path(f"/proc/{target_pid}/status").read_text().splitlines():
                    if line.startswith("VmHWM:"):
                        val = int(line.split()[1])
                        best = val if best is None else max(best, val)
                        break
            except (OSError, ValueError, IndexError):
                continue
        return best

    def cpu_seconds(self) -> float | None:
        """Total CPU seconds across the kernel and its sandbox wrapper tree."""
        pid = self.pid
        if not pid or pid <= 0:
            return None
        if sys.platform == "win32":
            return _win_cpu_seconds(pid)
        try:
            clk_tck = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            return None
        pids = _descendant_pids(pid)
        total_ticks = 0
        found = False
        for target_pid in pids:
            try:
                raw = Path(f"/proc/{target_pid}/stat").read_text()
                rparen = raw.rfind(")")
                if rparen == -1:
                    continue
                after_comm = raw[rparen + 2 :].split()
                # after_comm[0] is state (field 3); utime is field 14 (index 11), stime is field 15 (index 12)
                total_ticks += int(after_comm[11]) + int(after_comm[12])
                found = True
            except (OSError, ValueError, IndexError):
                continue
        return (total_ticks / clk_tck) if found else None

    # ---- execution ----

    def execute(
        self,
        code: str,
        *,
        timeout_s: float = 1800.0,
        callbacks: KernelCallbacks | None = None,
        store_history: bool = True,
        preserve_on_timeout: bool = False,
        execution_id: str = "",
    ) -> CellResult:
        """Run one cell to ``status: idle`` and return its outcome.

        The wall clock starts before the request is sent and covers the
        whole drain, per the plan's rule that a default timeout is
        retained (30 min) with idle detection beside it. On deadline the
        ladder is started here — interrupt, then a short grace — and the
        result carries ``status="timeout"`` for the manager to finish.
        Protected internal operations instead keep draining until idle or
        confirmed process death; their deadline never authorizes a kernel reset.
        """
        callbacks = callbacks or KernelCallbacks()
        from .outputs import OrderedOutputs
        from .outputs import text as bounded_text

        ordered = OrderedOutputs(
            limit=self.limits.ordered_output_max, sink=callbacks.on_output
        )
        eid = str(execution_id or "").strip() or uuid.uuid4().hex
        result = CellResult(execution_id=eid)
        tallies = {
            "stdout": _StreamTally("stdout", self.limits),
            "stderr": _StreamTally("stderr", self.limits),
        }
        figures: list[FigureRef] = []
        error: dict[str, Any] | None = None
        began = time.monotonic()
        last_output = began
        idle_noticed = False

        def _emit(name: str) -> Callable[[str], None]:
            sink = callbacks.on_stream

            def forward(text: str) -> None:
                if sink is not None:
                    try:
                        sink(name, text)
                    except Exception:  # noqa: BLE001 - an audience must not fail the cell
                        pass

            return forward

        self._interrupted = False
        before_files = _scan_workspace_files(self.workspace) if store_history else {}
        ledger_offset = (
            self._ledger_path.stat().st_size
            if (store_history and self._ledger_path is not None and self._ledger_path.is_file())
            else 0
        )
        if store_history and self._scratch is not None:
            with contextlib.suppress(OSError):
                (self._scratch / "current_execution_id.txt").write_text(eid, encoding="utf-8")
        exec_code = _resolve_kernel_code_markers(code, self.workspace) if self.language == "python" else code
        msg_id = self._kc.execute(exec_code, store_history=store_history)
        deadline = began + max(0.0, float(timeout_s))
        interrupted_at: float | None = None
        pending_noticed = False
        alive_checks = 0
        idle = False
        while not idle:
            now = time.monotonic()
            if now >= deadline and interrupted_at is None:
                interrupted_at = now
                self.interrupt()
            if (
                interrupted_at is not None
                and now - interrupted_at > TIMEOUT_INTERRUPT_GRACE_S
            ):
                if not preserve_on_timeout:
                    result.status = "timeout"
                    break
                if not pending_noticed:
                    pending_noticed = True
                    if callbacks.on_timeout_pending is not None:
                        try:
                            callbacks.on_timeout_pending(now - began)
                        except Exception:  # noqa: BLE001 - retain the kernel if an observer fails
                            _log.debug("Protected execution timeout observer failed")
            try:
                wait = (
                    1.0
                    if interrupted_at is not None
                    else min(1.0, max(0.001, deadline - now))
                )
                msg = self._jupyter_sync(self._kc.get_iopub_msg, timeout=wait)
            except Empty:
                now = time.monotonic()
                alive_checks += 1
                # A SIGKILLed kernel says nothing at all — no error frame,
                # no status — so silence alone cannot be told from a long
                # computation without asking the process table. Probed on
                # every empty poll once past the first couple (a healthy
                # kernel between outputs is routinely silent), because the
                # one thing that must not happen is waiting the full cell
                # timeout for a process that no longer exists.
                if alive_checks > 2 and not self.is_alive():
                    result.status = "dead"
                    break
                if not idle_noticed and now - last_output > STREAM_IDLE_AFTER_S:
                    idle_noticed = True
                    label = _last_label(tallies)
                    if callbacks.on_idle is not None:
                        try:
                            callbacks.on_idle(now - last_output, label)
                        except Exception:  # noqa: BLE001
                            pass
                continue
            except Exception:  # noqa: BLE001 - the socket died under us
                result.status = "dead"
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind = msg.get("header", {}).get("msg_type", "")
            content = msg.get("content", {})
            if kind == "status":
                if content.get("execution_state") == "idle":
                    idle = True
                continue
            last_output = time.monotonic()
            idle_noticed = False
            if kind == "stream":
                name = str(content.get("name", "stdout"))
                tally = tallies.get(name)
                if tally is None:
                    name = "stdout"
                    tally = tallies["stdout"]
                text = str(content.get("text", ""))
                if text:
                    tally.feed(text, _emit(name))
                    ordered.add(
                        {
                            "type": "stream",
                            "name": name,
                            "text": bounded_text(text, 4096),
                        }
                    )
            elif kind == "error":
                error = {
                    "ename": bounded_text(content.get("ename", "Error"), 256),
                    "evalue": bounded_text(
                        content.get("evalue", ""), min(self.limits.result_max, 2048)
                    ),
                    "traceback": bounded_text(
                        _ANSI.sub(
                            "",
                            "\n".join(
                                bounded_text(line, 512)
                                for line in (content.get("traceback") or [])[:32]
                            ),
                        ),
                        min(self.limits.result_max, 4096),
                    ),
                }
                ordered.add(
                    {
                        "type": "error",
                        **error,
                        "traceback": error["traceback"].splitlines(),
                    }
                )
            elif kind == "clear_output":
                ordered.add({"type": "clear", "wait": bool(content.get("wait"))})
            elif kind in ("display_data", "execute_result", "update_display_data"):
                data = content.get("data") or {}
                if not isinstance(data, dict):
                    continue
                ref = None
                png = data.get("image/png")
                if isinstance(png, str) and png:
                    if (
                        self.limits.figure_count_max is not None
                        and len(figures) >= self.limits.figure_count_max
                    ) or (
                        self.limits.figure_encoded_max is not None
                        and len(png) > self.limits.figure_encoded_max
                    ):
                        tallies["stderr"].feed(
                            "[Notebook figure output truncated]\n", _emit("stderr")
                        )
                        ordered.add(
                            {"type": "truncated", "reason": "figure_output_budget"}
                        )
                        continue
                    self._figure_seq += 1
                    ref = self._save_figure(png, self._figure_seq)
                    if ref is None or not ref.artifact_id:
                        ordered.add(
                            {
                                "type": "truncated",
                                "reason": "image_artifact_unavailable",
                            }
                        )
                    if ref is not None:
                        figures.append(ref)
                        if callbacks.on_display is not None:
                            try:
                                callbacks.on_display(ref)
                            except Exception:  # noqa: BLE001
                                pass
                ordered.display(
                    data,
                    image=ref,
                    display_id=(content.get("transient") or {}).get("display_id"),
                    update=kind == "update_display_data",
                )
        if idle:
            if interrupted_at is not None:
                result.status = "timeout"
            elif self._interrupted:
                result.status = "interrupted"
            else:
                result.status = "ok"
            reply = self._shell_reply(msg_id)
            if reply is not None:
                result.execution_count = reply.get("content", {}).get("execution_count")
        if error is not None:
            result.error = error
            if interrupted_at is not None or result.status == "timeout":
                result.status = "timeout"
            elif error.get("ename") == "KeyboardInterrupt" or self._interrupted:
                result.status = "interrupted"
            else:
                result.status = "error"
        if store_history:
            after_files = _scan_workspace_files(self.workspace)
            captured_paths = {str(Path(f.path).resolve()) for f in figures}
            auto_prefix = f"figures/kernel_{_session_slug(self.session_id)}_"
            written: list[dict[str, Any]] = []
            for rel in sorted(after_files):
                if rel.startswith(auto_prefix):
                    continue
                mtime_ns, size = after_files[rel]
                if before_files.get(rel) == (mtime_ns, size):
                    continue
                written.append({"path": rel, "size_bytes": size})
                full_path = self.workspace / rel
                ext = full_path.suffix.lower()
                if ext in _IMAGE_MIMES and str(full_path.resolve()) not in captured_paths:
                    mime = _IMAGE_MIMES[ext]
                    artifact_id = ""
                    if self._on_figure is not None:
                        try:
                            artifact_id, _kind = self._on_figure(full_path, mime)
                        except Exception:  # noqa: BLE001
                            pass
                    ref = FigureRef(path=str(full_path), mime=mime, artifact_id=artifact_id)
                    figures.append(ref)
                    captured_paths.add(str(full_path.resolve()))
                    if callbacks.on_display is not None:
                        try:
                            callbacks.on_display(ref)
                        except Exception:  # noqa: BLE001
                            pass
                    if artifact_id:
                        ordered.add({"type": "image", "artifactId": artifact_id, "mime": mime})
            audit_reads, audit_writes, audit_skills, audit_loaded, audit_objects = _harvest_kernel_ledger_v2(
                self._ledger_path, ledger_offset
            )
            for sc in audit_skills:
                if isinstance(sc, dict) and not sc.get("execution_id"):
                    sc["execution_id"] = eid
            written_by_path = {w["path"]: w for w in written}
            for aw in audit_writes:
                p = aw.get("path")
                if p and p not in written_by_path:
                    written.append(aw)
                    written_by_path[p] = aw
                elif p and aw.get("sha256"):
                    written_by_path[p]["sha256"] = aw["sha256"]
            for w in written:
                if "sha256" not in w:
                    fp = self.workspace / w["path"]
                    if fp.is_file() and fp.stat().st_size <= 64 * 1024 * 1024:
                        w["sha256"] = _sha256_quick(fp)
            result.files_written = written
            result.files_read = audit_reads
            result.skill_calls = audit_skills
            result.skills_loaded = audit_loaded
            result.object_versions = audit_objects
            _append_workspace_ledger(
                self.workspace,
                session_id=self.session_id,
                execution_count=result.execution_count,
                execution_id=eid,
                status=result.status,
                code=code,
                files_read=audit_reads,
                files_written=written,
                skill_calls=audit_skills,
                skills_loaded=audit_loaded,
                object_versions=audit_objects,
            )
        result.stdout = tallies["stdout"].text()
        result.stderr = tallies["stderr"].text()
        result.figures = figures
        result.outputs = ordered.items
        result.usage = {
            "wall_s": round(time.monotonic() - began, 3),
            "cpu_s": self.cpu_seconds(),
            "peak_rss_kb": self.peak_rss_kb(),
        }
        if preserve_on_timeout:
            result.usage["stop_confirmed"] = idle or not self.is_alive()
            result.usage["deadline_exceeded"] = interrupted_at is not None
        if store_history:
            self.cells += 1
        if callbacks.on_usage is not None:
            try:
                callbacks.on_usage(result.usage)
            except Exception:  # noqa: BLE001
                pass
        return result

    def _shell_reply(self, msg_id: str) -> Any:
        """The execute reply, or ``None`` when it never came (dead kernel)."""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                msg = self._jupyter_sync(self._kc.get_shell_msg, timeout=2.0)
            except Empty:
                continue
            except Exception:  # noqa: BLE001
                return None
            if (
                msg.get("msg_id") == msg_id
                or msg.get("parent_header", {}).get("msg_id") == msg_id
            ):
                return msg
        return None

    def _save_figure(self, png_b64: str, number: int) -> FigureRef | None:
        """Decode one ``image/png`` onto the workspace and register it."""
        try:
            # IRkernel emits MIME base64 with line breaks; other characters stay invalid.
            payload = png_b64.encode("ascii").translate(None, b" \t\r\n")
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            return None
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        directory = self.workspace / "figures"
        directory.mkdir(parents=True, exist_ok=True)
        name = figure_basename(self.session_id, number)
        path = directory / name
        path.write_bytes(raw)
        artifact_id = ""
        if self._on_figure is not None:
            try:
                artifact_id, _kind = self._on_figure(path, "image/png")
            except Exception:  # noqa: BLE001 - a registration fault is not a cell fault
                _log.exception("kernel figure registration failed: %s", path)
        return FigureRef(path=str(path), mime="image/png", artifact_id=artifact_id)


def _last_label(tallies: dict[str, _StreamTally]) -> str:
    """A one-line hint of what the cell last said, for the idle notice."""
    for name in ("stdout", "stderr"):
        pieces = tallies[name]._pieces
        if pieces:
            tail = pieces[-1].strip().splitlines()
            if tail:
                return f"{name}: {tail[-1][:80]}"
    return "no output yet"


def _linux_birth(pid: int):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _descendant_pids(root_pid: int) -> list[int]:
    """Return ``[root_pid, ...descendants...]`` by walking ``/proc/<pid>/task/*/children``."""
    seen: list[int] = []
    visited: set[int] = set()
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        if current in visited or current <= 0:
            continue
        visited.add(current)
        seen.append(current)
        task_dir = Path(f"/proc/{current}/task")
        try:
            tids = list(task_dir.iterdir()) if task_dir.is_dir() else []
        except OSError:
            tids = []
        for tid_entry in tids:
            children_file = tid_entry / "children"
            try:
                text = children_file.read_text()
            except OSError:
                continue
            for token in text.split():
                if token.isdigit():
                    child = int(token)
                    if child not in visited:
                        queue.append(child)
    return seen


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]


def _win_peak_rss_kb(pid: int) -> int | None:
    if not hasattr(ctypes, "windll"):
        return None
    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    if psapi is None:
        return None
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, int(pid))
    if not handle:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize) // 1024
        return None
    finally:
        kernel32.CloseHandle(handle)


def _win_cpu_seconds(pid: int) -> float | None:
    if not hasattr(ctypes, "windll"):
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0400, False, int(pid))
    if not handle:
        return None
    try:
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        k_ticks = (int(kernel_time.dwHighDateTime) << 32) | int(
            kernel_time.dwLowDateTime
        )
        u_ticks = (int(user_time.dwHighDateTime) << 32) | int(user_time.dwLowDateTime)
        return (k_ticks + u_ticks) / 10_000_000.0
    finally:
        kernel32.CloseHandle(handle)


KernelSession = SessionKernel


def _sha256_quick(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(262144), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


_KERNEL_MARKER_RE: Final = re.compile(r"\{\{(!?)artifact:([^}]*)\}\}")


def _resolve_kernel_artifact_path(raw_id: str, workspace: Path) -> Path | None:
    rid = str(raw_id or "").strip()
    if not rid or any(ch in rid for ch in ("'", '"', "[", "]", "{", "}", " ", "\t", "\n", "\r", "\\", "..")):
        return None
    if rid.startswith("/"):
        return None
    ws = workspace.resolve()
    candidates = [rid, rid[4:] if rid.startswith("art_") else f"art_{rid}"]

    for reg_cand in (
        ws / "provenance" / "artifacts.json",
        ws / ".omicsclaw" / "provenance" / "artifacts.json",
    ):
        if reg_cand.is_file():
            try:
                reg = json.loads(reg_cand.read_text(encoding="utf-8"))
                entries: list[dict[str, Any]] = []
                if isinstance(reg, dict):
                    if rid in reg and isinstance(reg[rid], (str, dict)):
                        val = reg[rid]
                        p_str = val if isinstance(val, str) else str(val.get("path") or "")
                        if p_str:
                            fp = (ws / p_str).resolve() if not Path(p_str).is_absolute() else Path(p_str).resolve()
                            if fp.is_file():
                                return fp
                    if isinstance(reg.get("artifacts"), list):
                        entries.extend(x for x in reg["artifacts"] if isinstance(x, dict))
                    else:
                        entries.extend(v for v in reg.values() if isinstance(v, dict))
                elif isinstance(reg, list):
                    entries.extend(x for x in reg if isinstance(x, dict))
                for ent in entries:
                    ent_ids = {
                        str(ent.get(k) or "")
                        for k in ("id", "artifact_id", "version_id", "latest_version_id", "vid")
                    } - {""}
                    ent_path = str(ent.get("path") or ent.get("filename") or "")
                    if (ent_ids & set(candidates)) or ent_path == rid or (ent_path and Path(ent_path).name == rid):
                        if ent_path:
                            fp = (ws / ent_path).resolve() if not Path(ent_path).is_absolute() else Path(ent_path).resolve()
                            if fp.is_file():
                                return fp
            except Exception:  # noqa: BLE001
                pass

    jobs_db = ws / ".omicsclaw" / "jobs.db"
    if jobs_db.is_file():
        try:
            import sqlite3
            with sqlite3.connect(str(jobs_db)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT id, path, sha256, meta_json FROM artifacts ORDER BY created_at DESC LIMIT 500").fetchall()
            for r in rows:
                r_id = str(r["id"] or "")
                r_path = Path(str(r["path"] or ""))
                if not r_path.is_absolute():
                    r_path = ws / r_path
                r_sha = str(r["sha256"] or "")
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except Exception:
                    meta = {}
                ver_id = str(meta.get("version_id") or meta.get("latest_version_id") or "")
                rel_p = ""
                try:
                    rel_p = r_path.resolve().relative_to(ws).as_posix()
                except ValueError:
                    rel_p = ""
                if (
                    r_id in candidates
                    or (ver_id and ver_id in candidates)
                    or (r_sha and len(rid) >= 8 and r_sha.startswith(rid))
                    or r_path.name == rid
                    or rel_p == rid
                    or (rel_p and rel_p.endswith(f"/{rid}"))
                ):
                    if r_path.is_file():
                        return r_path.resolve()
        except Exception:  # noqa: BLE001
            pass

    for direct_cand in (ws / rid, ws / "output" / rid):
        try:
            resolved_cand = direct_cand.resolve()
            if resolved_cand.is_relative_to(ws) and resolved_cand.is_file():
                return resolved_cand
        except Exception:  # noqa: BLE001
            pass

    for sub in ("output", "results", "figures", "tables"):
        base = ws / sub
        if not base.is_dir():
            continue
        for fp in sorted(base.rglob("*")):
            if not fp.is_file():
                continue
            rel = fp.relative_to(ws).as_posix()
            if (
                rel == rid
                or rel.endswith(f"/{rid}")
                or fp.name == rid
                or f"art_{fp.stem}" in candidates
            ):
                return fp.resolve()
    return None


def _resolve_kernel_code_markers(code: str, workspace: Path) -> str:
    if "artifact:" not in code:
        return code
    ws = workspace.resolve()

    def _sub(m: re.Match[str]) -> str:
        bang, rid = m.group(1), m.group(2)
        if bang == "!":
            return f"{{{{artifact:{rid}}}}}"
        resolved = _resolve_kernel_artifact_path(rid, ws)
        if resolved is None:
            return m.group(0)
        try:
            return resolved.relative_to(ws).as_posix()
        except ValueError:
            return resolved.as_posix()

    return _KERNEL_MARKER_RE.sub(_sub, code)


_LEDGER_STARTUP_TEMPLATE: Final = r'''# OmicsClaw kernel I/O, object-version, host.* SDK & skill auto-ledger hook
def __omicsclaw_install_ledger():
    import builtins, functools, hashlib, json, os, pathlib, sqlite3, sys, time
    try:
        from IPython import get_ipython
        ip = get_ipython()
    except Exception:
        ip = None
    if ip is None:
        return
    cfg = json.loads(__OMICSCLAW_CFG_REPR__)
    ws_lex = pathlib.Path(os.path.abspath(cfg["workspace"]))
    ws_root = pathlib.Path(cfg["workspace"]).resolve()
    ledger_file = pathlib.Path(cfg["ledger"]).resolve()
    exec_id_file = pathlib.Path(cfg["exec_id_file"]).resolve()
    state = {
        "active": False,
        "reads": {},
        "writes": {},
        "skills": [],
        "skills_loaded": [],
        "object_versions": [],
        "pre_objects": {},
    }
    skip_prefixes = (".omicsclaw/", ".git/", "__pycache__/", ".pytest_cache/", "provenance/")
    obj_registry = ip.user_ns.setdefault("__omicsclaw_object_versions__", {})

    def _record_skill(entry):
        if isinstance(entry, dict):
            item = dict(entry)
            if not item.get("execution_id"):
                item["execution_id"] = str(ip.user_ns.get("__omicsclaw_execution_id__", ""))
            state["skills"].append(item)
            if item.get("assumptions"):
                matched = False
                for sl in state["skills_loaded"]:
                    if isinstance(sl, dict) and (sl.get("skill") == item.get("skill") or sl.get("slug") == item.get("slug")):
                        sl["assumptions"] = dict(item["assumptions"])
                        matched = True
                if not matched:
                    state["skills_loaded"].append(item)

    def _record_sidecar_loaded(entry):
        if isinstance(entry, dict):
            item = dict(entry)
            if item not in state["skills_loaded"]:
                state["skills_loaded"].append(item)
            if not state["active"]:
                try:
                    with ledger_file.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"skills_loaded": [item]}) + "\n")
                except Exception:
                    pass

    ip.user_ns["__omicsclaw_record_skill_call__"] = _record_skill
    ip.user_ns["__omicsclaw_record_sidecar_loaded__"] = _record_sidecar_loaded

    def _record_path(target, mode="r"):
        if not state["active"]:
            return
        try:
            if isinstance(target, int) or target is None:
                return
            raw = os.fsdecode(os.fspath(target))
            p = pathlib.Path(raw)
            if not p.is_absolute():
                p = pathlib.Path(os.getcwd()) / p
            lex_p = pathlib.Path(os.path.abspath(str(p)))
            real_p = p.resolve()
            rel = None
            for cand_p, cand_ws in ((lex_p, ws_lex), (lex_p, ws_root), (real_p, ws_root)):
                try:
                    rel = cand_p.relative_to(cand_ws).as_posix()
                    break
                except Exception:
                    continue
            if not rel or rel == "." or rel.startswith(skip_prefixes) or "/." in f"/{rel}":
                return
            mode_str = str(mode or "r")
            if any(ch in mode_str for ch in "wa+x"):
                state["writes"][rel] = str(real_p)
            else:
                if rel not in state["writes"]:
                    state["reads"].setdefault(rel, str(real_p))
        except Exception:
            return

    def _fingerprint_one(obj):
        try:
            if hasattr(obj, "obs") and hasattr(obj, "var") and hasattr(obj, "shape"):
                shape = [int(x) for x in obj.shape]
                obs_cols = [str(c) for c in getattr(obj.obs, "columns", ())]
                var_cols = [str(c) for c in getattr(obj.var, "columns", ())]
                uns_keys = sorted(str(k) for k in getattr(obj, "uns", {}).keys())
                obsm_keys = sorted(str(k) for k in getattr(obj, "obsm", {}).keys())
                layers_keys = sorted(str(k) for k in getattr(obj, "layers", {}).keys())
                x_sig = ""
                x_mat = getattr(obj, "X", None)
                if x_mat is not None:
                    try:
                        arr = x_mat[: min(4, shape[0]), : min(4, shape[1])]
                        if hasattr(arr, "toarray"):
                            arr = arr.toarray()
                        x_sig = f"{getattr(x_mat, 'dtype', '')}:{repr(arr.tolist() if hasattr(arr, 'tolist') else str(arr))[:160]}"
                    except Exception:
                        x_sig = repr(x_mat)[:160]
                obs_sig = ""
                try:
                    if obs_cols and len(obj.obs) > 0:
                        if hasattr(obj.obs, "iloc"):
                            obs_sig = repr(obj.obs.iloc[: min(3, len(obj.obs))].to_dict(orient="list"))[:200]
                        elif hasattr(obj.obs, "to_dict"):
                            obs_sig = repr(obj.obs.to_dict())[:200]
                except Exception:
                    pass
                meta = {
                    "type": "AnnData",
                    "shape": shape,
                    "obs_columns": obs_cols,
                    "var_columns": var_cols,
                    "uns_keys": uns_keys,
                    "obsm_keys": obsm_keys,
                    "layers_keys": layers_keys,
                }
                fp = hashlib.sha256(
                    json.dumps({**meta, "x_sig": x_sig, "obs_sig": obs_sig}, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
                return {"fingerprint": fp, "meta": meta}
            if hasattr(obj, "columns") and hasattr(obj, "shape") and not hasattr(obj, "obs"):
                shape = [int(x) for x in obj.shape]
                cols = [str(c) for c in obj.columns]
                val_sig = ""
                try:
                    if shape[0] > 0 and shape[1] > 0:
                        if hasattr(obj, "iloc"):
                            val_sig = repr(obj.iloc[: min(3, shape[0]), : min(6, shape[1])].to_dict(orient="list"))[:200]
                        elif hasattr(obj, "to_dict"):
                            val_sig = repr(obj.to_dict())[:200]
                        elif hasattr(obj, "_data"):
                            val_sig = repr(obj._data)[:200]
                except Exception:
                    pass
                meta = {"type": "DataFrame", "shape": shape, "columns": cols}
                fp = hashlib.sha256(
                    json.dumps({**meta, "val_sig": val_sig}, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
                return {"fingerprint": fp, "meta": meta}
        except Exception:
            return None
        return None

    def _scan_namespace_objects():
        snap = {}
        skip_names = {"In", "Out", "exit", "quit", "get_ipython", "host", "load_skill"}
        for k, v in list(ip.user_ns.items()):
            if not isinstance(k, str) or k.startswith("_") or k in skip_names:
                continue
            info = _fingerprint_one(v)
            if info is not None:
                snap[k] = info
        return snap

    class _FindingsResult(list):
        def __getitem__(self, key):
            if isinstance(key, str):
                if key == "findings":
                    return list(self)
                if key == "count":
                    return len(self)
                raise KeyError(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            if key == "findings":
                return list(self)
            if key == "count":
                return len(self)
            return default

        def __contains__(self, item):
            if item in ("findings", "count"):
                return True
            return super().__contains__(item)

    class _FindingsAccessor:
        _VALID = ("unresolved", "open", "addressed", "claimed", "unaddressed", "resolved", "all")

        def _paths(self):
            return [
                ws_root / ".omicsclaw" / "provenance" / "findings.json",
                ws_root / "provenance" / "findings.json",
            ]

        def _load_all(self):
            for p in self._paths():
                if p.is_file():
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            return [x for x in data if isinstance(x, dict)]
                        if isinstance(data, dict) and isinstance(data.get("findings"), list):
                            return [x for x in data["findings"] if isinstance(x, dict)]
                    except Exception:
                        continue
            return []

        def _save_all(self, items):
            payload = json.dumps(items, indent=2, ensure_ascii=False)
            for p in self._paths():
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(payload, encoding="utf-8")
                except Exception:
                    pass

        def __call__(self, status="unresolved", frame=None, all_branches=False):
            if not isinstance(status, str) or status not in self._VALID:
                raise ValueError(f"host.findings: status={status!r} is not valid. Must be one of: {list(self._VALID)}")
            items = self._load_all()
            if status == "all":
                filtered = items
            elif status == "unresolved":
                filtered = [f for f in items if f.get("status", "unresolved") in ("unresolved", "open", "unaddressed")]
            elif status == "addressed":
                filtered = [f for f in items if f.get("status", "unresolved") in ("addressed", "claimed", "resolved")]
            else:
                filtered = [f for f in items if f.get("status", "unresolved") == status]
            return _FindingsResult(filtered)

        def mark_addressed(self, ids, note=""):
            if isinstance(ids, str):
                ids = [ids]
            if not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
                raise TypeError("host.findings.mark_addressed: ids must be a non-empty finding id or list of finding ids")
            if len(ids) > 100:
                raise ValueError("host.findings.mark_addressed: at most 100 ids per call")
            if not isinstance(note, str) or len(note) > 2000:
                raise ValueError("host.findings.mark_addressed: note must be a string (max 2000 chars)")
            target_set = set(ids)
            items = self._load_all()
            updated = []
            now = time.time()
            for f in items:
                fid = str(f.get("id") or "")
                if fid in target_set or "*" in target_set:
                    f["status"] = "addressed"
                    f["resolution_note"] = note.strip()
                    f["addressed_note"] = note.strip()
                    f["addressed_at"] = now
                    updated.append(fid)
            self._save_all(items)
            return {"addressed": updated, "count": len(updated)}

    class _OperonHost:
        def __init__(self):
            self.findings = _FindingsAccessor()

        def artifacts(self, filename=None, exact=False, kind=None, limit=200):
            items = []
            seen_keys = set()
            seen_paths = set()
            for reg_cand in (
                ws_root / "provenance" / "artifacts.json",
                ws_root / ".omicsclaw" / "provenance" / "artifacts.json",
            ):
                if reg_cand.is_file():
                    try:
                        reg = json.loads(reg_cand.read_text(encoding="utf-8"))
                        reg_list = []
                        if isinstance(reg, list):
                            reg_list = reg
                        elif isinstance(reg, dict):
                            if isinstance(reg.get("artifacts"), list):
                                reg_list = list(reg["artifacts"])
                            for k, v in reg.items():
                                if k == "artifacts":
                                    continue
                                if isinstance(v, str):
                                    reg_list.append({"id": k, "vid": k, "path": v})
                                elif isinstance(v, dict):
                                    ent_copy = dict(v)
                                    ent_copy.setdefault("id", k)
                                    ent_copy.setdefault("vid", k)
                                    reg_list.append(ent_copy)
                        for ent in reg_list:
                            if not isinstance(ent, dict):
                                continue
                            rel = str(ent.get("path") or ent.get("filename") or "")
                            if not rel:
                                continue
                            p = (ws_root / rel).resolve() if not pathlib.Path(rel).is_absolute() else pathlib.Path(rel).resolve()
                            aid = str(ent.get("id") or ent.get("artifact_id") or ent.get("vid") or f"art_{p.stem}")
                            vid_val = str(ent.get("vid") or ent.get("version_id") or ent.get("latest_version_id") or aid)
                            if (aid, rel) in seen_keys:
                                continue
                            seen_keys.add((aid, rel))
                            seen_paths.add(rel)
                            items.append({
                                "id": aid,
                                "vid": vid_val,
                                "artifact_id": aid,
                                "latest_version_id": vid_val,
                                "marker": "{{artifact:" + aid + "}}",
                                "kind": str(ent.get("kind") or "other"),
                                "path": rel,
                                "abs_path": str(p),
                                "filename": p.name,
                                "title": str(ent.get("title") or p.name),
                                "sha256": str(ent.get("sha256") or ""),
                                "size": int(ent.get("size") or (p.stat().st_size if p.is_file() else 0)),
                            })
                    except Exception:
                        pass
            jobs_db = ws_root / ".omicsclaw" / "jobs.db"
            if jobs_db.is_file():
                try:
                    with sqlite3.connect(str(jobs_db)) as conn:
                        conn.row_factory = sqlite3.Row
                        rows = conn.execute("SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
                    for r in rows:
                        p = pathlib.Path(str(r["path"] or ""))
                        if not p.is_absolute():
                            p = ws_root / p
                        try:
                            rel = p.resolve().relative_to(ws_root).as_posix()
                        except Exception:
                            rel = str(p)
                        aid = str(r["id"])
                        try:
                            meta = json.loads(r["meta_json"] or "{}") if r["meta_json"] else {}
                        except Exception:
                            meta = {}
                        vid_val = str(meta.get("version_id") or meta.get("latest_version_id") or aid)
                        if (aid, rel) in seen_keys:
                            continue
                        seen_keys.add((aid, rel))
                        seen_paths.add(rel)
                        items.append({
                            "id": aid,
                            "vid": vid_val,
                            "artifact_id": aid,
                            "latest_version_id": vid_val,
                            "marker": "{{artifact:" + aid + "}}",
                            "kind": str(r["kind"] or "other"),
                            "path": rel,
                            "abs_path": str(p.resolve()) if p.exists() else str(p),
                            "filename": p.name,
                            "title": str(r["title"] or p.name),
                            "sha256": str(r["sha256"] or ""),
                            "size": int(r["size"] or 0),
                        })
                except Exception:
                    pass
            for sub in ("output", "results", "figures", "tables"):
                base = ws_root / sub
                if not base.is_dir():
                    continue
                for fp in sorted(base.rglob("*")):
                    if not fp.is_file() or fp.name.startswith("."):
                        continue
                    rel = fp.relative_to(ws_root).as_posix()
                    if rel in seen_paths or "/provenance/" in f"/{rel}" or "/sidecars/" in f"/{rel}":
                        continue
                    seen_paths.add(rel)
                    aid = f"art_{fp.stem}"
                    items.append({
                        "id": aid,
                        "vid": rel,
                        "artifact_id": aid,
                        "latest_version_id": aid,
                        "marker": "{{artifact:" + rel + "}}",
                        "kind": "table" if fp.suffix.lower() in (".csv", ".tsv") else ("figure" if fp.suffix.lower() in (".png", ".svg", ".pdf") else "other"),
                        "path": rel,
                        "abs_path": str(fp.resolve()),
                        "filename": fp.name,
                        "title": fp.name,
                        "sha256": "",
                        "size": fp.stat().st_size,
                    })
            if kind:
                items = [x for x in items if x.get("kind") == kind]
            if filename:
                fn_str = str(filename)
                if exact:
                    items = [x for x in items if x.get("filename") == fn_str or x.get("path") == fn_str]
                else:
                    items = [x for x in items if fn_str.lower() in x.get("filename", "").lower() or fn_str.lower() in x.get("path", "").lower()]
            return items[: max(1, min(int(limit), 1000))]

        def artifact_path(self, vid):
            raw = str(vid or "").strip()
            if raw.startswith("{{artifact:") and raw.endswith("}}"):
                raw = raw[len("{{artifact:"):-2].strip()
            elif raw.startswith("{{!artifact:") and raw.endswith("}}"):
                raw = raw[len("{{!artifact:"):-2].strip()
            if not raw or any(ch in raw for ch in ("'", '"', "[", "]", "{", "}", " ", "\t", "\n", "\r", "\\", "..")):
                raise ValueError(f"host.artifact_path: invalid artifact id {vid!r}")
            cands = {raw, raw[4:] if raw.startswith("art_") else f"art_{raw}"}
            for item in self.artifacts(limit=1000):
                if (
                    item["id"] in cands
                    or item.get("vid") in cands
                    or item["artifact_id"] in cands
                    or item["latest_version_id"] in cands
                    or item["filename"] == raw
                    or item["path"] == raw
                    or item["path"].endswith(f"/{raw}")
                    or (item.get("sha256") and len(raw) >= 8 and item["sha256"].startswith(raw))
                ):
                    abs_p = item["abs_path"]
                    _record_path(abs_p, "r")
                    return abs_p
            for direct_cand in (ws_root / raw, ws_root / "output" / raw):
                try:
                    resolved = direct_cand.resolve()
                    if resolved.is_relative_to(ws_root) and resolved.is_file():
                        abs_p = str(resolved)
                        _record_path(abs_p, "r")
                        return abs_p
                except Exception:
                    pass
            raise FileNotFoundError(f"host.artifact_path: unknown artifact {vid!r}")

        def artifact_marker(self, vid, escaped=False):
            raw = str(vid or "").strip()
            if raw.startswith("{{artifact:") and raw.endswith("}}"):
                raw = raw[len("{{artifact:"):-2].strip()
            elif raw.startswith("{{!artifact:") and raw.endswith("}}"):
                raw = raw[len("{{!artifact:"):-2].strip()
            if not raw or any(ch in raw for ch in ("'", '"', "[", "]", "{", "}", " ", "\t", "\n", "\r", "\\", "..")):
                raise ValueError(f"host.artifact_marker: invalid artifact id {vid!r}")
            prefix = "{{!artifact:" if escaped else "{{artifact:"
            return f"{prefix}{raw}}}}}"

    ip.user_ns["host"] = _OperonHost()

    def _patch_h5_and_anndata():
        h5_mod = sys.modules.get("h5py")
        if h5_mod is not None:
            file_cls = getattr(h5_mod, "File", None)
            if file_cls is not None and not getattr(file_cls.__init__, "__omicsclaw_patched__", False):
                orig_init = file_cls.__init__
                @functools.wraps(orig_init)
                def _wrapped_h5_init(self, name, mode="r", *args, **kwargs):
                    _record_path(name, mode if isinstance(mode, str) else "r")
                    return orig_init(self, name, mode, *args, **kwargs)
                _wrapped_h5_init.__omicsclaw_patched__ = True
                try:
                    file_cls.__init__ = _wrapped_h5_init
                except Exception:
                    pass
        for mod_name in ("anndata", "scanpy"):
            mod = sys.modules.get(mod_name)
            if mod is not None:
                fn = getattr(mod, "read_h5ad", None)
                if callable(fn) and not getattr(fn, "__omicsclaw_patched__", False):
                    @functools.wraps(fn)
                    def _wrapped_read_h5ad(filename, backed=None, *args, _orig=fn, **kwargs):
                        _record_path(filename, "r+" if backed == "r+" else "r")
                        return _orig(filename, backed=backed, *args, **kwargs)
                    _wrapped_read_h5ad.__omicsclaw_patched__ = True
                    try:
                        setattr(mod, "read_h5ad", _wrapped_read_h5ad)
                    except Exception:
                        pass

    _orig_import = builtins.__import__
    if not getattr(_orig_import, "__omicsclaw_patched__", False):
        @functools.wraps(_orig_import)
        def _wrapped_import(name, globals=None, locals=None, fromlist=(), level=0):
            res = _orig_import(name, globals, locals, fromlist, level)
            if isinstance(name, str) and (
                name in ("h5py", "anndata", "scanpy")
                or name.startswith(("h5py.", "anndata.", "scanpy."))
            ):
                _patch_h5_and_anndata()
            return res
        _wrapped_import.__omicsclaw_patched__ = True
        builtins.__import__ = _wrapped_import

    def _audit(event, args):
        if not state["active"]:
            return
        if event == "open" and args:
            target = args[0]
            mode = str(args[1]) if len(args) > 1 and args[1] is not None else "r"
            _record_path(target, mode)
        elif event == "import":
            _patch_h5_and_anndata()

    def _hash_map(mapping):
        items = []
        for rel, full_str in sorted(mapping.items()):
            try:
                fp = pathlib.Path(full_str)
                if not fp.is_file():
                    continue
                st = fp.stat()
                sha = ""
                if st.st_size <= 64 * 1024 * 1024:
                    h = hashlib.sha256()
                    with fp.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(262144), b""):
                            h.update(chunk)
                    sha = h.hexdigest()
                items.append({"path": rel, "size_bytes": st.st_size, "sha256": sha})
            except Exception:
                continue
        return items

    def _pre_cell(info):
        if not getattr(info, "store_history", True):
            state["active"] = False
            return
        state["reads"].clear()
        state["writes"].clear()
        state["skills"].clear()
        state["skills_loaded"].clear()
        state["object_versions"].clear()
        state["pre_objects"] = _scan_namespace_objects()
        try:
            if exec_id_file.is_file():
                ip.user_ns["__omicsclaw_execution_id__"] = exec_id_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        _patch_h5_and_anndata()
        state["active"] = True

    def _post_cell(result):
        if not state["active"]:
            return
        state["active"] = False
        try:
            post_objects = _scan_namespace_objects()
            for name, info in sorted(post_objects.items()):
                prev = obj_registry.get(name)
                fp = info["fingerprint"]
                meta = info["meta"]
                if prev is None or prev.get("fingerprint") != fp:
                    prev_ver = int(prev.get("version", 0)) if prev else 0
                    new_ver = prev_ver + 1
                    prev_meta = (prev.get("meta") if prev else None) or {}
                    entry = {
                        "name": name,
                        "type": meta.get("type", "Object"),
                        "version": new_ver,
                        "previous_version": prev_ver,
                        "prev_version": prev_ver,
                        "pointer": f"{name}@v{new_ver}",
                        "action": "created" if prev_ver == 0 else "mutated",
                        "shape": meta.get("shape"),
                        "shape_before": prev_meta.get("shape"),
                        "fingerprint": fp,
                        "execution_id": str(ip.user_ns.get("__omicsclaw_execution_id__", "")),
                    }
                    if meta.get("type") == "AnnData":
                        for field_k, out_k in (
                            ("obs_columns", "added_obs"),
                            ("uns_keys", "added_uns"),
                            ("obsm_keys", "added_obsm"),
                            ("layers_keys", "added_layers"),
                        ):
                            before_s = set(prev_meta.get(field_k) or ())
                            after_l = [x for x in (meta.get(field_k) or ()) if x not in before_s]
                            if after_l:
                                entry[out_k] = after_l
                    elif meta.get("type") == "DataFrame":
                        before_c = set(prev_meta.get("columns") or ())
                        added_c = [x for x in (meta.get("columns") or ()) if x not in before_c]
                        if added_c:
                            entry["added_columns"] = added_c
                    obj_registry[name] = {"version": new_ver, "fingerprint": fp, "meta": meta}
                    state["object_versions"].append(entry)

            reads_list = _hash_map(state["reads"])
            writes_list = _hash_map(state["writes"])
            skills_list = list(state["skills"])
            loaded_list = list(state["skills_loaded"])
            objects_list = list(state["object_versions"])
            for s_item in ip.user_ns.get("__omicsclaw_loaded_sidecars__", ()):
                if isinstance(s_item, dict) and s_item not in loaded_list:
                    loaded_list.append(dict(s_item))
            if reads_list or writes_list or skills_list or loaded_list or objects_list:
                payload = json.dumps({
                    "execution_count": getattr(result, "execution_count", None),
                    "execution_id": str(ip.user_ns.get("__omicsclaw_execution_id__", "")),
                    "files_read": reads_list,
                    "files_written": writes_list,
                    "skill_calls": skills_list,
                    "skills_loaded": loaded_list,
                    "object_versions": objects_list,
                })
                with ledger_file.open("a", encoding="utf-8") as fh:
                    fh.write(payload + "\n")
        except Exception:
            pass

    ip.events.register("pre_run_cell", _pre_cell)
    ip.events.register("post_run_cell", _post_cell)
    sys.addaudithook(_audit)

try:
    __omicsclaw_install_ledger()
finally:
    del __omicsclaw_install_ledger
'''


def _write_ipython_ledger_startup(
    ipython_dir: Path, *, workspace: Path, ledger_path: Path
) -> None:
    startup_dir = ipython_dir / "profile_default" / "startup"
    startup_dir.mkdir(parents=True, exist_ok=True)
    script = startup_dir / "00_omicsclaw_ledger.py"
    exec_id_file = ipython_dir.parent / "current_execution_id.txt"
    cfg = json.dumps(
        {
            "workspace": str(workspace),
            "ledger": str(ledger_path),
            "exec_id_file": str(exec_id_file),
        }
    )
    code = _LEDGER_STARTUP_TEMPLATE.replace("__OMICSCLAW_CFG_REPR__", repr(cfg))
    script.write_text(code, encoding="utf-8")


def _harvest_kernel_ledger_v2(
    ledger_path: Path | None, offset: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if ledger_path is None or not ledger_path.is_file():
        return [], [], [], [], []
    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    loaded: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    try:
        with ledger_path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read().decode("utf-8", errors="ignore")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                continue
            for r in item.get("files_read") or []:
                if r not in reads:
                    reads.append(r)
            for w in item.get("files_written") or []:
                if w not in writes:
                    writes.append(w)
            for s in item.get("skill_calls") or []:
                if s not in skills:
                    skills.append(s)
            for sl in item.get("skills_loaded") or []:
                if sl not in loaded:
                    loaded.append(sl)
            for ov in item.get("object_versions") or []:
                if ov not in objects:
                    objects.append(ov)
    except Exception:  # noqa: BLE001
        pass
    return reads, writes, skills, loaded, objects


def _harvest_kernel_ledger(
    ledger_path: Path | None, offset: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    r, w, s, sl, _ov = _harvest_kernel_ledger_v2(ledger_path, offset)
    return r, w, s, sl


def _append_workspace_ledger(
    workspace: Path,
    *,
    session_id: str,
    execution_count: int | None,
    execution_id: str = "",
    status: str = "ok",
    code: str,
    files_read: list[dict[str, Any]],
    files_written: list[dict[str, Any]],
    skill_calls: list[dict[str, Any]],
    skills_loaded: list[dict[str, Any]] | None = None,
    object_versions: list[dict[str, Any]] | None = None,
) -> None:
    if not code.strip() and not (files_read or files_written or skill_calls or skills_loaded or object_versions):
        return
    try:
        record = {
            "session_id": session_id,
            "execution_count": execution_count,
            "execution_id": execution_id,
            "status": status,
            "timestamp": time.time(),
            "code_sha256": hashlib.sha256(code.strip().encode("utf-8")).hexdigest(),
            "code": code[:65536],
            "files_read": files_read,
            "files_written": files_written,
            "skill_calls": skill_calls,
            "skills_loaded": list(skills_loaded or []),
            "object_versions": list(object_versions or []),
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        for prov_dir in (
            workspace / ".omicsclaw" / "provenance",
            workspace / "provenance",
        ):
            prov_dir.mkdir(parents=True, exist_ok=True)
            for fname in ("kernel_ledger.jsonl", "session_ledger.jsonl"):
                with (prov_dir / fname).open("a", encoding="utf-8") as fh:
                    fh.write(line)
    except OSError:
        pass

