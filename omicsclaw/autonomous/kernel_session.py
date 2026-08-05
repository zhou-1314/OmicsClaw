"""Persistent sandboxed kernel session for the Autonomous Code Mini-Agent.

ADR 0032 §4: one persistent Jupyter kernel per autonomous run, launched inside
the bubblewrap safety envelope. State survives across mini-agent steps; the
kernel speaks to the client over ZMQ **IPC** (unix sockets at an absolute path
under a bind-mounted directory) so ``--unshare-net`` blocks all real network
without breaking the kernel<->client channel.

The validated launch recipe (see the spike evidence in the ADR 0032 PR):

* ``write_connection_file(transport="ipc", ip="<conn_dir>/sock")`` — absolute
  ipc socket paths, identical inside and outside the sandbox;
* ``bwrap`` argv from :mod:`kernel_envelope`, launched with the scrubbed env;
* ``BlockingKernelClient`` loads the same connection file and drains iopub.

``sandbox=False`` runs an un-isolated kernel for development / CI on hosts
without bubblewrap; the runner decides policy (fail-closed vs degrade).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .kernel_envelope import (
    EnvelopeConfig,
    build_bwrap_argv,
    build_launch_env,
    envelope_available,
    scrub_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# "Kernel scratch home" inside the sandbox's ephemeral /tmp tmpfs. Each bwrap
# sandbox gets its own private /tmp, so this fixed path never collides across
# concurrent runs and vanishes when the sandbox exits — tool caches never touch
# the user-facing run workspace.
_SANDBOX_KERNEL_HOME = "/tmp/oc-kernel-home"

# Names hidden from variable introspection (interpreter + our own helpers).
_INTROSPECT_SKIP = {"In", "Out", "exit", "quit", "get_ipython", "open"}

_INTROSPECT_CODE = r"""
import json as _oc_json
def _oc_introspect():
    _info = {}
    for _n, _v in list(globals().items()):
        if _n.startswith('_') or _n in __OC_SKIP__:
            continue
        _d = {'type': type(_v).__name__}
        try:
            _shape = getattr(_v, 'shape', None)
            if _shape is not None:
                _d['shape'] = str(_shape)
        except Exception:
            pass
        _info[_n] = _d
    print('__OC_VARS__' + _oc_json.dumps(_info))
_oc_introspect()
"""


class KernelStartError(RuntimeError):
    """Raised when the kernel process never becomes ready."""


@dataclass(slots=True)
class CellResult:
    """Outcome of executing one code cell in the kernel."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    result_text: str = ""
    ename: str = ""
    evalue: str = ""
    traceback: str = ""
    timed_out: bool = False
    cancelled: bool = False
    duration_seconds: float = 0.0

    @property
    def error_summary(self) -> str:
        if self.cancelled:
            return f"cancelled after {self.duration_seconds:.1f}s"
        if self.timed_out:
            return f"timed out after {self.duration_seconds:.1f}s"
        if self.ename:
            return f"{self.ename}: {self.evalue}"
        return ""


class KernelSession:
    """A persistent, optionally-sandboxed Jupyter kernel."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        read_roots: list[str | Path] | None = None,
        sandbox: bool = True,
        repo_root: str | Path = REPO_ROOT,
        startup_timeout: float = 60.0,
        allow_network: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.read_roots = [Path(p) for p in (read_roots or [])]
        self.repo_root = Path(repo_root).resolve()
        self.sandbox = bool(sandbox) and envelope_available()
        self.startup_timeout = float(startup_timeout)
        self.allow_network = bool(allow_network)

        self._conn_dir: Path | None = None
        self._conn_file: Path | None = None
        self._home_dir: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._client = None
        self._alive = False

    # -- lifecycle ------------------------------------------------------- #

    @property
    def alive(self) -> bool:
        return self._alive and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        from jupyter_client import BlockingKernelClient
        from jupyter_client.connect import write_connection_file

        if not kernel_ipc_available():
            raise KernelStartError("ZMQ IPC sockets are unavailable; persistent kernel cannot start")

        # IPC sockets, NOT the deep workspace. AF_UNIX paths are capped at ~107
        # chars (sizeof sockaddr_un.sun_path); a long workspace/run-dir name
        # (e.g. pytest tmp dirs, ``autonomous-code__<ts>__<id>``) overflows it and
        # the kernel's ``s.bind('ipc://…')`` fails, hanging the client. So the
        # connection dir lives in a short system-temp path and is bind-mounted in.
        self._conn_dir = Path(tempfile.mkdtemp(prefix="ock-", dir=_short_tmp_base()))
        self._conn_file = self._conn_dir / "kernel.json"
        ip_prefix = self._conn_dir / "k"
        if len(str(ip_prefix)) > 90:
            shutil.rmtree(self._conn_dir, ignore_errors=True)
            raise KernelStartError(
                f"ipc socket base path too long for AF_UNIX ({len(str(ip_prefix))} chars): {ip_prefix}"
            )
        # Absolute, short ipc socket prefix: identical inside and outside the sandbox.
        write_connection_file(
            fname=str(self._conn_file),
            transport="ipc",
            ip=str(ip_prefix),
        )

        inner = [sys.executable, "-m", "ipykernel_launcher", "-f", str(self._conn_file)]
        if self.sandbox:
            # Kernel scratch HOME lives in the sandbox's own /tmp tmpfs: ephemeral,
            # invisible to the host, no bind and no cleanup needed.
            config = EnvelopeConfig(
                workspace_root=self.workspace_root,
                ipc_dir=self._conn_dir,
                repo_root=self.repo_root,
                home_dir=Path(_SANDBOX_KERNEL_HOME),
                read_roots=list(self.read_roots),
                allow_network=self.allow_network,
            )
            argv = build_bwrap_argv(config, inner)
            env = build_launch_env(config)
        else:
            # No sandbox (no tmpfs): give the kernel a throwaway host HOME, removed
            # on shutdown so tool caches never persist beside the run workspace.
            self._home_dir = Path(tempfile.mkdtemp(prefix="ock-home-", dir=_short_tmp_base()))
            argv = inner
            env = scrub_env(
                dict(os.environ),
                workspace_root=self.workspace_root,
                home_dir=self._home_dir,
            )
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [str(self.repo_root), env.get("PYTHONPATH", "")])
            )

        self._proc = subprocess.Popen(argv, env=env)
        client = BlockingKernelClient()
        client.load_connection_file(str(self._conn_file))
        client.start_channels()
        try:
            self._wait_for_ready(client)
        except RuntimeError as exc:
            client.stop_channels()
            self._terminate_proc()
            self._cleanup_scratch()
            raise KernelStartError(
                f"kernel did not become ready within {self.startup_timeout}s: {exc}"
            ) from exc
        self._client = client
        self._alive = True

    def _wait_for_ready(self, client) -> None:
        """Wait through jupyter-client's transient IPC heartbeat race.

        ``BlockingKernelClient`` has no KernelManager when OmicsClaw launches
        bubblewrap itself.  During IPC startup its first heartbeat check can
        therefore report ``Kernel died`` even though the owned process is still
        alive and becomes ready a fraction of a second later.  Process liveness
        is the authoritative early-failure signal; retries remain bounded by
        the original startup deadline.
        """

        deadline = time.monotonic() + self.startup_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("kernel_info reply timed out")
            try:
                client.wait_for_ready(timeout=remaining)
                return
            except RuntimeError:
                if self._proc is None or self._proc.poll() is not None:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.25, remaining))

    def shutdown(self) -> None:
        if self._client is not None:
            try:
                self._client.stop_channels()
            except Exception:
                pass
            self._client = None
        self._terminate_proc()
        self._alive = False
        self._cleanup_scratch()

    def __enter__(self) -> "KernelSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    # -- execution ------------------------------------------------------- #

    def execute(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        cancel_event: "threading.Event | None" = None,
    ) -> CellResult:
        """Run *code* in the kernel and collect stdout/stderr/error.

        On timeout the running cell is interrupted; if the kernel does not go
        idle the caller should treat the session as needing a restart.

        ``cancel_event`` (ADR 0009) lets a Surface interrupt a running cell
        mid-flight (the desktop "Stop" button). Without it the only way to stop
        a stuck cell — notably an ``oc.run`` skill call blocked for up to
        ``skill_call_timeout_seconds`` — was to wait out the full ``timeout``,
        so an autonomous run appeared frozen. When set, the cell is interrupted
        and the kernel restarted exactly like a timeout, but the result is
        marked ``cancelled`` rather than ``timed_out``.
        """
        if self._client is None:
            raise KernelStartError("kernel session is not started")
        client = self._client
        t0 = time.monotonic()
        msg_id = client.execute(code, store_history=True)

        stdout: list[str] = []
        stderr: list[str] = []
        result_text = ""
        ename = evalue = tb_text = ""
        deadline = t0 + timeout
        timed_out = False
        cancelled = False
        # Poll more tightly when a cancel is watchable so "Stop" feels immediate;
        # a plain cell keeps the original 1s cadence.
        poll_cap = 0.2 if cancel_event is not None else 1.0

        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                self._interrupt()
                self._restart_after_timeout()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self._interrupt()
                self._restart_after_timeout()
                break
            try:
                msg = client.get_iopub_msg(timeout=min(remaining, poll_cap))
            except queue.Empty:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            mtype = msg["msg_type"]
            content = msg["content"]
            if mtype == "stream":
                (stdout if content.get("name") == "stdout" else stderr).append(content.get("text", ""))
            elif mtype in ("execute_result", "display_data"):
                data = content.get("data", {})
                if "text/plain" in data:
                    result_text = data["text/plain"]
            elif mtype == "error":
                ename = content.get("ename", "")
                evalue = content.get("evalue", "")
                tb_text = "\n".join(content.get("traceback", []))
            elif mtype == "status" and content.get("execution_state") == "idle":
                break

        duration = time.monotonic() - t0
        return CellResult(
            ok=not timed_out and not cancelled and not ename,
            stdout="".join(stdout),
            stderr="".join(stderr),
            result_text=result_text,
            ename=ename,
            evalue=evalue,
            traceback=_strip_ansi(tb_text),
            timed_out=timed_out,
            cancelled=cancelled,
            duration_seconds=duration,
        )

    def introspect(self) -> dict[str, dict]:
        """Return ``{var_name: {type, shape?}}`` for user variables."""
        code = _INTROSPECT_CODE.replace("__OC_SKIP__", repr(_INTROSPECT_SKIP))
        result = self.execute(code, timeout=20)
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("__OC_VARS__"):
                try:
                    return json.loads(line[len("__OC_VARS__"):])
                except json.JSONDecodeError:
                    return {}
        return {}

    # -- internals ------------------------------------------------------- #

    def _interrupt(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            self._proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    def _restart_after_timeout(self) -> None:
        """Replace a timed-out kernel so busy state cannot leak into later cells."""
        self._alive = False
        if self._client is not None:
            try:
                self._client.stop_channels()
            except Exception:
                pass
            self._client = None
        self._terminate_proc()
        self._cleanup_scratch()
        try:
            self.start()
        except Exception:
            self._alive = False

    def _cleanup_scratch(self) -> None:
        """Remove the per-run scratch dirs (ipc connection dir + throwaway HOME).

        Called on shutdown, on a post-timeout restart, AND on a failed start — so a
        kernel that never becomes ready does not leak its connection dir or its
        non-sandbox HOME caches under /tmp.
        """
        for attr in ("_conn_dir", "_home_dir"):
            path = getattr(self, attr)
            if path is not None and path.exists():
                shutil.rmtree(path, ignore_errors=True)
            setattr(self, attr, None)
        self._conn_file = None

    def _terminate_proc(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None


def _short_tmp_base() -> str:
    """Shortest writable temp base, to keep AF_UNIX ipc paths under 107 chars."""
    for base in ("/tmp", tempfile.gettempdir()):
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return base
    return tempfile.gettempdir()


def kernel_ipc_available() -> bool:
    """Return whether this process can create ZMQ IPC sockets for Jupyter."""
    try:
        import zmq
    except Exception:
        return False

    probe_dir = Path(tempfile.mkdtemp(prefix="ock-probe-", dir=_short_tmp_base()))
    ctx = None
    sock = None
    try:
        ctx = zmq.Context()
        sock = ctx.socket(zmq.PAIR)
        sock.bind("ipc://" + str(probe_dir / "k"))
        return True
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close(linger=0)
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.term()
            except Exception:
                pass
        shutil.rmtree(probe_dir, ignore_errors=True)


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


__all__ = ["CellResult", "KernelSession", "KernelStartError", "REPO_ROOT", "kernel_ipc_available"]
