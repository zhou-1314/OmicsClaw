"""Real-SSH smoke tests, against this container's own ``localhost``.

Everything else in ``tests/remote`` runs over fakes; these few run the
actual ``ssh``/``sftp`` binaries end to end — probe, submit, watch the
job finish, fetch the result — against the only host a test may assume:
itself, over the loopback interface, with the key the container already
trusts. Skipped (not failed) wherever that is not true, so the suite
stays green on a laptop without sshd.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from omicsclaw.memory.database import Database
from omicsclaw.remote.plane import RemoteExecutor, RemotePlaneBinding
from omicsclaw.remote.ssh import (
    RemoteHostUnreachable,
    RemoteTransferError,
    SystemSshSpawner,
    build_ssh_argv,
)
from omicsclaw.tools.builtin.remote import ExecOutcome
from tests.remote._fakes import run

HOST = "localhost"


def _self_reachable() -> bool:
    ssh = shutil.which("ssh")
    if ssh is None:
        return False
    try:
        probe = subprocess.run(
            build_ssh_argv(HOST, "true"),
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _self_reachable(),
    reason="ssh localhost is not reachable from this environment",
)


@pytest.fixture(scope="module")
def plane(tmp_path_factory) -> RemotePlaneBinding:
    workspace = tmp_path_factory.mktemp("omicsclaw-remote-ws")
    return RemotePlaneBinding(
        Database(":memory:"),
        spawner=SystemSshSpawner(),
        workspace=workspace,
    )


def test_the_spawner_runs_a_real_command(plane):
    outcome = run(plane.exec(HOST, "printf omicsclaw-probe-ok", 30.0))
    assert isinstance(outcome, ExecOutcome)
    assert outcome.exit_code == 0
    assert "omicsclaw-probe-ok" in outcome.output


def test_the_probe_reads_this_machine(plane):
    card = run(plane.probe_card(HOST))
    assert card.parse_error == ""
    assert card.hostname  # localhost always has a name
    assert card.nproc and card.nproc >= 1
    assert card.scratch_root  # falls back to ~ when nothing else exists
    # the tool resolution the plan cares about, answered as present/absent
    assert set(card.commands) >= {"sbatch", "conda", "module", "uv", "sinfo"}


def test_a_real_nohup_job_runs_to_completion_and_fetches(plane):
    results = plane._workspace / "results"
    outcome = run(
        plane.submit(
            HOST,
            "printf 'hello-from-remote\\n' > out.txt && sleep 0.3",
            outputs=("out.txt",),
        )
    )
    assert outcome.kind in ("nohup", "slurm")
    deadline = time.time() + 60
    state = "running"
    while time.time() < deadline:
        report = run(plane.status(outcome.job_ref))
        state = report.state
        if state in ("done", "failed", "canceled", "unknown"):
            break
        time.sleep(0.5)
    assert state == "done", f"job ended as {state}"

    fetched = run(plane.fetch(outcome.job_ref, str(results), 100.0))
    assert fetched.downloaded
    downloaded = results / "out.txt"
    assert downloaded.read_text().strip() == "hello-from-remote"


def test_an_over_limit_fetch_stays_remote(plane):
    results = plane._workspace / "results"
    outcome = run(
        plane.submit(
            HOST,
            "head -c 4096 /dev/zero | tr '\\0' 'x' > big.txt",
            outputs=("big.txt",),
        )
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if run(plane.status(outcome.job_ref)).state == "done":
            break
        time.sleep(0.5)
    result = run(plane.fetch(outcome.job_ref, str(results), max_mb=0.000001))
    assert result.downloaded == ()
    assert result.remote_ref and result.remote_ref.startswith("remote://localhost/")
    assert not (results / "big.txt").exists()


def test_a_real_cancel_stops_the_job(plane):
    outcome = run(plane.submit(HOST, "sleep 60"))
    # wait for the job to actually be running before cancelling it
    deadline = time.time() + 30
    while time.time() < deadline:
        if run(plane.status(outcome.job_ref)).state == "running":
            break
        time.sleep(0.2)
    report = run(plane.cancel(outcome.job_ref))
    # Signal delivery is an acknowledgement; termination must be observed.
    deadline = time.monotonic() + 10
    while (
        report.state not in {"canceled", "done", "failed"}
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
        report = run(plane.status(outcome.job_ref))
    assert report.state == "canceled"


def test_declared_inputs_really_travel(plane, tmp_path):
    # The end-to-end half of the review's probe (a): the file is read
    # back through the remote command that consumed it.
    workspace = plane._workspace
    (workspace / "data").mkdir(exist_ok=True)
    (workspace / "data" / "greet.txt").write_text("hello-input\n")
    outcome = run(
        plane.submit(
            HOST,
            "cat greet.txt > echoed.txt",
            inputs=(("data/greet.txt", "greet.txt"),),
            outputs=("echoed.txt",),
        )
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if run(plane.status(outcome.job_ref)).state == "done":
            break
        time.sleep(0.5)
    fetched = run(plane.fetch(outcome.job_ref, str(workspace / "results"), 100.0))
    assert fetched.downloaded
    echoed = workspace / "results" / "echoed.txt"
    assert echoed.read_text() == "hello-input\n"


def test_a_fetch_dest_outside_the_workspace_is_refused(plane, tmp_path):
    from omicsclaw.remote.ssh import RemotePathRefused

    with pytest.raises(RemotePathRefused):
        run(plane.fetch("remote://" + HOST + "//root/whatever", str(tmp_path)))
    with pytest.raises(RemotePathRefused):
        run(plane.fetch("remote://" + HOST + "//root/whatever", "../outside"))


def test_duplicate_submission_observes_one_claim_and_one_real_execution(plane):
    workdir = str(plane._workspace / "canonical-claim-job")
    first = run(
        plane.submit(
            HOST,
            "printf 'once\\n' >> counter.txt",
            request_id="claim-once",
            target_workdir=workdir,
        )
    )
    second = run(
        plane.submit(
            HOST,
            "printf 'once\\n' >> counter.txt",
            request_id="claim-once",
            target_workdir=workdir,
        )
    )
    deadline = time.monotonic() + 10
    while (
        run(plane.status(first.job_ref)).state != "done" and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert (
        first.job_ref,
        second.job_ref,
        Path(workdir, "counter.txt").read_text(),
    ) == (
        first.job_ref,
        first.job_ref,
        "once\n",
    )


def test_ack_lost_after_real_launch_recovers_original_handle_without_relaunch(tmp_path):
    class LoseLaunchAck(SystemSshSpawner):
        lost = False

        async def spawn(self, argv, *, timeout, stdin_text=None):
            result = await super().spawn(argv, timeout=timeout, stdin_text=stdin_text)
            if not self.lost and "_omicsclaw_launch_claim" in argv[-1]:
                self.lost = True
                raise RemoteHostUnreachable("owned_fixture_ack_lost")
            return result

    database = Database(tmp_path / "jobs.db")
    spawner = LoseLaunchAck()
    instance = RemotePlaneBinding(database, spawner=spawner, workspace=tmp_path)
    workdir = str(tmp_path / "ack-lost-job")
    intent = {"request_id": "ack-lost", "target_workdir": workdir}
    try:
        with pytest.raises(RemoteHostUnreachable, match="owned_fixture_ack_lost"):
            run(instance.submit(HOST, "printf 'once\\n' >> counter.txt", **intent))
        original = instance.job_store().by_request("ack-lost")
        observed = run(
            instance.submit(HOST, "printf 'once\\n' >> counter.txt", **intent)
        )
        deadline = time.monotonic() + 10
        while (
            run(instance.status(observed.job_ref)).state != "done"
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert (
            observed.job_ref,
            observed.pgid is not None,
            Path(workdir, "counter.txt").read_text(),
        ) == (
            str(original.id),
            True,
            "once\n",
        )
    finally:
        database.close()


def test_changed_staged_input_is_refused_before_real_launch(tmp_path):
    workdir = tmp_path / "drift-job"

    class ChangeUploadedInput(SystemSshSpawner):
        async def spawn(self, argv, *, timeout, stdin_text=None):
            result = await super().spawn(argv, timeout=timeout, stdin_text=stdin_text)
            if argv[0] == "sftp" and stdin_text and "input.txt" in stdin_text:
                (workdir / "input.txt").write_text("other")
            return result

    (tmp_path / "input.txt").write_text("first")
    database = Database(":memory:")
    instance = RemotePlaneBinding(
        database, spawner=ChangeUploadedInput(), workspace=tmp_path
    )
    try:
        with pytest.raises(RemoteTransferError, match="input_integrity_unconfirmed"):
            run(
                instance.submit(
                    HOST,
                    "printf done > counter.txt",
                    request_id="input-drift",
                    target_workdir=str(workdir),
                    inputs=(("input.txt", "input.txt"),),
                )
            )
        assert not (workdir / "counter.txt").exists()
    finally:
        database.close()


def test_finished_job_from_a_different_boot_is_unknown_even_with_exit_file(plane):
    outcome = run(
        plane.submit(
            HOST,
            "true",
            request_id="terminal-boot",
            target_workdir=str(plane._workspace / "terminal-boot-job"),
        )
    )
    deadline = time.monotonic() + 10
    while (
        run(plane.status(outcome.job_ref)).state != "done"
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    handle = plane.job_store().get(outcome.job_ref).handle()
    proof = {**handle.process_identity, "boot_id": "0" * 32}
    report = run(
        RemoteExecutor(HOST, SystemSshSpawner()).status(
            replace(handle, process_identity=proof)
        )
    )
    assert report.state == "unknown"
