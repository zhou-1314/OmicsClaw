"""Probe isolation and cleanup are checked against real policies and processes."""

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from omicsclaw.entry.ssh.environment_probe import probe_environment
from omicsclaw.sandbox import SandboxConfig, SandboxManager, probe_sandbox_backend
from omicsclaw.sandbox.config import (
    EgressMode,
    IsolationLevel,
    ProbeReport,
    SandboxBackendName,
)


def spec(executable):
    return {
        "language": "python",
        "executable": str(executable),
        "libraryPaths": [],
        "requiredPackages": [],
    }


def identity(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "state": value[0], "start": value[19]}
    except FileNotFoundError:
        return None


def stop_fixture(processes):
    for record in processes:
        actual = identity(record["pid"])
        if actual and actual["start"] == record["start"] and actual["state"] != "Z":
            with contextlib.suppress(ProcessLookupError):
                os.kill(record["pid"], signal.SIGKILL)


def child_fixture(tmp_path, *, detached=False, flood=False):
    binary, records = tmp_path / "probe", tmp_path / "processes.jsonl"
    binary.write_text(f"""#!{sys.executable}
import os,time,json
def record():
 value=open('/proc/self/stat').read().rsplit(')',1)[1].split()
 with open({str(records)!r},'a') as f:
  f.write(json.dumps(dict(pid=os.getpid(),start=value[19]))+'\\n')
record()
if os.fork()==0:
 if {detached!r}: os.setsid()
 record()
 while True:
  if {flood!r}: os.write(1,b'x'*4096)
  else: time.sleep(.01)
time.sleep(60)
""")
    binary.chmod(0o700)
    return binary, records


def test_private_child_home_keeps_host_credentials_masked_in_final_bwrap_policy(
    tmp_path,
):
    async def run():
        host_home, workspace = tmp_path / "host-home", tmp_path / "workspace"
        workspace.mkdir()
        for name in (".ssh", ".aws"):
            (host_home / name).mkdir(parents=True)
            (host_home / name / "synthetic").write_text("fixture")
        executable = host_home / "bin" / "python"
        executable.parent.mkdir()
        executable.symlink_to(sys.executable)
        manager = SandboxManager(
            workspace,
            config=SandboxConfig(egress=EgressMode.DENY),
            report=ProbeReport(
                available=True,
                backend=SandboxBackendName.BWRAP,
                level=IsolationLevel.FULL,
                summary="inspect generated policy",
            ),
            env_provider=lambda: {"HOME": str(host_home)},
        )
        await manager.start()
        captured = {}

        class Capture:
            def wrap(self, *args, **kwargs):
                argv, env = manager.spawn_wrapper().wrap(*args, **kwargs)
                captured.update(argv=argv, env=env)
                raise ValueError("captured_before_spawn")

        try:
            with pytest.raises(ValueError, match="captured_before_spawn"):
                await probe_environment(spec(executable), workspace, wrapper=Capture())
            assert captured["env"]["HOME"] != str(host_home)
            for name in (".ssh", ".aws"):
                index = captured["argv"].index(str(host_home / name))
                assert captured["argv"][index - 1] == "--tmpfs"
                # The selected executable exposes its prefix as a read mount.
                # Its parent must not override a credential mask later in argv.
                assert index > max(
                    i
                    for i, value in enumerate(captured["argv"])
                    if value == str(host_home)
                )
        finally:
            await manager.aclose()

    asyncio.run(run())


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux process identity fixture"
)
@pytest.mark.parametrize(
    "cancel,detached,flood",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, True, True),
    ],
)
def test_real_probe_cleanup_has_a_deadline(tmp_path, cancel, detached, flood):
    binary, records = child_fixture(tmp_path, detached=detached, flood=flood)

    async def run():
        task = asyncio.create_task(
            probe_environment(spec(binary), tmp_path, timeout=0.2, output_limit=1024)
        )
        captured = []
        try:
            for _ in range(100):
                if records.exists() and len(records.read_text().splitlines()) == 2:
                    captured = [
                        json.loads(line) for line in records.read_text().splitlines()
                    ]
                    break
                await asyncio.sleep(0.01)
            assert len(captured) == 2
            started = time.monotonic()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            else:
                with pytest.raises(
                    ValueError, match="probe_output_limit" if flood else "probe_timeout"
                ):
                    await asyncio.wait_for(task, 1)
            assert time.monotonic() - started < 0.9
            assert identity(captured[0]["pid"]) is None
            child = identity(captured[1]["pid"])
            if detached:
                # A process group alone cannot contain a child that created a new session.
                assert child is None or child["start"] == captured[1]["start"]
            else:
                assert child is None or child["state"] == "Z"
        finally:
            stop_fixture(captured)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not probe_sandbox_backend().available,
    reason="Linux native PID containment required",
)
def test_native_pid_namespace_contains_probe_descendant(tmp_path, monkeypatch):
    from omicsclaw.entry.ssh import environment_probe as module
    from omicsclaw.kernel.session import _descendant_pids

    binary, records = child_fixture(tmp_path, detached=True)
    spawn = module.spawn_group_leader
    roots = []

    async def capture(awaitable):
        process = await spawn(awaitable)
        roots.append(process.pid)
        return process

    monkeypatch.setattr(module, "spawn_group_leader", capture)

    async def run():
        manager = SandboxManager(tmp_path, config=SandboxConfig(egress=EgressMode.DENY))
        await manager.start()
        task = None
        captured = []
        try:
            started = time.monotonic()
            task = asyncio.create_task(
                probe_environment(
                    spec(binary), tmp_path, wrapper=manager.spawn_wrapper(), timeout=0.5
                )
            )
            for _ in range(30):
                if (
                    roots
                    and records.exists()
                    and len(records.read_text().splitlines()) == 2
                ):
                    captured = [
                        item
                        for pid in [roots[0], *_descendant_pids(roots[0])]
                        if (item := identity(pid)) is not None
                    ]
                    break
                await asyncio.sleep(0.01)
            assert len(captured) >= 3
            with pytest.raises(ValueError, match="probe_timeout"):
                await task
            assert time.monotonic() - started < 1.5
            for _ in range(50):
                living = [
                    record
                    for record in captured
                    if (actual := identity(record["pid"]))
                    and actual["start"] == record["start"]
                    and actual["state"] != "Z"
                ]
                if not living:
                    break
                await asyncio.sleep(0.01)
            assert not living
        finally:
            stop_fixture(captured)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await manager.aclose()

    asyncio.run(run())
