"""One live IPython kernel and the iopub consumption primitive.

P4 track B of the front-back plan. The module below is the OmicOS
``session_notebook_executor`` pattern re-derived for OmicsClaw: start one
ipykernel through :mod:`jupyter_client`, send code with ``kc.execute``,
then **poll** ``get_iopub_msg(timeout=1.0)`` and dispatch by message
type, keeping only frames whose ``parent_header.msg_id`` is the request
we are waiting for (a kernel that posts status for a comm or a
background thread must not be read as our cell finishing), and ending at
``status: idle``.

The kernel is started from a **temporary kernelspec** whose ``argv`` is
this interpreter — the same isolation
``skills/_sdk/notebook/_runners.py`` established for the step-runner
(track A, deliberately untouched): IPython and Jupyter directories point
into a per-session scratch folder under ``<workspace>/.omicsclaw/kernel/``,
so the user's ``~/.ipython`` startup profile never runs inside an
analysis kernel, and the kernel never writes under ``$HOME``.

**This class is synchronous and thread-confined.** Every method blocks
(zmq reads, process waits); the manager calls it through
``asyncio.to_thread`` and owns the asyncio half (locks, cancellation,
the reaper). ``interrupt``/``kill`` are the two methods safe from any
thread — jupyter_client's own shutdown path relies on the same.

**The output ceilings are three, not one** (Claude-Science
kernel_worker.py:113/126/176, re-derived here as characters because a
Python ``str`` cannot cut a code point in half — the argument
``bash.py`` makes for its own limit):

1. *streaming* — once a stream (stdout, stderr — each measured alone)
   has delivered ``stream_emit_max`` characters to the callback, the
   callback stops being called and one truncation marker is emitted.
   The run keeps going; the audience stops paying.
2. *cell buffer* — the in-memory buffer of one cell keeps at most
   ``cell_buffer_max`` characters per stream; whatever overflows is
   counted and reported exactly: ``...(N further bytes dropped)``.
3. *result* — the :class:`CellResult` text fields are clamped to
   ``result_max`` characters each, a last line of defence before
   anything serialises the outcome into JSON or a model's context.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

from queue import Empty

__all__ = [
    "CellResult",
    "KernelCallbacks",
    "KERNEL_NAME",
    "OutputLimits",
    "SessionKernel",
    "figure_basename",
]

_log = logging.getLogger(__name__)

KERNEL_NAME: Final = "omicsclaw-session"
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

STREAM_EMIT_MARKER: Final = (
    "...[stream output reached the 10 MB streaming cap; "
    "further output is suppressed]"
)
STREAM_IDLE_AFTER_S: Final = 30.0
"""Output silence that long, while a cell runs, is worth an idle notice."""


@dataclass(frozen=True, slots=True)
class OutputLimits:
    """The three ceilings, overridable as one object for tests."""

    stream_emit_max: int = 10 * 1024 * 1024
    cell_buffer_max: int = 1024 * 1024
    result_max: int = 1024 * 1024


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
    usage: dict[str, Any] = field(default_factory=dict)
    notice: str = ""
    """A kernel-restart handover notice, prepended to stderr by the manager."""


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


def figure_basename(session_id: str, number: int) -> str:
    """``kernel_<session>_<N>.png``; *number* is a per-kernel monotonic
    sequence seeded past anything earlier kernels of this session wrote,
    so neither a second cell nor a lazy cold restart overwrites history."""
    return f"kernel_{_session_slug(session_id)}_{number}.png"


class SessionKernel:
    """One persistent ipykernel owned by one session.

    Constructing is cheap (no process); :meth:`start` launches the kernel
    and blocks until it answers ``kernel_info``. Every blocking method
    expects to run on the manager's worker thread.
    """

    def __init__(
        self,
        session_id: str,
        *,
        workspace: Path,
        python: str | None = None,
        limits: OutputLimits | None = None,
        on_figure: Callable[[Path, str], tuple[str, str]] | None = None,
        startup_timeout: float = 120.0,
    ) -> None:
        self.session_id = session_id
        self.workspace = Path(workspace)
        self.python = python or sys.executable
        self.limits = limits or OutputLimits()
        self._on_figure = on_figure
        self._startup_timeout = startup_timeout
        self._km: Any = None
        self._kc: Any = None
        self._scratch: Path | None = None
        self.started_at = 0.0
        self.cells = 0
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
            return bool(self._km is not None and self._km.is_alive())
        except Exception:  # noqa: BLE001 - a dead manager object is a dead kernel
            return False

    def start(self) -> None:
        """Launch the kernel; block until it is ready. Idempotent-refusing."""
        if self._km is not None:
            raise RuntimeError("kernel already started")
        from jupyter_client.kernelspec import KernelSpecManager
        from jupyter_client.manager import KernelManager

        scratch = self.workspace / ".omicsclaw" / "kernel" / re.sub(
            r"[^0-9A-Za-z_.-]", "-", self.session_id
        )[:48].strip("-") or "session"
        scratch.mkdir(parents=True, exist_ok=True)
        spec_dir = scratch / "kernels" / KERNEL_NAME
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": [
                        self.python,
                        "-m",
                        "ipykernel_launcher",
                        "-f",
                        "{connection_file}",
                        "--HistoryManager.enabled=False",
                    ],
                    "display_name": "OmicsClaw session",
                    "language": "python",
                }
            ),
            encoding="utf-8",
        )
        isolated = {
            "IPYTHONDIR": str(scratch / "ipython"),
            "JUPYTER_RUNTIME_DIR": str(scratch / "runtime"),
            "JUPYTER_DATA_DIR": str(scratch / "data"),
            "JUPYTER_CONFIG_DIR": str(scratch / "config"),
        }
        for folder in isolated.values():
            Path(folder).mkdir(parents=True, exist_ok=True)
        env = {
            name: value
            for name, value in os.environ.items()
            if name.upper() != "OMICSCLAW_REMOTE_AUTH_TOKEN"
        }
        env.update(isolated)
        spec_manager = KernelSpecManager(
            kernel_dirs=[str(scratch / "kernels")], ensure_native_kernel=False
        )
        manager = KernelManager(
            kernel_name=KERNEL_NAME,
            kernel_spec_manager=spec_manager,
            connection_file=str(scratch / "runtime" / "kernel.json"),
        )
        manager.start_kernel(env=env, cwd=str(self.workspace))
        client = manager.client()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=self._startup_timeout)
        except Exception:
            self._shutdown_client(client)
            self._kill_manager(manager)
            raise
        self._km = manager
        self._kc = client
        self._scratch = scratch
        self.started_at = time.time()
        self._seed_figure_sequence()

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
        if self._km is not None:
            try:
                self._km.interrupt_kernel()
            except Exception as exc:  # noqa: BLE001 - interrupting a dead kernel
                _log.debug("interrupt failed for %s: %s", self.session_id, exc)

    def kill(self) -> None:
        """SIGKILL the kernel's process group. Safe from any thread."""
        if self._km is not None:
            self._kill_manager(self._km)

    def shutdown(self) -> None:
        """Polite shutdown; falls back to kill."""
        if self._km is None:
            return
        try:
            self._shutdown_client(self._kc)
        finally:
            try:
                if self._km.has_kernel:
                    self._km.shutdown_kernel(now=True)
            except Exception:  # noqa: BLE001
                self._kill_manager(self._km)
            finally:
                self._km = None
                self._kc = None

    @staticmethod
    def _shutdown_client(client: Any) -> None:
        try:
            client.stop_channels()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _kill_manager(manager: Any) -> None:
        provisioner = getattr(manager, "provisioner", None)
        pid = getattr(provisioner, "pid", None)
        pgid = getattr(provisioner, "pgid", None)
        try:
            if pgid and pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
            elif pid:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # ---- resource probes ----

    def peak_rss_kb(self) -> int | None:
        """The kernel's high-water RSS, from ``/proc`` when it exists."""
        pid = self.pid
        if not pid:
            return None
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    def cpu_seconds(self) -> float | None:
        pid = self.pid
        if not pid:
            return None
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            # utime + stime, in clock ticks; fields 14 and 15 (1-based)
            ticks = int(fields[13]) + int(fields[14])
            return ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None

    # ---- execution ----

    def execute(
        self,
        code: str,
        *,
        timeout_s: float = 1800.0,
        callbacks: KernelCallbacks | None = None,
    ) -> CellResult:
        """Run one cell to ``status: idle`` and return its outcome.

        The wall clock starts before the request is sent and covers the
        whole drain, per the plan's rule that a default timeout is
        retained (30 min) with idle detection beside it. On deadline the
        ladder is started here — interrupt, then a short grace — and the
        result carries ``status="timeout"`` for the manager to finish.
        """
        callbacks = callbacks or KernelCallbacks()
        result = CellResult()
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

        msg_id = self._kc.execute(code)
        deadline = began + max(1.0, float(timeout_s))
        interrupted_at: float | None = None
        alive_checks = 0
        idle = False
        while not idle:
            try:
                msg = self._kc.get_iopub_msg(timeout=1.0)
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
                if now > deadline and interrupted_at is None:
                    interrupted_at = now
                    self.interrupt()
                if interrupted_at is not None and now - interrupted_at > 5.0:
                    result.status = "timeout"
                    break
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
            elif kind == "error":
                error = {
                    "ename": str(content.get("ename", "Error")),
                    "evalue": str(content.get("evalue", "")),
                    "traceback": _ANSI.sub("", "\n".join(content.get("traceback") or [])),
                }
            elif kind in ("display_data", "execute_result"):
                data = content.get("data") or {}
                png = data.get("image/png")
                if isinstance(png, str) and png:
                    self._figure_seq += 1
                    ref = self._save_figure(png, self._figure_seq)
                    if ref is not None:
                        figures.append(ref)
                        if callbacks.on_display is not None:
                            try:
                                callbacks.on_display(ref)
                            except Exception:  # noqa: BLE001
                                pass
        if idle:
            result.status = "ok"
            reply = self._shell_reply(msg_id)
            if reply is not None:
                result.execution_count = reply.get("content", {}).get(
                    "execution_count"
                )
        if error is not None:
            result.error = error
            result.status = (
                "interrupted"
                if error.get("ename") == "KeyboardInterrupt"
                else ("timeout" if result.status == "timeout" else "error")
            )
        result.stdout = tallies["stdout"].text()
        result.stderr = tallies["stderr"].text()
        result.figures = figures
        result.usage = {
            "wall_s": round(time.monotonic() - began, 3),
            "cpu_s": self.cpu_seconds(),
            "peak_rss_kb": self.peak_rss_kb(),
        }
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
                msg = self._kc.get_shell_msg(timeout=2.0)
            except Empty:
                continue
            except Exception:  # noqa: BLE001
                return None
            if msg.get("msg_id") == msg_id or msg.get("parent_header", {}).get(
                "msg_id"
            ) == msg_id:
                return msg
        return None

    def _save_figure(self, png_b64: str, number: int) -> FigureRef | None:
        """Decode one ``image/png`` onto the workspace and register it."""
        try:
            raw = base64.b64decode(png_b64)
        except (ValueError, TypeError):
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
