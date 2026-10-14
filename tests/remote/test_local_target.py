"""The same executor runs external jobs on the controller host without SSH."""

import asyncio
import hashlib
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omicsclaw.remote.plane import RemoteExecutor
from omicsclaw.remote.ssh import (
    RemoteHostRefused,
    RemotePathRefused,
    build_sftp_argv,
    build_sftp_batch,
    build_ssh_argv,
)


def test_local_executor_reuses_real_process_claim_without_ssh_credentials(tmp_path):
    from omicsclaw.remote.local import LocalTargetSpawner

    async def scenario():
        transport = LocalTargetSpawner(tmp_path, alias="controller")
        executor = RemoteExecutor("controller", transport)
        directory = str(tmp_path / "job")
        command = "printf once >> counter.txt"
        intent = {
            "request_id": "local-once",
            "payload_hash": hashlib.sha256(command.encode()).hexdigest(),
        }
        first = await executor.submit_in(directory, command, **intent)
        second = await executor.submit_in(directory, command, **intent)
        deadline = time.monotonic() + 5
        while (
            await executor.status(first)
        ).state != "done" and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert (
            first.pgid,
            second.pgid,
            (tmp_path / "job" / "counter.txt").read_text(),
        ) == (first.pgid, first.pgid, "once")

    asyncio.run(scenario())


def test_owned_nested_inputs_are_staged_before_one_launch(tmp_path):
    from omicsclaw.remote.local import LocalTargetSpawner

    source = tmp_path / "input.txt"
    source.write_text("owned nested input")

    async def scenario():
        executor = RemoteExecutor("controller", LocalTargetSpawner(tmp_path))
        workdir = tmp_path / "job"
        handle = await executor.submit_in(
            str(workdir),
            "cat data/nested/input.txt > output.txt",
            uploads=[(str(source), str(workdir / "data/nested/input.txt"))],
        )
        deadline = time.monotonic() + 5
        while (await executor.status(handle)).state not in {"done", "failed"}:
            if time.monotonic() > deadline:
                raise TimeoutError("owned nested input job")
            await asyncio.sleep(0.02)
        assert (workdir / "output.txt").read_text() == "owned nested input"

    asyncio.run(scenario())


def test_local_binding_refuses_another_target_before_process_start(tmp_path):
    from omicsclaw.remote.local import LocalTargetSpawner

    async def scenario():
        transport = LocalTargetSpawner(tmp_path, alias="controller")
        with pytest.raises(RemoteHostRefused):
            await transport.spawn(build_ssh_argv("other-target", "true"), timeout=1)

    asyncio.run(scenario())


def test_local_transfer_refuses_a_symlink_escaping_the_bound_root(tmp_path):
    from omicsclaw.remote.local import LocalTargetSpawner

    root = tmp_path / "owned"
    root.mkdir()
    (root / "input.txt").write_text("input")
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)

    async def scenario():
        transport = LocalTargetSpawner(root, alias="controller")
        with pytest.raises(RemotePathRefused):
            await transport.spawn(
                build_sftp_argv("controller"),
                timeout=1,
                stdin_text=build_sftp_batch(
                    puts=[
                        (str(root / "input.txt"), str(root / "escape" / "outside.txt"))
                    ]
                ),
            )
        assert not (tmp_path / "outside.txt").exists()

    asyncio.run(scenario())


def test_native_workdir_escape_is_refused_before_directory_creation(tmp_path):
    from omicsclaw.remote.local import LocalTargetSpawner

    root = tmp_path / "owned-root"
    root.mkdir()
    outside = tmp_path / "outside-root"

    async def scenario():
        executor = RemoteExecutor("controller", LocalTargetSpawner(root))
        with pytest.raises(RemotePathRefused):
            await executor.submit_in(str(outside), "true")
        assert not outside.exists()

    asyncio.run(scenario())


@pytest.mark.skipif(
    sys.platform != "linux", reason="process birth proof requires Linux /proc"
)
@pytest.mark.parametrize("closure", ["sigkill", "eof"])
def test_same_local_external_business_job_survives_owned_controller_close(
    tmp_path, closure
):
    fixture = Path(__file__).with_name("_persistent_job_controller.py")
    nonce = secrets.token_hex(16)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(fixture.parents[2]),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with (tmp_path / "owned-controller-stderr.log").open("w") as capture:
        process = subprocess.Popen(
            [
                sys.executable,
                str(fixture),
                str(tmp_path),
                "eof" if closure == "eof" else "launch",
                nonce,
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=capture,
            stdin=subprocess.PIPE if closure == "eof" else subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        birth = Path(f"/proc/{process.pid}/stat").read_text().rpartition(")")[2].split()
        try:
            readable, _, _ = select.select([process.stdout], [], [], 12)
            assert readable, "owned_controller_readiness_timeout"
            launched = json.loads(process.stdout.readline())
            actual = (
                Path(f"/proc/{process.pid}/stat").read_text().rpartition(")")[2].split()
            )
            assert (
                launched["controllerPid"],
                launched["controllerUid"],
                launched["controllerStartTicks"],
                launched["controllerNonce"],
                launched["workspace"],
                int(actual[19]),
            ) == (
                process.pid,
                os.getuid(),
                int(birth[19]),
                nonce,
                str(tmp_path),
                int(birth[19]),
            )
            if closure == "sigkill":
                process.send_signal(signal.SIGKILL)
            else:
                process.stdin.close()
            process.wait(timeout=5)
            proof = launched["handleProcess"]
            worker = (
                Path(f"/proc/{proof['pid']}/stat")
                .read_text()
                .rpartition(")")[2]
                .split()
            )
            assert (int(worker[19]), int(worker[2]), int(worker[3])) == (
                proof["start_ticks"],
                proof["pgid"],
                proof["sid"],
            )
            restored = subprocess.run(
                [
                    sys.executable,
                    str(fixture),
                    str(tmp_path),
                    "recover",
                    secrets.token_hex(16),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            recovered = json.loads(restored.stdout)
            (tmp_path / "owned-lifecycle-proof.json").write_text(
                json.dumps(
                    {"closure": closure, "launched": launched, "recovered": recovered},
                    sort_keys=True,
                )
            )
            assert (
                recovered["businessJobId"],
                recovered["remoteRowId"],
                recovered["handleProcess"],
                recovered["runtime"],
                recovered["lifetime"],
                recovered["status"],
                recovered["counter"],
                recovered["jobs"],
                recovered["submissions"],
                recovered["terminalEvents"],
                recovered["executionEvents"],
                recovered["artifacts"],
                recovered["observers"],
                recovered["scienceImports"],
            ) == (
                launched["businessJobId"],
                launched["remoteRowId"],
                proof,
                "local",
                "external",
                "succeeded",
                "once\n",
                1,
                1,
                1,
                1,
                1,
                0,
                [],
            )
        finally:
            if process.poll() is None:
                current = (
                    Path(f"/proc/{process.pid}/stat")
                    .read_text()
                    .rpartition(")")[2]
                    .split()
                )
                if (
                    int(current[19]) == int(birth[19])
                    and int(current[1]) == os.getpid()
                ):
                    process.kill()
                    process.wait(timeout=5)


def test_explicit_native_cancel_uses_one_observed_exit_and_one_terminal(tmp_path):
    from types import SimpleNamespace

    from omicsclaw.entry.desktop.jobs_manager import JobsManager, JobStore
    from omicsclaw.memory.database import Database
    from omicsclaw.remote.bridge import RemoteJobsBridge
    from omicsclaw.remote.local import LocalTargetSpawner
    from omicsclaw.remote.plane import RemotePlaneBinding

    async def scenario():
        authority = Database(tmp_path / "authority.db")
        plane = RemotePlaneBinding(
            authority, spawner=LocalTargetSpawner(tmp_path), workspace=tmp_path
        )
        bridge = RemoteJobsBridge(
            plane,
            target_alias="controller",
            target_root=str(tmp_path / "external"),
            poll_interval_s=0.3,
        )
        app = SimpleNamespace(
            config=SimpleNamespace(workspace=str(tmp_path), approval_timeout_s=None)
        )
        manager = JobsManager(
            app,
            store=JobStore(Database(tmp_path / "jobs.db")),
            remote_runtime=bridge,
            register_default_runners=False,
        )
        manager.register_runner("owned-command", bridge)
        record = await manager.create_job(
            kind="owned-command",
            source_request_id="native-cancel",
            inputs={"command": "sleep 5"},
        )
        deadline = time.monotonic() + 8
        while (
            row := plane.job_store().by_local_job(record.id)
        ) is None or row.handle().process_identity is None:
            if time.monotonic() > deadline:
                raise RuntimeError("owned_cancel_handle_timeout")
            await asyncio.sleep(0.01)
        proof = dict(row.handle().process_identity)
        acknowledged = await manager.cancel_job(record.id)
        while (
            manager.get_job(record.id).status != "canceled"
            and time.monotonic() < deadline
        ):
            await manager.reconcile_remote_jobs()
            await asyncio.sleep(0.02)
        await manager.shutdown_observations()
        await manager.reconcile_remote_jobs()
        events = manager.store.events_after(record.id, 0, limit=5000)
        verification = manager.run_store.load("verification:exit:" + record.id)
        observed = {
            "workspace": str(tmp_path),
            "businessJobId": record.id,
            "handleProcess": proof,
            "ack": acknowledged,
            "controllerPid": os.getpid(),
            "controllerUid": os.getuid(),
            "controllerStartTicks": int(
                Path(f"/proc/{os.getpid()}/stat")
                .read_text()
                .rpartition(")")[2]
                .split()[19]
            ),
            "status": manager.get_job(record.id).status,
            "exitCode": verification.evidence_ref,
            "terminalEvents": sum(
                kind in {"job.done", "job.failed"} for _, kind, _ in events
            ),
            "executionEvents": sum(
                kind == "execution.finished" for _, kind, _ in events
            ),
        }
        (tmp_path / "owned-native-cancel-proof.json").write_text(
            json.dumps(observed, sort_keys=True)
        )
        assert (
            observed["status"],
            observed["exitCode"],
            observed["terminalEvents"],
            observed["executionEvents"],
        ) == ("canceled", "143", 1, 1)

    asyncio.run(scenario())


def test_terminal_file_cannot_confirm_a_different_recorded_process_birth(tmp_path):
    from dataclasses import replace

    from omicsclaw.remote.local import LocalTargetSpawner

    async def scenario():
        executor = RemoteExecutor("controller", LocalTargetSpawner(tmp_path))
        handle = await executor.submit_in(str(tmp_path / "own-job"), "true")
        deadline = time.monotonic() + 5
        while (await executor.status(handle)).state != "done":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        bad = replace(
            handle,
            process_identity={
                **dict(handle.process_identity),
                "start_ticks": handle.process_identity["start_ticks"] + 1,
            },
        )
        observed = await executor.status(bad)
        assert (observed.state, observed.identity_verified) == ("unknown", False)
        assert (await executor.cancel(bad)).state == "unknown"

    asyncio.run(scenario())
