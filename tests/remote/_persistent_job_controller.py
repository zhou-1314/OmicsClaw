"""Owned finite process hosting the real job manager for crash probes."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from omicsclaw.entry.desktop.jobs_manager import JobsManager, JobStore
from omicsclaw.entry.ssh.controller import WorkspaceOwner
from omicsclaw.memory.database import Database
from omicsclaw.remote.bridge import RemoteJobsBridge
from omicsclaw.remote.local import LocalTargetSpawner
from omicsclaw.remote.plane import RemotePlaneBinding


async def main():
    root, action, nonce = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    owner = WorkspaceOwner(root, nonce)
    app = SimpleNamespace(
        config=SimpleNamespace(workspace=str(root), approval_timeout_s=None)
    )
    authority = Database(root / "authority.db")
    plane = RemotePlaneBinding(
        authority, spawner=LocalTargetSpawner(root), workspace=root
    )
    bridge = RemoteJobsBridge(
        plane,
        poll_interval_s=0.03,
        target_alias="controller",
        target_root=str(root / "external-jobs"),
    )
    manager = JobsManager(
        app,
        store=JobStore(Database(root / "jobs.db")),
        remote_runtime=bridge,
        register_default_runners=False,
    )
    manager.register_runner("owned-command", bridge)
    command = (
        "printf 'once\\n' >> counter.txt; sleep 3; printf 'finished\\n' > finished.txt"
    )
    record = await manager.create_job(
        kind="owned-command",
        inputs={"command": command, "outputs": ["finished.txt"]},
        source_request_id="same-business-job",
        runtime="local",
    )
    if action in {"launch", "eof"}:
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            row = plane.job_store().by_local_job(record.id)
            if row is not None and row.handle().process_identity is not None:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("owned_controller_handle_timeout")
            await asyncio.sleep(0.01)
        fields = (
            Path(f"/proc/{os.getpid()}/stat").read_text().rpartition(")")[2].split()
        )
        print(
            json.dumps(
                {
                    "controllerPid": os.getpid(),
                    "controllerUid": os.getuid(),
                    "controllerStartTicks": int(fields[19]),
                    "controllerNonce": nonce,
                    "workspace": str(root),
                    "businessJobId": record.id,
                    "runtime": record.runtime,
                    "lifetime": record.lifetime,
                    "remoteRowId": row.id,
                    "handleProcess": dict(row.handle().process_identity),
                }
            ),
            flush=True,
        )
        if action == "eof":
            await asyncio.to_thread(sys.stdin.read)
            await manager.shutdown_observations()
            owner.close()
        else:
            await asyncio.Event().wait()
    else:
        before = record.status
        deadline = asyncio.get_running_loop().time() + 10
        while manager.get_job(record.id).status not in {
            "succeeded",
            "failed",
            "canceled",
        }:
            await manager.reconcile_remote_jobs()
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("owned_controller_recovery_timeout")
            await asyncio.sleep(0.03)
        # Repeating reconciliation must not create another terminal or artifact.
        await manager.reconcile_remote_jobs()
        row = plane.job_store().by_local_job(record.id)
        events = manager.store.events_after(record.id, 0, limit=5000)
        print(
            json.dumps(
                {
                    "businessJobId": record.id,
                    "remoteRowId": row.id,
                    "beforeStatus": before,
                    "status": manager.get_job(record.id).status,
                    "runtime": record.runtime,
                    "lifetime": record.lifetime,
                    "handleProcess": dict(row.handle().process_identity),
                    "workdir": row.workdir,
                    "counter": (Path(row.workdir) / "counter.txt").read_text(),
                    "jobs": len(manager.list_jobs()),
                    "submissions": authority.run(
                        lambda conn: conn.execute(
                            "SELECT COUNT(*) FROM remote_jobs"
                        ).fetchone()[0]
                    ),
                    "terminalEvents": sum(
                        kind in {"job.done", "job.failed"} for _, kind, _ in events
                    ),
                    "executionEvents": sum(
                        kind == "execution.finished" for _, kind, _ in events
                    ),
                    "artifacts": len(manager.run_store.list("artifact")),
                    "observers": len(manager._live),
                    "scienceImports": sorted(
                        name
                        for name in sys.modules
                        if name.split(".")[0]
                        in {"numpy", "scipy", "pandas", "scanpy", "anndata", "rpy2"}
                    ),
                }
            ),
            flush=True,
        )
        await manager.shutdown_observations()
        owner.close()


asyncio.run(main())
