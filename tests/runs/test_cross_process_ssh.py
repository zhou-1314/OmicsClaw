"""A real SSH job survives its submitting Python process and an outage."""

import json
import subprocess
import sys

import pytest

from tests.remote.test_integration_ssh import _self_reachable

CHILD = r"""
import asyncio, json, sys, time
from pathlib import Path
from types import SimpleNamespace
from omicsclaw.entry.desktop.jobs_manager import JobRecord, JobsManager
from omicsclaw.memory.database import Database
from omicsclaw.remote.plane import RemotePlaneBinding
from omicsclaw.remote.bridge import RemoteJobsBridge
from omicsclaw.remote.ssh import SystemSshSpawner, RemoteHostUnreachable
from omicsclaw.runs.models import Run, Job
workspace = Path(sys.argv[1])
plane = RemotePlaneBinding(Database(workspace / 'remote.db'), spawner=SystemSshSpawner(), workspace=workspace)
manager = JobsManager(SimpleNamespace(config=SimpleNamespace(workspace=workspace)), remote_runtime=RemoteJobsBridge(plane))
async def main():
    if sys.argv[2] == 'submit':
        run = Run(run_id='run-survives', status='running', execution_location='remote:localhost')
        manager.run_store.save(run)
        manager.run_store.save(Job(job_id='job-survives', run_id=run.run_id, execution_ref='desktop', status='running'))
        manager.store.insert_job(JobRecord(id='desktop', runtime='remote:localhost', status='running', inputs={'outputs':['out.txt']}))
        outcome = await plane.submit('localhost', "while [ ! -f release ]; do sleep 0.1; done; printf 'complete' > out.txt", local_job_id='desktop', target_workdir=str(workspace/'host-job'), outputs=('out.txt',), scheduler='none')
        print(json.dumps({'ref':outcome.job_ref}))
        return
    if sys.argv[2] == 'cancel':
        print(json.dumps({'cancel': await manager.cancel_job('desktop'), 'run': manager.run_store.load('run-survives').status}))
        return
    if sys.argv[2] == 'outage':
        class Offline:
            async def status(self, handle):
                raise OSError('injected connection outage')
        plane.executor_for = lambda alias: Offline()
    report = await manager.reconcile_remote_jobs()
    print(json.dumps({'report':report,'run':manager.run_store.load('run-survives').status,'job':manager.run_store.load('job-survives').status,'desktop':manager.get_job('desktop').status, 'events':len(manager.run_store.events()), 'artifacts':len(manager.run_store.list('artifact')), 'exit':[(v.check,v.verdict) for v in manager.run_store.list('verification') if v.check=='exit_code']}))
asyncio.run(main())
"""


@pytest.mark.skipif(not _self_reachable(), reason="ssh localhost unavailable")
def test_new_process_recovers_real_ssh_handle_after_outage(tmp_path):
    def child(action):
        result = subprocess.run(
            [sys.executable, "-c", CHILD, str(tmp_path), action],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    child("submit")
    try:
        assert child("outage")["run"] == "unknown"
        recovered = child("recover")
        assert recovered["run"] == recovered["job"] == recovered["desktop"] == "running"
    finally:
        (tmp_path / "host-job" / "release").touch()
    import time

    deadline = time.monotonic() + 15
    while True:
        finished = child("recover")
        if finished["run"] == "succeeded":
            break
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert finished["job"] == finished["desktop"] == "succeeded"
    assert finished["artifacts"] == 1
    assert finished["exit"] == [["exit_code", "pass"]]
    assert child("recover")["events"] == finished["events"]
    assert (tmp_path / "host-job" / "out.txt").read_text() == "complete"
    import sqlite3

    with sqlite3.connect(tmp_path / "remote.db") as database:
        assert database.execute("SELECT count(*) FROM remote_jobs").fetchone()[0] == 1


@pytest.mark.skipif(not _self_reachable(), reason="ssh localhost unavailable")
def test_new_process_cancels_real_ssh_handle(tmp_path):
    def child(action):
        result = subprocess.run(
            [sys.executable, "-c", CHILD, str(tmp_path), action],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    child("submit")
    try:
        acknowledgement = child("cancel")
        assert acknowledgement["cancel"] in {"cancel_requested", "canceled"}
        import time

        deadline = time.monotonic() + 15
        while True:
            observed = child("recover")
            if observed["run"] == "cancelled":
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert observed["desktop"] == "canceled"
        assert observed["exit"] == [["exit_code", "fail"]]
    finally:
        (tmp_path / "host-job" / "release").touch()
