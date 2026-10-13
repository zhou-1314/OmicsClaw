"""C3 over a real SSH hop: this container's own ``localhost``.

The unit suites run over fakes; these few run the actual wire — launch
a remote ipykernel through the real ``ssh``, tunnel its five ports,
attach with the real :mod:`jupyter_client`, and hold the manager to the
plan's acceptance scenarios:

* two rounds of code on one session, the second referencing the first
  round's variable — the namespace lives on the remote kernel;
* the tunnel process killed between cells: the next cell re-spawns the
  forwards, re-attaches, and the variable is still there — no cold
  start, no obituary;
* the environment supply is idempotent: a second ``ensure_env`` issues
  no ssh at all, and a *fresh* runtime over the same host probes once
  and installs nothing;
* an explicit shutdown leaves nothing behind: the tunnel process is
  gone, the remote pid is gone, the remote session directory is gone;
* nothing launches before the first cell asks for one.

Skipped (not failed) wherever ``ssh localhost`` is not true, like the
C1 integration suite beside it. The "remote" python is this process's
own interpreter (the OmicsClaw env's — it has ipykernel), passed as the
runtime's pinned interpreter: the provisioning code paths that would
hit the network are covered by the unit suites' command construction,
not by downloading scanpy here.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.skipif(
        shutil.which("ssh") is None, reason="no ssh client on this machine"
    ),
]
pytest.importorskip("jupyter_client", reason="C3 needs jupyter_client")
pytest.importorskip("ipykernel", reason="C3 needs ipykernel")

from omicsclaw.kernel import PersistentKernelManager
from omicsclaw.remote.kernel import RemoteKernelRuntime
from omicsclaw.remote.ssh import SystemSshSpawner, build_ssh_argv

HOST = "localhost"


def _self_reachable() -> bool:
    try:
        probe = subprocess.run(
            build_ssh_argv(HOST, "true"),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark.append(
    pytest.mark.skipif(
        not _self_reachable(), reason="ssh localhost is not reachable here"
    )
)


class CountingSpawner(SystemSshSpawner):
    """The real spawner, with a command log bolted on."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def spawn(self, argv, *, timeout, stdin_text=None):
        if argv and argv[0] != "sftp":
            self.commands.append(argv[-1])
        return await super().spawn(argv, timeout=timeout, stdin_text=stdin_text)


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "OMICSCLAW.md").write_text("c3 ssh tests", encoding="utf-8")
    return tmp_path


@pytest.fixture
def spawner() -> CountingSpawner:
    return CountingSpawner()


@pytest.fixture
def runtime(spawner: CountingSpawner) -> RemoteKernelRuntime:
    # the pinned interpreter is the fast path the plan allows: probe,
    # never install — this env already has ipykernel
    return RemoteKernelRuntime(spawner, python=sys.executable)


def _remote_state(manager: PersistentKernelManager, session: str):
    return manager.state_for(session, location=f"remote:{HOST}")


def _wait_reaped(process, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)


def _ssh_exit(command: str) -> int:
    probe = subprocess.run(
        build_ssh_argv(HOST, command),
        capture_output=True,
        timeout=30,
        check=False,
    )
    return probe.returncode


def _sh(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


# ---- the acceptance scenarios -------------------------------------------------


def test_two_rounds_of_code_share_the_remote_namespace(workspace, runtime):
    async def scenario():
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            first = await manager.execute(
                "c3-acc-1",
                "remote_marker = 41\nprint('cell one')",
                location=f"remote:{HOST}",
            )
            assert first.status == "ok", first.stderr
            assert "cell one" in first.stdout
            second = await manager.execute(
                "c3-acc-1",
                "print('answer', remote_marker + 1)",
                location=f"remote:{HOST}",
            )
            assert second.status == "ok", second.stderr
            assert "answer 42" in second.stdout
            # the kernel really is elsewhere: its pid is not ours, and
            # the tunnel that carries it is standing
            kernel = _remote_state(manager, "c3-acc-1").kernel
            assert kernel.remote_pid != os.getpid()
            assert kernel._tunnel.poll() is None
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 240))


def test_a_killed_tunnel_re_attaches_and_the_variables_survive(workspace, runtime):
    async def scenario():
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            await manager.execute(
                "c3-acc-2",
                "survivor = 'still here'",
                location=f"remote:{HOST}",
                timeout_s=30,
            )
            kernel = _remote_state(manager, "c3-acc-2").kernel
            first_tunnel = kernel._tunnel
            # the forward process dies under us — a laptop sleep, a
            # network blip — while the remote kernel lives on
            os.killpg(os.getpgid(first_tunnel.pid), signal.SIGKILL)
            first_tunnel.wait(timeout=10)
            assert first_tunnel.poll() is not None
            # the kernel's own liveness question does not confuse the
            # two failure modes: the pid answers over ssh
            assert kernel.is_alive() is True
            second = await manager.execute(
                "c3-acc-2",
                "print('after the outage:', survivor)",
                location=f"remote:{HOST}",
                timeout_s=30,
            )
            assert second.status == "ok", second.stderr
            assert "after the outage: still here" in second.stdout
            # no obituary — the namespace was never gone
            assert "kernel restarted" not in second.stderr
            # and a new forward carries the same kernel
            assert kernel._tunnel is not first_tunnel
            assert kernel._tunnel.poll() is None
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 240))


def test_the_environment_supply_is_idempotent(spawner):
    async def scenario():
        runtime = RemoteKernelRuntime(spawner, python=sys.executable)
        first = await runtime.ensure_env(HOST)
        assert first.provisioned is False
        after_first = len(spawner.commands)
        # the cached second call issues nothing at all
        second = await runtime.ensure_env(HOST)
        assert second.python == first.python
        assert len(spawner.commands) == after_first
        # and a fresh runtime (a restarted backend) probes once and
        # installs nothing — no uv anywhere in its transcript
        fresh_spawner = CountingSpawner()
        fresh = RemoteKernelRuntime(fresh_spawner, python=sys.executable)
        env = await fresh.ensure_env(HOST)
        assert env.provisioned is False
        assert not any("uv" in c for c in fresh_spawner.commands)
        assert len(fresh_spawner.commands) == 1

    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_shutdown_cleans_the_tunnel_the_pid_and_the_session_dir(workspace, runtime):
    async def scenario():
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        tunnel = pid = session_dir = None
        try:
            await manager.execute("c3-acc-4", "bye = 1", location=f"remote:{HOST}")
            kernel = _remote_state(manager, "c3-acc-4").kernel
            # captured before the close: teardown nulls the kernel's own
            # references, and the test needs the records to verify with
            tunnel, pid = kernel._tunnel, kernel.remote_pid
            session_dir = kernel.session_dir
        finally:
            await manager.aclose()
        # the forward process is reaped
        _wait_reaped(tunnel)
        assert tunnel.poll() is not None
        # the remote kernel process is gone (kill -0 fails)
        assert _ssh_exit(f"kill -0 {pid}") != 0
        # and the session directory was removed
        assert _ssh_exit(f"test -d {_sh(session_dir)}") != 0

    asyncio.run(asyncio.wait_for(scenario(), 240))


def test_the_kernel_is_lazy_nothing_launches_before_the_first_cell(
    workspace, runtime, spawner
):
    async def scenario():
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            assert not any("ipykernel_launcher" in c for c in spawner.commands), (
                "a kernel launched before any cell asked for one"
            )
            await manager.execute(
                "c3-acc-5", "print('first cell')", location=f"remote:{HOST}"
            )
            assert any("ipykernel_launcher" in c for c in spawner.commands)
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 240))
