"""C3's kernel half: the launch, the tunnel, and the lifecycle.

No network, no jupyter: the ssh side is a recording driver double, the
tunnel a fake process, the attach a fake ``(manager, client)`` pair and
the client a scripted iopub/shell speaker. What these pin:

* the launch command's shape — setsid/nohup detachment, the kernel
  writing its own connection file, the pid the detached shell records,
  the markers, and the self-clean of a kernel that never answered;
* the connection-file rewrite (loopback only) and the local copy under
  the workspace scratch;
* the tunnel argv — five loopback-bound forwards, ``-N``, no shared
  ControlMaster, ``ExitOnForwardFailure``;
* the lifecycle state machine — a failed launch raises with nothing
  left behind, ``kill`` tears down tunnel and remote process by the
  recorded pid and is idempotent, a tunnel that died between cells is
  re-spawned and the cell still sees the namespace, ``interrupt`` is a
  remote SIGINT, and the usage probes read the remote ``/proc``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from omicsclaw.remote.kernel import (
    RemoteKernelLaunchError,
    RemoteKernelTunnelError,
    RemoteSessionKernel,
    build_kernel_launch_command,
    build_tunnel_argv,
    parse_launch_output,
    rewrite_connection_info,
)
from omicsclaw.tools.builtin.remote import ExecOutcome

CONN = {
    "shell_port": 53101,
    "iopub_port": 53102,
    "stdin_port": 53103,
    "hb_port": 53104,
    "control_port": 53105,
    "ip": "127.0.0.1",
    "key": "a-fresh-random-key",
    "transport": "tcp",
}


def _launch_output(conn: dict | None = CONN, pid: int = 4242) -> str:
    body = (
        "===OMICSCLAW_KERNEL_CONN===\n"
        + json.dumps(conn, indent=1)
        + "\n===OMICSCLAW_KERNEL_PID===\n"
        + str(pid)
        + "\n"
    )
    return body if conn is not None else "===OMICSCLAW_KERNEL_LOG===\nboom\n"


class FakeDriver:
    """The sync ssh double: records commands, answers by needle."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.rules: list[tuple[str, ExecOutcome]] = []

    def on(self, needle: str, outcome: ExecOutcome) -> FakeDriver:
        self.rules.insert(0, (needle, outcome))
        return self

    def __call__(self, command: str, *, timeout: float = 30.0) -> ExecOutcome:
        self.calls.append((command, timeout))
        for needle, outcome in self.rules:
            if needle in command:
                return outcome
        return ExecOutcome(output="", exit_code=0)

    def commands(self) -> list[str]:
        return [command for command, _ in self.calls]


class FakeTunnelProcess:
    """The ``ssh -N`` double: alive until killed, remembers its argv.

    ``pid`` is a deliberately nonexistent one: the teardown's killpg
    then raises ProcessLookupError, which it tolerates. A pid of 0 or
    -1 here would be a loaded gun — ``os.kill(-1, ...)`` is a broadcast
    — and the kernel's own guard refuses those anyway; the double must
    not rely on being saved by it."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.killed = False

    def poll(self) -> int | None:
        return 9 if self.killed else None

    @property
    def returncode(self) -> int | None:
        return 9 if self.killed else None

    def kill(self) -> None:
        """The Popen-shaped teardown hook the kernel calls."""
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 9

    @property
    def pid(self) -> int:
        return 999_999

    def die(self) -> None:
        """Simulate the forward process dying under us."""
        self.killed = True


class FakeClient:
    """A scripted blocking client: one iopub stream frame, then idle."""

    def __init__(self) -> None:
        self.started_channels = False
        self.stopped = False
        self.shutdown_requested = False
        self.beating = True
        self.executed: list[str] = []
        self.sent: list[str] = []
        self.channel_boom = False
        """Raised-from flag: when set, the iopub channel is dead and
        every read raises — the mid-cell channel-death scenario."""

    def start_channels(self) -> None:
        self.started_channels = True
        self.stopped = False

    def stop_channels(self) -> None:
        self.stopped = True

    def is_alive(self) -> bool:
        return self.beating

    def shutdown(self, restart: bool = False) -> str:
        # a kernel that honours the request exits, so its heartbeat
        # stops — the wait loop in shutdown() keys on exactly that
        self.shutdown_requested = True
        self.beating = False
        return "shutdown-msg-id"

    def execute(self, code: str) -> str:
        self.executed.append(code)
        self.sent.append(code)
        return "msg-1"

    def get_iopub_msg(self, timeout: float = 1.0):
        from queue import Empty

        if self.channel_boom:
            raise ValueError("the channel closed under us")
        if self.executed and not self.stopped:
            code = self.executed.pop(0)
            frames = [
                {
                    "parent_header": {"msg_id": "msg-1"},
                    "header": {"msg_type": "stream"},
                    "content": {"name": "stdout", "text": f"ran {code!r}\n"},
                },
                {
                    "parent_header": {"msg_id": "msg-1"},
                    "header": {"msg_type": "status"},
                    "content": {"execution_state": "idle"},
                },
            ]
            self.executed = []
            self._frames = frames
        if getattr(self, "_frames", None):
            return self._frames.pop(0)
        raise Empty

    def get_shell_msg(self, timeout: float = 2.0):
        return {
            "msg_id": "reply-1",
            "parent_header": {"msg_id": "msg-1"},
            "content": {"execution_count": 3},
        }


class FakeAttach:
    """The attach double: hands out its clients in order, records files.

    One client is the common case; the mid-cell retry path wants a
    second, healthier one to be handed to the re-attach."""

    def __init__(self, *clients: FakeClient) -> None:
        self.clients = list(clients)
        self.files: list[str] = []

    def __call__(self, connection_file: str, timeout: float):
        self.files.append(connection_file)
        index = min(len(self.files) - 1, len(self.clients) - 1)
        client = self.clients[index]
        client.start_channels()  # the attach contract
        return object(), client


def _kernel(
    tmp_path: pathlib.Path,
    driver: FakeDriver,
    client: FakeClient,
    tunnels: list[FakeTunnelProcess] | None = None,
) -> tuple[RemoteSessionKernel, FakeAttach, list[FakeTunnelProcess]]:
    made: list[FakeTunnelProcess] = tunnels if tunnels is not None else []

    def factory(argv: list[str]):
        process = FakeTunnelProcess()
        process.argv = list(argv)
        made.append(process)
        return process

    attach = FakeAttach(client)
    kernel = RemoteSessionKernel(
        "sess-1",
        host="lab1",
        workspace=tmp_path,
        python="/opt/envs/k/bin/python",
        driver=driver.on(
            "ipykernel_launcher", ExecOutcome(output=_launch_output(), exit_code=0)
        ),
        tunnel_factory=factory,
        attach=attach,
        port_open=lambda port: True,  # no real listener in a unit test
    )
    return kernel, attach, made


# ---- the pure builders ------------------------------------------------------


def test_the_launch_command_detaches_and_lets_the_kernel_pick_the_ports():
    command = build_kernel_launch_command(
        "/opt/envs/k/bin/python", "~/.omicsclaw/kernel-sessions/s-1"
    )
    assert "setsid nohup" in command
    assert "ipykernel_launcher" in command
    assert "-f" in command and "kernel.json" in command
    assert "mkdir -p -m 700" in command and "umask 077" in command
    # the kernel writes its own file: no port is ever named by us
    assert "5310" not in command
    # the pid is recorded by the detached shell, not by $! guessing
    assert 'echo $$ > "$1/kernel.pid"' in command
    assert "exec" in command
    # a kernel that never answers is killed by its own pid file
    assert "kill -KILL $(cat kernel.pid)" in command


def test_the_launch_command_uses_the_frozen_target_workdir_as_kernel_cwd():
    command = build_kernel_launch_command(
        "/opt/envs/k/bin/python",
        "~/.omicsclaw/kernel-sessions/s-1",
        "/scratch/frozen-job",
    )
    assert "mkdir -p -m 700" in command and "/scratch/frozen-job" in command
    assert "cd '/scratch/frozen-job'" in command


def test_parse_launch_output_reads_the_conn_and_the_pid():
    info, pid = parse_launch_output(_launch_output())
    assert pid == 4242
    assert info["hb_port"] == CONN["hb_port"]
    assert info["key"] == CONN["key"]


def test_parse_launch_output_without_a_conn_carries_the_log():
    with pytest.raises(RemoteKernelLaunchError) as caught:
        parse_launch_output(_launch_output(conn=None))
    assert "no connection file" in str(caught.value)
    assert "boom" in str(caught.value)


def test_parse_launch_output_refuses_a_partial_conn():
    broken = dict(CONN)
    broken.pop("control_port")
    with pytest.raises(RemoteKernelLaunchError):
        parse_launch_output(_launch_output(conn=broken))


def test_the_rewrite_pins_loopback_and_keeps_ports():
    widened = dict(CONN, ip="0.0.0.0")
    rewritten = rewrite_connection_info(widened)
    assert rewritten["ip"] == "127.0.0.1"
    assert rewritten["transport"] == "tcp"
    for name in ("shell_port", "iopub_port", "stdin_port", "hb_port", "control_port"):
        assert rewritten[name] == CONN[name]


def test_the_tunnel_argv_forwards_five_loopback_ports_and_shares_nothing():
    argv = build_tunnel_argv("lab1", CONN)
    forwards = [part for part in argv if part.startswith("127.0.0.1:")]
    assert forwards == [
        f"127.0.0.1:{CONN[name]}:127.0.0.1:{CONN[name]}"
        for name in (
            "shell_port",
            "iopub_port",
            "stdin_port",
            "hb_port",
            "control_port",
        )
    ]
    assert argv[-3:] == ["-N", "--", "lab1"]
    assert "ExitOnForwardFailure=yes" in argv
    assert "ControlMaster=no" in argv and "ControlPath=none" in argv
    assert "BatchMode=yes" in argv and "ForwardAgent=no" in argv


# ---- start --------------------------------------------------------------------


def test_start_launches_tunnels_and_attaches(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, attach, tunnels = _kernel(tmp_path, driver, client)
    kernel.start()
    assert kernel.remote_pid == 4242
    assert kernel.session_dir.startswith("~/.omicsclaw/kernel-sessions/")
    assert "ipykernel_launcher" in driver.commands()[0]
    # the tunnel carries the five ports of the *rewritten* file
    assert len(tunnels) == 1
    # the local conn file is the rewritten one, under the workspace
    local = json.loads(pathlib.Path(attach.files[0]).read_text())
    assert tunnels[0].argv == build_tunnel_argv("lab1", local, remote_ports=CONN)
    assert local["ip"] == "127.0.0.1"
    assert attach.files[0].startswith(str(tmp_path))
    assert client.started_channels


def test_a_failed_launch_raises_and_leaves_no_tunnel(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, tunnels = _kernel(tmp_path, driver, client)
    # added after the helper's launch rule, so it wins for this test
    driver.on("mkdir", ExecOutcome(output="denied", exit_code=1))
    with pytest.raises(RemoteKernelLaunchError):
        kernel.start()
    assert tunnels == []


def test_a_tunnel_that_cannot_bind_fails_the_start(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, _tunnels = _kernel(tmp_path, driver, client)

    def dead_factory(argv: list[str]):
        process = FakeTunnelProcess()
        process.argv = list(argv)
        process.killed = True  # exited instantly: port taken
        return process

    kernel._tunnel_factory = dead_factory
    with pytest.raises(RemoteKernelTunnelError):
        kernel.start()
    # and the remote kernel it had launched is cleaned up by pid —
    # the cleanup *command*, not the launch text (which carries its
    # own self-clean shell line)
    cleanup = [c for c in driver.commands() if c.startswith("kill -KILL 4242")]
    assert cleanup


# ---- lifecycle ----------------------------------------------------------------


def test_kill_tears_down_tunnel_and_remote_by_the_recorded_pid(
    tmp_path: pathlib.Path,
):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, tunnels = _kernel(tmp_path, driver, client)
    kernel.start()
    kernel.kill()
    assert tunnels[0].killed
    cleanup = [c for c in driver.commands() if c.startswith("kill -KILL 4242")]
    assert len(cleanup) == 1
    assert "rm -rf" in cleanup[0]
    assert kernel.is_alive() is False
    # idempotent: a second kill issues nothing further
    kernel.kill()
    assert len([c for c in driver.commands() if c.startswith("kill -KILL 4242")]) == 1


def test_shutdown_is_polite_then_fatal(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, tunnels = _kernel(tmp_path, driver, client)
    kernel.start()
    kernel.shutdown()
    assert client.shutdown_requested is True  # the control-channel request
    assert client.stopped is True
    assert tunnels[0].killed
    assert any(c.startswith("kill -KILL 4242") for c in driver.commands())


def test_interrupt_is_a_remote_sigint(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, _ = _kernel(tmp_path, driver, client)
    kernel.start()
    kernel.interrupt()
    assert driver.commands()[-1] == "kill -INT 4242"


def test_is_alive_asks_ssh_when_the_heartbeat_is_silent(
    tmp_path: pathlib.Path,
):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, _ = _kernel(tmp_path, driver, client)
    kernel.start()
    # healthy: the heartbeat answers, ssh is not consulted
    assert kernel.is_alive() is True
    assert not any("kill -0" in c for c in driver.commands())
    # tunnel dead, kernel alive: ssh says the pid lives — this is the
    # answer that keeps a re-attach from becoming a cold start
    client.beating = False
    driver.on("kill -0 4242", ExecOutcome(exit_code=0))
    assert kernel.is_alive() is True
    # kernel dead: ssh says the pid is gone
    driver.on("kill -0 4242", ExecOutcome(exit_code=1))
    assert kernel.is_alive() is False


def test_the_usage_probes_read_the_remote_proc(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, _tunnels = _kernel(tmp_path, driver, client)
    kernel.start()
    driver.on("VmHWM", ExecOutcome(output="VmHWM:\t 1048576 kB\n", exit_code=0))
    driver.on(
        "cat /proc/4242/stat",
        ExecOutcome(
            output="4242 (python) S 1 4242 4242 0 -1 0 100 0 0 0 12 34 0 0 20 0 1 0\n",
            exit_code=0,
        ),
    )
    assert kernel.peak_rss_kb() == 1048576
    # utime=12 + stime=34 ticks at 100 Hz
    assert kernel.cpu_seconds() == pytest.approx(0.46)
    # a probe that cannot answer is None, never an exception
    driver.on("VmHWM", ExecOutcome(output="", exit_code=1))
    assert kernel.peak_rss_kb() is None


# ---- the tunnel-aware execute -------------------------------------------------


def test_a_cell_runs_through_the_inherited_loop(tmp_path: pathlib.Path):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, _ = _kernel(tmp_path, driver, client)
    kernel.start()
    result = kernel.execute("marker = 1\nprint('ran')", timeout_s=30.0)
    assert result.status == "ok"
    assert "ran" in result.stdout


def test_a_dead_tunnel_between_cells_is_respawned_not_a_death(
    tmp_path: pathlib.Path,
):
    driver, client = FakeDriver(), FakeClient()
    kernel, attach, tunnels = _kernel(tmp_path, driver, client)
    kernel.start()
    first = tunnels[0]
    first.die()  # the forward process is killed between cells
    result = kernel.execute("print('still here')", timeout_s=30.0)
    assert result.status == "ok"
    assert len(tunnels) == 2  # a second forward was raised
    assert len(attach.files) == 2  # a ready channel before sending the next cell
    assert kernel.remote_pid == 4242  # the same kernel, never restarted


# ---- the pid guard: no signal, command or syscall, from a bad pid -------------
#
# The review's probe semantics, inverted into regressions: a pid of
# zero or less must be refused everywhere it is spliced, and the refusal
# must cost zero kill signals.


def test_the_group_kill_refuses_nonpositive_pids(monkeypatch):
    """Probe 1, formalised: _kill_process_group with -1/0/None fires
    nothing — not even getpgid."""
    import os as _os

    from omicsclaw.remote.kernel import _kill_process_group

    fired: list[tuple] = []
    monkeypatch.setattr(_os, "kill", lambda *a: fired.append(("kill", a)))
    monkeypatch.setattr(_os, "killpg", lambda *a: fired.append(("killpg", a)))
    monkeypatch.setattr(
        _os,
        "getpgid",
        lambda p: (_ for _ in ()).throw(
            AssertionError("getpgid reached for a nonpositive pid")
        ),
    )
    for bad in (-1, 0, None, "4242"):
        _kill_process_group(bad)  # type: ignore[arg-type]
    assert fired == []


def test_parse_launch_output_refuses_nonpositive_pids():
    """Half of probe 2, inverted: a launch that reports -1 (or 0) is
    refused whole, so the pid never reaches a cleanup command."""
    for bad in ("-1", "0"):
        text = (
            "===OMICSCLAW_KERNEL_CONN===\n"
            + json.dumps(CONN)
            + f"\n===OMICSCLAW_KERNEL_PID===\n{bad}\n"
        )
        with pytest.raises(RemoteKernelLaunchError, match="names no one process"):
            parse_launch_output(text)


def test_the_cleanup_command_refuses_nonpositive_pids():
    """The other half of probe 2: 'kill -KILL -1' can no longer be
    built, by anyone, from any pid."""
    from omicsclaw.remote.kernel import build_remote_cleanup_command

    for bad in (-1, 0, None):
        with pytest.raises(ValueError, match="broadcast ids"):
            build_remote_cleanup_command(bad, "~/.omicsclaw/kernel-sessions/s")


def test_a_corrupted_pid_reaches_no_signal_and_no_probe(tmp_path):
    """A started kernel whose pid goes bad (the fields are plain
    attributes) must issue no kill, no kill -0, no /proc read — and its
    own teardown still cleans the local half."""
    driver, client = FakeDriver(), FakeClient()
    kernel, _, tunnels = _kernel(tmp_path, driver, client)
    kernel.start()
    kernel.remote_pid = -1  # the corrupted record under test
    client.beating = False  # so liveness must fall through to the pid
    kernel.interrupt()
    assert not any(c.startswith("kill -INT") for c in driver.commands())
    assert kernel.is_alive() is False
    assert not any(c.startswith("kill -0") for c in driver.commands())
    assert kernel.peak_rss_kb() is None
    assert not any("/proc/" in c for c in driver.commands())
    kernel.kill()
    assert tunnels[0].killed  # the local teardown still happened
    # and nothing that starts like a kill command was ever sent (the
    # launch command embeds its own self-clean text and does not count)
    assert not any(c.startswith("kill") for c in driver.commands())


# ---- the mid-cell channel death: re-attach, retry once, disclose --------------


def test_a_mid_cell_channel_death_reattaches_and_retries_the_cell(
    tmp_path: pathlib.Path,
):
    driver, healthy = FakeDriver(), FakeClient()
    broken = FakeClient()
    broken.channel_boom = True  # the iopub channel dies under the cell
    made: list[FakeTunnelProcess] = []

    def factory(argv: list[str]):
        process = FakeTunnelProcess()
        process.argv = list(argv)
        made.append(process)
        return process

    attach = FakeAttach(broken, healthy)
    kernel = RemoteSessionKernel(
        "sess-retry",
        host="lab1",
        workspace=tmp_path,
        python="/opt/envs/k/bin/python",
        driver=driver.on(
            "ipykernel_launcher",
            ExecOutcome(output=_launch_output(pid=4242), exit_code=0),
        ).on("kill -0 4242", ExecOutcome(exit_code=0)),  # the kernel lives
        tunnel_factory=factory,
        attach=attach,
        port_open=lambda port: True,
    )
    kernel.start()
    result = kernel.execute("marker = 1\nprint('retry me')", timeout_s=30.0)
    assert result.status == "ok", result.stderr
    assert "retry me" in result.stdout
    # the cell was sent twice — once into the dead channel, once on the
    # re-attached one — and both sends are disclosed as one retry
    assert len(broken.sent) == 1 and len(healthy.sent) == 1
    assert "tunnel to the remote kernel dropped mid-cell" in result.notice
    assert result.stderr.startswith("note:")
    # a fresh tunnel was raised and a fresh attach was made
    assert len(made) == 2 and len(attach.files) == 2
    assert kernel.remote_pid == 4242  # the same kernel, never relaunched


def test_the_mid_cell_retry_happens_once_never_twice(tmp_path: pathlib.Path):
    """Both clients dead: the retry itself dies and is reported as-is —
    no third send, no loop, no notice dressing up a dead cell."""
    driver = FakeDriver()
    broken_one, broken_two = FakeClient(), FakeClient()
    broken_one.channel_boom = True
    broken_two.channel_boom = True
    made: list[FakeTunnelProcess] = []

    def factory(argv: list[str]):
        process = FakeTunnelProcess()
        process.argv = list(argv)
        made.append(process)
        return process

    attach = FakeAttach(broken_one, broken_two)
    kernel = RemoteSessionKernel(
        "sess-once",
        host="lab1",
        workspace=tmp_path,
        python="/opt/envs/k/bin/python",
        driver=driver.on(
            "ipykernel_launcher",
            ExecOutcome(output=_launch_output(pid=4242), exit_code=0),
        ).on("kill -0 4242", ExecOutcome(exit_code=0)),
        tunnel_factory=factory,
        attach=attach,
        port_open=lambda port: True,
    )
    kernel.start()
    result = kernel.execute("print('never lands')", timeout_s=30.0)
    assert result.status == "dead"
    assert len(broken_one.sent) == 1 and len(broken_two.sent) == 1
    assert len(attach.files) == 2  # the original attach and one re-attach
    assert result.notice == ""  # a dead cell is not dressed as a retry


def test_tunnel_can_use_distinct_local_and_remote_ports():
    local = dict(CONN, hb_port=54104)
    argv = build_tunnel_argv("lab1", local, remote_ports=CONN)
    assert "127.0.0.1:54104:127.0.0.1:53104" in argv


def test_local_port_allocation_failure_cleans_remote_kernel(tmp_path, monkeypatch):
    driver, client = FakeDriver(), FakeClient()
    kernel, _, tunnels = _kernel(tmp_path, driver, client)

    def exhausted(*args, **kwargs):
        raise OSError("socket unavailable")

    monkeypatch.setattr("omicsclaw.remote.kernel.socket.socket", exhausted)
    with pytest.raises(OSError, match="socket unavailable"):
        kernel.start()
    assert any("kill" in command and "4242" in command for command in driver.commands())
    assert not tunnels
