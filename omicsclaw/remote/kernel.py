"""C3: the remote persistent kernel — ipykernel beside the data.

The one piece of remote state the plan keeps (§4 C3, absorbing OmicOS's
real advantage): a session's analysis kernel runs **on the remote host**,
so ``adata`` loads once and survives turns without the data ever
travelling to this machine. The supply side stays minimal — the remote
host is asked for one Python environment under ``~/.omicsclaw/kernel-env``
(uv venv + ipykernel + the scanpy core stack), not a product stack.

**The wire is the standard jupyter protocol.** The remote ipykernel
writes its own connection file (five ports on the remote loopback plus a
random key); we read that file back over SFTP-less ``ssh cat``, rewrite
nothing but the ``ip`` (defensively — both sides are 127.0.0.1), and run
one ``ssh -N`` process carrying five ``-L`` forwards bound to the **local
127.0.0.1 only**. :mod:`jupyter_client` then *attaches* —
``load_connection_file`` + ``client()`` + ``wait_for_ready`` — never
``start_kernel``: the process belongs to the remote host, and the local
side owns only the tunnel.

**The kernel object is P4's, subclassed.** :class:`RemoteSessionKernel`
extends :class:`~omicsclaw.kernel.session.SessionKernel` and overrides
exactly the process-management face — ``start``/``is_alive``/
``interrupt``/``kill``/``shutdown`` and the two ``/proc`` probes —
because the cell-execution half (the three output ceilings, figure
capture, the idle notice, the interrupt ladder) is location-agnostic and
is inherited, not copied. The manager treats it as one more kernel:
same reaper, same idle soft top, same cancel ladder, same obituaries.

**Liveness has two witnesses.** The heartbeat channel is the cheap local
one; when it falls silent the two failure modes — kernel died, tunnel
died — are indistinguishable locally, so ``ssh kill -0 <pid>`` settles
it (the pid the launch recorded, nothing else). A tunnel that died with
the kernel alive is *not* a death: the next cell re-spawns the forwards
and the zmq channels reconnect, and the variables are still there — the
acceptance scenario (kill the tunnel, reconnect, the namespace survives)
is a property of this split, not a recovery path bolted on later.

**Every remote command from a kernel method runs on a worker thread**
(the manager's ``asyncio.to_thread`` boundary). The sync driver here
enforces that invariant: it refuses to run on the event loop, because
``asyncio.run`` cannot nest — the one P4 call site that used to kill
from the loop thread (``_park_with_obituary``) now offloads to a thread
so the remote kill can pay its SSH round trip off-loop.

**Two seams deliberately untouched.** The ``X-OmicsClaw-Runtime``
response header (P3's routing seam) is not spoken here: it answers
*where an HTTP request's backend lives*, and a remote kernel is reached
by a tool argument or a job body, not by a wire route — widening it to
mean two things would cost the header its one meaning. And ``bash``
stays location-blind: remote shell-shaped work is C1's ``remote_exec``
family's trade, and a location-dispatching ``bash`` would be a second
shell seam with none of the kernel's persistent state to offer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omicsclaw.envsupply.pins import (
    CORE_STACK_PACKAGES,
    KERNEL_BASE_PACKAGES,
    PYTHON_VERSION,
    UV_INSTALL_URL,
)
from omicsclaw.kernel.session import (
    CellResult,
    KernelCallbacks,
    OutputLimits,
    SessionKernel,
)
from omicsclaw.tools.builtin.remote import ExecOutcome

from .ssh import (
    SshSpawner,
    SystemSshSpawner,
    build_ssh_argv,
    encode_login_script,
    shell_quote,
    validate_host,
)

__all__ = [
    "CORE_STACK_PACKAGES",
    "DEFAULT_ENV_DIR",
    "DEFAULT_SESSIONS_DIR",
    "REMOTE_LAUNCH_TIMEOUT_S",
    "RemoteKernelEnv",
    "RemoteKernelEnvError",
    "RemoteKernelLaunchError",
    "RemoteKernelRuntime",
    "RemoteKernelTunnelError",
    "RemoteSessionKernel",
    "build_env_probe_command",
    "build_kernel_launch_command",
    "build_tunnel_argv",
    "ensure_remote_kernel_env",
    "install_remote_kernel_env",
    "parse_launch_output",
    "probe_remote_kernel_env",
    "rewrite_connection_info",
]

_log = logging.getLogger(__name__)

DEFAULT_ENV_DIR = "~/.omicsclaw/kernel-env"
"""Where the remote kernel environment lives, under the remote $HOME.

Deliberately not the probed scratch: a kernel environment is installed
once and reused for months, which is home-directory lifetime, while
scratch candidates are for job-sized data."""

DEFAULT_SESSIONS_DIR = "~/.omicsclaw/kernel-sessions"
"""One 700 directory per running kernel: connection file, pid, log."""

# The stack/version pins above are imported from envsupply (P5), not
# forked: the remote and local planes provision the same kind of
# environment, and two copies of a pin are one pin that has drifted.
# REMOTE_PYTHON_VERSION keeps the remote-facing name its callers read.
REMOTE_PYTHON_VERSION = PYTHON_VERSION

ENV_PROBE_TIMEOUT_S = 60.0
ENV_INSTALL_TIMEOUT_S = 1800.0
"""One half hour for the first install: the scanpy stack is hundreds of
megabytes and a lab uplink is not a datacenter's."""

REMOTE_LAUNCH_TIMEOUT_S = 90.0
"""The launch command's own budget: spawn + wait for the kernel to write
its connection file (up to 60 s inside) + cat it back."""

TUNNEL_OPTIONS: tuple[str, ...] = (
    "-o", "BatchMode=yes",
    "-o", "ForwardAgent=no",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2",
    # The tunnel is a long-lived connection of its own. Sharing the
    # command channel's ControlMaster socket would make this -N process
    # the master for every other remote call, so killing a kernel's
    # tunnel would tear down in-flight probes with it — the one coupling
    # this module must not create.
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    # A forward that cannot bind (local port taken) must fail the start
    # loudly, not come up four-fifths alive.
    "-o", "ExitOnForwardFailure=yes",
)
"""The ``ssh -N`` tunnel's options. The security posture is
:data:`~omicsclaw.remote.ssh.SSH_OPTIONS`'s (batch, no agent, system
host keys) minus the multiplexing the tunnel must not share, plus the
forward-failure guarantee."""

CONNECTION_PORTS: tuple[str, ...] = (
    "shell_port",
    "iopub_port",
    "stdin_port",
    "hb_port",
    "control_port",
)
"""The five ports a jupyter connection file carries, in the order the
tunnel forwards them."""

_LAUNCH_CONN_MARKER = "===OMICSCLAW_KERNEL_CONN==="
_LAUNCH_PID_MARKER = "===OMICSCLAW_KERNEL_PID==="
_LAUNCH_LOG_MARKER = "===OMICSCLAW_KERNEL_LOG==="


def _valid_pid(pid: Any) -> bool:
    """Whether *pid* names one process a signal may be aimed at.

    Zero and the negatives are not "no process" in ``os.kill``'s
    language — ``-1`` is *every process the caller may signal* — so the
    check lives in one place and every site that splices a pid into a
    command or hands one to a syscall asks it: the parse, the cleanup
    command, the interrupt, the liveness probe, and the group kill. The
    incident that taught this: a test double's ``pid = -1`` reached
    ``os.kill(-1, SIGKILL)`` and broadcast across the host."""
    return isinstance(pid, int) and pid > 0


def _remote_path(path: str) -> str:
    """A path as the remote shell should receive it.

    A leading ``~`` means the **remote** $HOME, and single-quoting would
    make it a literal tilde — the classic ``mkdir '~'`` trap — so it is
    spliced as ``"$HOME"`` with the tail unquoted. The tail is safe
    unquoted because every path this package builds is its own
    closed alphabet (``[A-Za-z0-9_./-]``: the env/session roots above
    and a slug/token pair); anything else (a caller's exotic path) is
    single-quoted whole, tilde or not."""
    if path.startswith("~/"):
        tail = path[2:]
        if re.fullmatch(r"[A-Za-z0-9_./-]+", tail or ""):
            return '"$HOME"/' + tail
    return shell_quote(path)


class RemoteKernelEnvError(RuntimeError):
    """The remote kernel environment is missing and could not be built.

    Carries the manual commands — the failure mode the plan prefers to a
    silent fallback is the user being told exactly what to run by hand
    on the host that refused automation."""

    def __init__(self, message: str, manual_commands: Sequence[str] = ()) -> None:
        self.manual_commands = tuple(manual_commands)
        text = message
        if self.manual_commands:
            text += " Manual setup on the host:\n  " + "\n  ".join(
                self.manual_commands
            )
        super().__init__(text)


class RemoteKernelLaunchError(RuntimeError):
    """A remote kernel could not be launched, or answered no connection
    file. Carries the transport's own output (the kernel log tail when
    the launch captured one) so the failure the caller reads is the one
    the host had."""


class RemoteKernelTunnelError(RuntimeError):
    """The ssh port-forward process could not be established."""


# ---- the environment: probe, install, ensure --------------------------------


@dataclass(frozen=True, slots=True)
class RemoteKernelEnv:
    """One host's kernel environment: which remote interpreter to use."""

    host: str
    python: str
    """Absolute path of the remote interpreter (quoted per use)."""

    provisioned: bool = False
    """Whether this process installed it (False = found ready)."""


def build_env_probe_command(python: str) -> str:
    """``<python> -c 'import ipykernel'`` — the whole env fast path.

    Exit 0 means the interpreter exists and can import the one package
    the wire protocol needs; anything else means the environment has to
    be built (or the pinned override is wrong, which is an error, not an
    install trigger)."""
    return f"{_remote_path(python)} -c 'import ipykernel'"


def manual_env_commands(env_dir: str) -> tuple[str, ...]:
    """The by-hand equivalent of the install, for the error card."""
    uv = "~/.local/bin/uv"
    pip = f"{env_dir}/bin/python"
    packages = " ".join(KERNEL_BASE_PACKAGES + CORE_STACK_PACKAGES)
    return (
        "curl -LsSf " + UV_INSTALL_URL + " | sh",
        f"{uv} python install {REMOTE_PYTHON_VERSION}",
        f"{uv} venv {env_dir} --python {REMOTE_PYTHON_VERSION}",
        f"{uv} pip install --python {pip} {packages}",
    )


def build_env_install_script(
    env_dir: str,
    *,
    packages: Sequence[str] = (),
    python_version: str = REMOTE_PYTHON_VERSION,
    uv_index_url: str | None = None,
) -> str:
    """The first-use install script, POSIX sh run by a login shell.

    Idempotent by construction: ``uv python install`` and ``uv venv``
    are no-ops when their outputs exist, and ``uv pip install`` resolves
    to "already satisfied" — so a retried install heals a half-finished
    one instead of fighting it. uv itself is located on PATH, then at
    ``~/.local/bin/uv`` (where the curl installer puts it), and only a
    host with neither gets the curl install — the one network fetch this
    module performs beyond pip's own. ``UV_INDEX_URL`` prefixes the pip
    line when a mirror was configured (the CN-network path the plan
    took from OmicOS, without its mirror racing). Pure: the caller
    decides what to run it on and who approved it.
    """
    resolved = tuple(KERNEL_BASE_PACKAGES) + tuple(
        packages or CORE_STACK_PACKAGES
    )
    pip_prefix = f"UV_INDEX_URL={uv_index_url} " if uv_index_url else ""
    lines = [
        "set -e",
        'UV="$(command -v uv 2>/dev/null || true)"',
        (
            'if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then'
            ' UV="$HOME/.local/bin/uv"; fi'
        ),
        'if [ -z "$UV" ]; then',
        f"  curl -LsSf {UV_INSTALL_URL} | sh",
        '  UV="$HOME/.local/bin/uv"',
        "fi",
        f'"$UV" python install {shell_quote(python_version)}',
        (
            f'"$UV" venv {_remote_path(env_dir)} --python'
            f" {shell_quote(python_version)}"
        ),
        (
            pip_prefix
            + f'"$UV" pip install --python'
            f" {_remote_path(env_dir + '/bin/python')}"
            + " "
            + " ".join(resolved)
        ),
        (
            f'{_remote_path(env_dir + "/bin/python")}'
            ' -c "import ipykernel"'
        ),
    ]
    return "\n".join(lines)


async def probe_remote_kernel_env(
    spawner: SshSpawner,
    host: str,
    *,
    env_dir: str = DEFAULT_ENV_DIR,
    python: str | None = None,
    timeout: float = ENV_PROBE_TIMEOUT_S,
) -> RemoteKernelEnv | None:
    """The environment if it is ready, ``None`` if it has to be built.

    *python* pins an interpreter (a shared conda env, a test's local
    one): it is probed and used as-is, never installed into — the
    override is a trust statement about a machine somebody already
    curates."""
    validate_host(host)
    candidate = python if python else f"{env_dir}/bin/python"
    outcome = await spawner.spawn(
        build_ssh_argv(host, build_env_probe_command(candidate)),
        timeout=timeout,
    )
    if outcome.exit_code == 0:
        return RemoteKernelEnv(host=host, python=candidate, provisioned=False)
    return None


async def install_remote_kernel_env(
    spawner: SshSpawner,
    host: str,
    *,
    env_dir: str = DEFAULT_ENV_DIR,
    packages: Sequence[str] = (),
    uv_index_url: str | None = None,
    timeout: float = ENV_INSTALL_TIMEOUT_S,
) -> RemoteKernelEnv:
    """Install (or finish installing) the kernel environment on *host*.

    The caller owns the approval decision — this function is the
    mechanism, not the policy. Answers the environment it built, or
    raises :exc:`RemoteKernelEnvError` carrying the manual commands and
    whatever the host said."""
    validate_host(host)
    script = build_env_install_script(
        env_dir, packages=packages, uv_index_url=uv_index_url
    )
    outcome = await spawner.spawn(
        build_ssh_argv(host, encode_login_script(script)), timeout=timeout
    )
    if outcome.exit_code != 0:
        raise RemoteKernelEnvError(
            "installing the kernel environment on "
            f"{host} failed (exit {outcome.exit_code}): "
            f"{outcome.output.strip()[-400:]}",
            manual_env_commands(env_dir),
        )
    verify = await probe_remote_kernel_env(spawner, host, env_dir=env_dir)
    if verify is None:
        raise RemoteKernelEnvError(
            f"the kernel environment on {host} installed but its python"
            " cannot import ipykernel",
            manual_env_commands(env_dir),
        )
    return RemoteKernelEnv(host=host, python=verify.python, provisioned=True)


async def ensure_remote_kernel_env(
    spawner: SshSpawner,
    host: str,
    *,
    env_dir: str = DEFAULT_ENV_DIR,
    packages: Sequence[str] = (),
    python: str | None = None,
    uv_index_url: str | None = None,
    approve: Callable[[str], Awaitable[None]] | None = None,
) -> RemoteKernelEnv:
    """The one-call composition: probe, consent, install — idempotent.

    An environment that already answers needs nothing: no approval, no
    commands, no network. Only the install road goes through *approve*
    (an async callable that raises to deny); with no hook bound the
    install is refused with the manual commands rather than performed
    silently — fail closed, the same direction
    :func:`~omicsclaw.tools.context.require_approval` points."""
    validate_host(host)
    if python is not None:
        found = await probe_remote_kernel_env(
            spawner, host, env_dir=env_dir, python=python
        )
        if found is None:
            raise RemoteKernelEnvError(
                f"the pinned remote python {python!r} on {host} cannot"
                " import ipykernel; install it there or drop the pin",
            )
        return found
    found = await probe_remote_kernel_env(spawner, host, env_dir=env_dir)
    if found is not None:
        return found
    if approve is None:
        raise RemoteKernelEnvError(
            f"the kernel environment on {host} is missing and this"
            " deployment has no approval channel bound to consent to"
            " installing it",
            manual_env_commands(env_dir),
        )
    await approve(host)
    return await install_remote_kernel_env(
        spawner, host, env_dir=env_dir, packages=packages, uv_index_url=uv_index_url
    )


# ---- the launch: command construction and parsing ----------------------------


def build_kernel_launch_command(
    python: str, session_dir: str, workdir: str = ""
) -> str:
    """The one remote command that starts a kernel and answers its ports.

    Shape, in order: a 700 session directory under a 077 umask; the
    kernel detached with ``setsid nohup`` so it survives the ssh session
    (the same detachment the C1 job scripts use); the kernel itself
    writing its **own** connection file — five random loopback ports and
    a fresh key are chosen by the remote ipykernel, so nothing here ever
    guesses a port on a machine it cannot see; a bounded wait for that
    file (import time on a cold NFS home is real); then the file, the
    pid and — on failure — the kernel log, between markers.

    The pid is recorded by the detached shell itself (``echo $$`` before
    ``exec``), which makes it the kernel's own pid regardless of whether
    ``setsid`` had to fork: the kill later addresses exactly the process
    this launch started, never a pattern match over the host's process
    table. Pure: *python* and *session_dir* are shell-quoted paths this
    package constructed.
    """
    inner = (
        'echo $$ > "$1/kernel.pid" && '
        'exec "$2" -m ipykernel_launcher -f "$1/kernel.json" '
        "--HistoryManager.enabled=False"
    )
    session = _remote_path(session_dir)
    cwd = _remote_path(workdir) if workdir else session
    mkdir = f"mkdir -p -m 700 {session}"
    if workdir:
        mkdir += f" {_remote_path(workdir)}"
    return (
        mkdir + " && "
        f"cd {cwd} && umask 077 && "
        f"(setsid nohup sh -c {shell_quote(inner)} run "
        f"{session} {_remote_path(python)} "
        "> kernel.log 2>&1 < /dev/null &) && "
        "for i in $(seq 1 300); do [ -s kernel.json ] && break;"
        " sleep 0.2; done; "
        f"if [ ! -s kernel.json ]; then echo {_LAUNCH_LOG_MARKER};"
        " cat kernel.log 2>/dev/null;"
        # A kernel that started but never wrote its file (a stalled
        # import past the wait) must not be left running namelessly on
        # the host — the failure branch kills what the pid file names.
        " [ -f kernel.pid ] && kill -KILL $(cat kernel.pid) 2>/dev/null;"
        " exit 1; fi; "
        f"echo {_LAUNCH_CONN_MARKER} && cat kernel.json && "
        f"echo {_LAUNCH_PID_MARKER} && cat kernel.pid"
    )


def _marker_section(text: str, marker: str) -> str:
    """The text after *marker* up to the next ``===OMICSCLAW_KERNEL_``
    marker or the end — markers bracket every section the launch emits."""
    start = text.find(marker)
    if start < 0:
        return ""
    body = text[start + len(marker):]
    following = body.find("===OMICSCLAW_KERNEL_")
    if following >= 0:
        body = body[:following]
    return body.strip()


def parse_launch_output(text: str) -> tuple[dict[str, Any], int]:
    """The connection info and pid from a launch command's output.

    :raises RemoteKernelLaunchError: the connection file never appeared
        (the kernel log rides in the message), or what appeared did not
        parse — a connection file is only useful whole.
    """
    conn_text = _marker_section(text, _LAUNCH_CONN_MARKER)
    if not conn_text:
        log_tail = _marker_section(text, _LAUNCH_LOG_MARKER)
        detail = f" kernel log:\n{log_tail[-800:]}" if log_tail else ""
        raise RemoteKernelLaunchError(
            "the remote kernel wrote no connection file" + detail
        )
    try:
        info = json.loads(conn_text)
    except ValueError as exc:
        raise RemoteKernelLaunchError(
            f"the remote connection file did not parse: {exc}; got"
            f" {conn_text[:200]!r}"
        ) from exc
    missing = [name for name in CONNECTION_PORTS if not info.get(name)]
    if missing or "key" not in info:
        raise RemoteKernelLaunchError(
            "the remote connection file lacks "
            + ", ".join(missing or ["key"])
            + f": {conn_text[:200]!r}"
        )
    pid_text = _marker_section(text, _LAUNCH_PID_MARKER)
    try:
        pid = int(pid_text.strip())
    except ValueError as exc:
        raise RemoteKernelLaunchError(
            f"the remote kernel recorded no usable pid ({pid_text!r})"
        ) from exc
    if not _valid_pid(pid):
        # a pid of zero or less is a broadcast id to kill, not a
        # process; a launch that reports one is refused whole rather
        # than spliced into a cleanup command later
        raise RemoteKernelLaunchError(
            f"the remote kernel recorded pid {pid!r}, which names no"
            " one process; refusing the launch"
        )
    return info, pid


def rewrite_connection_info(info: dict[str, Any]) -> dict[str, Any]:
    """The connection info as the local client must see it.

    The ports are kept verbatim — the tunnel maps each local port number
    to the same remote number, so the file stays truthful on both ends.
    The ``ip`` is forced to 127.0.0.1 even though the remote kernel
    already bound there: this is the one field a host's jupyter config
    could have widened, and the local client must never dial anything
    but the loopback the tunnel listens on."""
    rewritten = dict(info)
    rewritten["ip"] = "127.0.0.1"
    rewritten["transport"] = "tcp"
    return rewritten


def build_tunnel_argv(host: str, ports: dict[str, Any]) -> list[str]:
    """The ``ssh -N`` argv carrying the five forwards. Pure.

    Each forward is ``127.0.0.1:<port>:127.0.0.1:<port>`` — the local
    bind is loopback-only (no other local user may reach the kernel's
    keyless ports) and the remote destination is the remote loopback the
    kernel bound. ``-N`` carries no shell; ``--`` ends options before
    the validated host word."""
    validate_host(host)
    argv = ["ssh", *TUNNEL_OPTIONS]
    for name in CONNECTION_PORTS:
        port = int(ports[name])
        argv.extend(["-L", f"127.0.0.1:{port}:127.0.0.1:{port}"])
    argv.extend(["-N", "--", host])
    return argv


def build_remote_cleanup_command(pid: int, session_dir: str) -> str:
    """The kill: SIGKILL the recorded pid, remove the session directory.

    Only ever addresses the pid this launch recorded and the directory
    this launch created — never a name match, never a pkill. The ``;``
    separators, not ``&&``: a kernel that already died must not stop the
    directory from being cleaned. A pid of zero or less is refused
    outright — it would splice into ``kill -KILL`` as a broadcast.

    :raises ValueError: *pid* does not name one process."""
    if not _valid_pid(pid):
        raise ValueError(
            f"refusing to build a kill command for pid {pid!r}: zero"
            " and negative pids are broadcast ids, not processes"
        )
    return (
        f"kill -KILL {int(pid)} 2>/dev/null; "
        f"rm -rf -- {_remote_path(session_dir)} 2>/dev/null; true"
    )


# ---- the sync driver ----------------------------------------------------------


class SyncSshRunner:
    """One ssh command per call, from a kernel worker thread.

    The kernel object's methods are synchronous and thread-confined (the
    P4 contract); the transport is an async :class:`SshSpawner`. This is
    the adapter: ``asyncio.run`` on a throwaway loop per call, which is
    legal exactly because the calling thread never holds a loop — and
    the running-loop check turns a future call-graph mistake (someone
    ssh-ing from the event loop) into a loud error instead of a
    ``RuntimeError`` from deep inside asyncio."""

    def __init__(self, spawner: SshSpawner, host: str) -> None:
        self._spawner = spawner
        self._host = validate_host(host)

    def __call__(self, command: str, *, timeout: float = 30.0) -> ExecOutcome:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "the sync ssh driver must not run on the event loop; a"
                " remote kernel method reached it without a worker"
                " thread — this is a call-graph bug, not a network one"
            )
        return asyncio.run(
            self._spawner.spawn(
                build_ssh_argv(self._host, command), timeout=timeout
            )
        )


def _spawn_tunnel(argv: list[str]) -> subprocess.Popen[bytes]:
    """The real tunnel process. Detached outputs and its own session, so
    a teardown SIGKILLs the whole group and nothing it printed can block
    this process's pipes (the ``bash.py`` measurement, applied)."""
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _tcp_port_open(port: int) -> bool:
    """Whether something on the local loopback accepts a connection on
    *port* — the tunnel's listening proof. The connection is empty and
    inert: the heartbeat socket on the far end echoes bytes and ignores
    none."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _attach_via_jupyter_client(
    connection_file: str, timeout: float
) -> tuple[Any, Any]:
    """Attach, never start: a manager whose connection info is the
    tunnel-facing file, and the blocking client over it. Raises on a
    kernel that does not answer ``kernel_info`` within *timeout*."""
    from jupyter_client.manager import KernelManager

    manager = KernelManager(connection_file=str(connection_file))
    manager.load_connection_file(str(connection_file))
    client = manager.client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=timeout)
    except BaseException:
        try:
            client.stop_channels()
        except Exception:  # noqa: BLE001 - tearing down a broken client
            pass
        raise
    return manager, client


# ---- the kernel ----------------------------------------------------------------


def _session_slug(session_id: str) -> str:
    """The session id made filename-safe for the remote directory name,
    cut and padded the way the local scratch does it."""
    cleaned = re.sub(r"[^0-9A-Za-z_.-]", "-", session_id)[:32].strip("-") or "s"
    return cleaned + "-" + secrets.token_hex(4)


class RemoteSessionKernel(SessionKernel):
    """One persistent ipykernel on a remote host, reached through one
    ``ssh -N`` tunnel.

    Everything a *cell* needs is inherited from
    :class:`~omicsclaw.kernel.session.SessionKernel` — the iopub
    consumption loop, the three output ceilings, figure capture, the
    interrupt-on-deadline ladder. What is overridden is everything a
    *process* needs, because the process is the remote host's:

    * ``start`` launches over ssh, reads back the kernel-written
      connection file, rewrites it loopback-local, raises the tunnel and
      attaches;
    * ``is_alive`` asks the heartbeat first and ``ssh kill -0`` second —
      a dead tunnel with a living kernel is alive, which is what makes
      re-attach a non-event;
    * ``interrupt``/``kill``/``shutdown`` signal the recorded remote pid
      over ssh (SIGINT / the control-channel shutdown request then
      SIGKILL) and always take the tunnel down with the kernel;
    * the ``/proc`` probes read the remote pid's ``/proc`` over ssh,
      best-effort like the local ones.

    The one execution-level override wraps a cell with tunnel
    maintenance: the forwards are re-spawned before a cell if they died
    between cells (the acceptance path — same kernel, same namespace),
    watched while it runs, and a channel death with the kernel still
    alive re-attaches and retries the cell once, disclosed in the
    result's notice. A mid-cell outage whose idle frame was lost while
    disconnected still ends at the cell timeout — the honest degradation,
    documented here rather than papered over.
    """

    def __init__(
        self,
        session_id: str,
        *,
        host: str,
        workspace: Path | str,
        python: str,
        driver: Callable[..., ExecOutcome],
        limits: OutputLimits | None = None,
        on_figure: Callable[[Path, str], tuple[str, str]] | None = None,
        startup_timeout: float = 120.0,
        language: str = "python",
        sessions_dir: str = DEFAULT_SESSIONS_DIR,
        target_workdir: str = "",
        tunnel_factory: Callable[[list[str]], Any] | None = None,
        attach: Callable[[str, float], tuple[Any, Any]] | None = None,
        port_open: Callable[[int], bool] | None = None,
    ) -> None:
        super().__init__(
            session_id,
            workspace=workspace,
            limits=limits,
            on_figure=on_figure,
            startup_timeout=startup_timeout,
            language=language,
        )
        self.host = validate_host(host)
        self.python = str(python)
        self.remote_pid: int | None = None
        self.session_dir = ""
        self._driver = driver
        self._sessions_dir = sessions_dir.rstrip("/")
        self._target_workdir = str(target_workdir or "").strip()
        self._tunnel_factory = tunnel_factory or _spawn_tunnel
        self._attach = attach or _attach_via_jupyter_client
        self._port_open = port_open or _tcp_port_open
        self._tunnel: Any = None
        self._torn_down = False
        self._local_conn: Path | None = None
        """The tunnel-facing copy of the connection file, in the local
        workspace scratch — the one artifact of a remote kernel that
        lives on this machine."""

    # ---- lifecycle ----

    @property
    def pid(self) -> int | None:
        return self.remote_pid

    def start(self) -> None:
        """Launch the remote kernel, raise the tunnel, attach. Blocks
        until the kernel answers ``kernel_info``; refuses to run twice."""
        if self._km is not None:
            raise RuntimeError("kernel already started")
        session_dir = f"{self._sessions_dir}/{_session_slug(self.session_id)}"
        outcome = self._driver(
            build_kernel_launch_command(
                self.python, session_dir, self._target_workdir
            ),
            timeout=REMOTE_LAUNCH_TIMEOUT_S,
        )
        if outcome.exit_code != 0:
            raise RemoteKernelLaunchError(
                f"launching the kernel on {self.host} failed (exit"
                f" {outcome.exit_code}): {outcome.output.strip()[-400:]}"
            )
        info, pid = parse_launch_output(outcome.output)
        self.remote_pid = pid
        self.session_dir = session_dir
        local_info = rewrite_connection_info(info)
        scratch = (
            self.workspace
            / ".omicsclaw"
            / "kernel"
            / re.sub(r"[^0-9A-Za-z_.-]", "-", self.session_id)[:48].strip("-")
            / f"remote-{self.host}"
        )
        (scratch / "runtime").mkdir(parents=True, exist_ok=True)
        conn_path = scratch / "runtime" / "kernel.json"
        conn_path.write_text(
            json.dumps(local_info, indent=1), encoding="utf-8"
        )
        # the file carries the session key: the same 0600 the remote
        # side's umask gave its copy, so no other local user may read it
        conn_path.chmod(0o600)
        self._local_conn = conn_path
        try:
            self._start_tunnel(local_info)
            self._km, self._kc = self._attach(
                str(conn_path), self._startup_timeout
            )
        except BaseException:
            self.kill()
            raise
        self.started_at = time.time()
        self._seed_figure_sequence()

    def is_alive(self) -> bool:
        """The remote kernel process's liveness, from two witnesses.

        The heartbeat over the tunnel is free and local; when it is
        silent, ``ssh kill -0 <pid>`` is the authority that
        distinguishes a dead kernel from a dead tunnel. A transport that
        cannot answer counts as alive: an unreachable host proves
        nothing either way, and the conservative answer keeps the
        namespace recyclable-by-nothing — the next successful connection
        re-attaches instead of cold-starting over the variables."""
        if self._torn_down:
            return False
        if self._kc is not None:
            try:
                if self._kc.is_alive():
                    return True
            except Exception:  # noqa: BLE001 - a broken client is not a verdict
                pass
        return self._remote_pid_alive()

    def interrupt(self) -> None:
        """Tier 1 over ssh: SIGINT the recorded pid. Safe from any
        thread (the driver enforces the worker-thread one)."""
        if self._km is None or not _valid_pid(self.remote_pid):
            return
        try:
            self._driver(f"kill -INT {self.remote_pid}", timeout=15.0)
        except Exception as exc:  # noqa: BLE001 - interrupting a dead kernel
            _log.debug("remote interrupt failed for %s: %s", self.session_id, exc)

    def kill(self) -> None:
        """SIGKILL the remote kernel, its tunnel and its session dir.

        Local teardown first (channels, tunnel — both instant, both safe
        from any thread), then the ssh kill of the recorded pid and the
        session directory's removal, best-effort: a kill whose ssh
        cannot reach the host any more has still killed everything local
        there was to kill, and the next start launches a fresh kernel
        rather than trusting a half-dead one."""
        try:
            if self._kc is not None:
                try:
                    self._kc.stop_channels()
                except Exception:  # noqa: BLE001
                    pass
            self._kill_tunnel()
        finally:
            self._km = None
            self._kc = None
        if self.remote_pid is not None and not self._torn_down:
            self._torn_down = True
            try:
                self._driver(
                    build_remote_cleanup_command(self.remote_pid, self.session_dir),
                    timeout=30.0,
                )
            except Exception as exc:  # noqa: BLE001 - the local half is done
                _log.debug(
                    "remote cleanup failed for %s on %s: %s",
                    self.session_id,
                    self.host,
                    exc,
                )

    def shutdown(self) -> None:
        """Polite first: the control-channel shutdown request (the kernel
        flushes and exits), a short wait on the heartbeat, then whatever
        is left gets :meth:`kill`."""
        if self._torn_down or self._km is None:
            return
        try:
            if self._kc is not None:
                try:
                    self._kc.shutdown(restart=False)
                except Exception:  # noqa: BLE001 - a dead channel is kill's job
                    _log.debug("remote shutdown request failed", exc_info=True)
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    try:
                        if not self._kc.is_alive():
                            break
                    except Exception:  # noqa: BLE001
                        break
                    time.sleep(0.2)
        finally:
            self.kill()

    # ---- execution (the tunnel-aware wrapper) ----

    def execute(
        self,
        code: str,
        *,
        timeout_s: float = 1800.0,
        callbacks: KernelCallbacks | None = None,
    ) -> CellResult:
        """One cell, with the tunnel kept alive around it.

        Before the cell: a tunnel that died between cells is re-spawned
        — the zmq channels reconnect to the same loopback ports and the
        namespace never notices. During: a watcher thread restores a
        tunnel that dies mid-cell, keeping the disconnected window to
        about a second (a frame emitted inside that window is still
        lost; the loop drains what re-arrives). After: a channel death
        with the kernel still alive re-attaches (fresh tunnel, fresh
        client) and retries the cell once, with the retry disclosed in
        the notice — the same honesty rule the manager applies to its
        own mid-cell retry."""
        if self._kc is None and self._km is None:
            raise RuntimeError("remote kernel has not been started")
        self._ensure_tunnel()
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_tunnel,
            args=(stop,),
            daemon=True,
            name=f"omicsclaw-remote-kernel-{self.session_id[:16]}",
        )
        watcher.start()
        result = None
        try:
            result = super().execute(code, timeout_s=timeout_s, callbacks=callbacks)
        finally:
            stop.set()
            watcher.join(2.0)
        if (
            result is not None
            and result.status == "dead"
            and not self._torn_down
            and self._remote_pid_alive()
        ):
            _log.info(
                "tunnel to %s dropped mid-cell; re-attaching and retrying"
                " the cell on the same kernel",
                self.host,
            )
            self._reattach()
            result = super().execute(code, timeout_s=timeout_s, callbacks=callbacks)
            if result.status != "dead":
                result.notice = (
                    "note: the ssh tunnel to the remote kernel dropped"
                    " mid-cell; the cell was re-run on the same kernel"
                    " and may have partially executed once — side"
                    " effects may have happened twice"
                )
                result.stderr = result.notice + "\n" + result.stderr
            # a retry that died too is returned as the dead cell it is:
            # dressing it as a retry would hide the failure, and the
            # manager's own mid-cell-death handling takes it from here
        return result

    # ---- resource probes (the remote /proc) ----

    def peak_rss_kb(self) -> int | None:
        """The remote kernel's high-water RSS, over ssh. ``None`` on any
        failure — a probe that cannot answer must not fail the cell."""
        pid = self.remote_pid
        if not _valid_pid(pid) or self._torn_down:
            return None
        try:
            outcome = self._driver(
                f"grep VmHWM /proc/{pid}/status 2>/dev/null", timeout=15.0
            )
            return int(outcome.output.split()[1])
        except (ValueError, IndexError, OSError, RuntimeError):
            return None

    def cpu_seconds(self) -> float | None:
        pid = self.remote_pid
        if not _valid_pid(pid) or self._torn_down:
            return None
        try:
            outcome = self._driver(
                f"cat /proc/{pid}/stat 2>/dev/null", timeout=15.0
            )
            fields = outcome.output.rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            # utime + stime, in clock ticks; 100 Hz is USER_HZ on every
            # mainstream Linux — the same assumption /proc tooling makes
            return ticks / 100.0
        except (ValueError, IndexError, OSError, RuntimeError, AttributeError):
            return None

    # ---- the tunnel ----

    def _tunnel_alive(self) -> bool:
        return self._tunnel is not None and self._tunnel.poll() is None

    def _start_tunnel(self, ports: dict[str, Any] | None = None) -> None:
        """Spawn the forwards and wait until a local port answers.

        The wait is a loopback TCP connect against one forwarded port: a
        tunnel that exits instantly (``ExitOnForwardFailure`` on a taken
        port) surfaces here as a start failure, and a tunnel that is
        merely slow to listen does not race the first zmq connect. An
        empty TCP connection to the heartbeat port is inert — the
        kernel's hb socket echoes bytes and ignores none."""
        if self._tunnel_alive():
            return
        info: dict[str, Any] | None = ports
        if info is None:
            if self._local_conn is None:
                raise RemoteKernelTunnelError("no connection info to tunnel")
            info = json.loads(self._local_conn.read_text(encoding="utf-8"))
        argv = build_tunnel_argv(self.host, info)
        self._tunnel = self._tunnel_factory(argv)
        probe_port = int(info["hb_port"])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._tunnel.poll() is not None:
                listed = ", ".join(
                    f"{name}={info[name]}" for name in CONNECTION_PORTS
                )
                raise RemoteKernelTunnelError(
                    f"the ssh port-forward to {self.host} exited"
                    f" immediately (exit {self._tunnel.returncode}); a"
                    f" local port is probably already taken ({listed})"
                )
            if self._port_open(probe_port):
                return
            time.sleep(0.1)
        raise RemoteKernelTunnelError(
            f"the ssh port-forward to {self.host} is running but its"
            f" local port {probe_port} never answered within 10 s"
        )

    def _ensure_tunnel(self) -> None:
        """Between cells: a dead tunnel is re-spawned, never a death."""
        if self._torn_down:
            return
        if not self._tunnel_alive():
            _log.info(
                "tunnel to %s is down between cells; re-spawning the"
                " forwards (the remote kernel keeps its namespace)",
                self.host,
            )
            self._start_tunnel()

    def _watch_tunnel(self, stop: threading.Event) -> None:
        """While a cell runs: restore a tunnel that dies, once per
        second at most. Never raises into the cell — a tunnel that
        cannot come back leaves the cell to its own deadline."""
        while not stop.wait(1.0):
            if self._torn_down:
                return
            if not self._tunnel_alive():
                try:
                    self._start_tunnel()
                except Exception:  # noqa: BLE001 - the cell's deadline is the backstop
                    _log.debug(
                        "tunnel restore failed for %s", self.host, exc_info=True
                    )
                    return

    def _reattach(self) -> None:
        """Fresh tunnel, fresh client, same connection info — for a
        channel death the zmq sockets cannot be trusted to survive."""
        self._kill_tunnel()
        self._start_tunnel()
        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception:  # noqa: BLE001
                pass
        assert self._local_conn is not None  # start() set it before any channel
        self._km, self._kc = self._attach(
            str(self._local_conn), min(self._startup_timeout, 60.0)
        )

    def _kill_tunnel(self) -> None:
        """SIGKILL the forward process and reap it, best-effort.

        ``Popen.kill()`` when the wrapper has one (the ssh -N runs in
        its own session and has no children, so the one signal is the
        whole group); the pid-based group kill is the fallback for a
        wrapper that only names a pid."""
        tunnel = self._tunnel
        self._tunnel = None
        if tunnel is None:
            return
        try:
            kill = getattr(tunnel, "kill", None)
            if callable(kill):
                kill()
            else:
                _kill_process_group(tunnel.pid)
            tunnel.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    def _remote_pid_alive(self) -> bool:
        if not _valid_pid(self.remote_pid):
            return False
        try:
            outcome = self._driver(
                f"kill -0 {self.remote_pid}", timeout=15.0
            )
        except Exception as exc:  # noqa: BLE001 - unreachable proves nothing
            _log.debug(
                "cannot ask %s about pid %s (%s); treating the kernel as"
                " alive rather than recycling over it",
                self.host,
                self.remote_pid,
                exc,
            )
            return True
        return outcome.exit_code == 0


def _kill_process_group(pid: int) -> None:
    """SIGKILL a process's group, tolerating an already-dead one — the
    tunnel spawned with its own session, so the group is exactly the
    ``ssh -N`` and nothing else.

    A pid of zero or less is refused outright: ``os.kill`` reads ``-1``
    as *every process the caller may signal*, and a test double (or a
    torn-down wrapper) handing one in must not become a broadcast. The
    guard is here rather than at the call sites because this is the one
    function whose whole job is aiming a signal; it is
    :func:`_valid_pid`, shared with every splice site."""
    if not _valid_pid(pid):
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ---- the runtime ----------------------------------------------------------------


class RemoteKernelRuntime:
    """The manager's remote half: environments, drivers, launches.

    Holds one :class:`SshSpawner` (the plane's, in a deployment), the
    per-host environment cache (an environment that answered needs
    nothing for as long as this process lives) and the one-time consent
    hook. ``python`` pins a remote interpreter for the whole runtime —
    a curated shared env, or a test's own — and pinning means probing
    only: a pinned interpreter is never installed into.

    The manager calls :meth:`start` under the session's lock, so a
    first-use install or consent wait is serialised exactly like a slow
    cell would be; the consent hook runs on the caller's task, which is
    what lets the chat face's approval card and the job face's
    ``tool_approval_request`` both be the gate without the runtime
    knowing either exists.
    """

    def __init__(
        self,
        spawner: SshSpawner | None = None,
        *,
        consent: Callable[[str], Awaitable[None]] | None = None,
        env_dir: str = DEFAULT_ENV_DIR,
        sessions_dir: str = DEFAULT_SESSIONS_DIR,
        packages: Sequence[str] = CORE_STACK_PACKAGES,
        uv_index_url: str | None = None,
        python: str | None = None,
        install_timeout_s: float = ENV_INSTALL_TIMEOUT_S,
        startup_timeout_s: float = 120.0,
    ) -> None:
        self._spawner = spawner if spawner is not None else SystemSshSpawner()
        self._consent = consent
        self._env_dir = env_dir
        self._sessions_dir = sessions_dir
        self._packages = tuple(packages)
        self._uv_index_url = uv_index_url
        self._python = python
        self._install_timeout_s = float(install_timeout_s)
        self._startup_timeout_s = float(startup_timeout_s)
        self._envs: dict[str, RemoteKernelEnv] = {}
        self._drivers: dict[str, SyncSshRunner] = {}

    async def ensure_env(self, host: str) -> RemoteKernelEnv:
        """The host's kernel environment, probing before anything else.

        Idempotent twice over: within this process the cache answers,
        and on a fresh process the probe does — a host whose environment
        exists is never installed onto, which is the property the
        acceptance test pins (second call, zero installs)."""
        validate_host(host)
        cached = self._envs.get(host)
        if cached is not None:
            return cached
        env = await ensure_remote_kernel_env(
            self._spawner,
            host,
            env_dir=self._env_dir,
            packages=self._packages,
            python=self._python,
            uv_index_url=self._uv_index_url,
            approve=self._consent,
        )
        self._envs[host] = env
        return env

    def driver_for(self, host: str) -> SyncSshRunner:
        """The cached sync driver for a host's kernel methods."""
        validate_host(host)
        driver = self._drivers.get(host)
        if driver is None:
            driver = SyncSshRunner(self._spawner, host)
            self._drivers[host] = driver
        return driver

    async def start(
        self,
        session_id: str,
        *,
        host: str,
        workspace: Path | str,
        limits: OutputLimits | None = None,
        on_figure: Callable[[Path, str], tuple[str, str]] | None = None,
        language: str = "python",
        startup_timeout: float | None = None,
        target_workdir: str = "",
    ) -> RemoteSessionKernel:
        """Ensure the environment, launch the kernel, attach — the
        manager's one call. Raises whatever the environment or the
        launch raised; a refusal here is a failed cell, not a fallback
        to a local kernel (a caller that asked for the remote host and
        got this machine would be worse than an error)."""
        if language != "python":
            raise ValueError(
                "remote kernels are python-only this round; R over"
                " IRkernel rides the language seam when it lands"
            )
        env = await self.ensure_env(host)
        kernel = RemoteSessionKernel(
            session_id,
            host=host,
            workspace=workspace,
            python=env.python,
            driver=self.driver_for(host),
            limits=limits,
            on_figure=on_figure,
            startup_timeout=startup_timeout or self._startup_timeout_s,
            language=language,
            sessions_dir=self._sessions_dir,
            target_workdir=target_workdir,
        )
        await asyncio.to_thread(kernel.start)
        return kernel
