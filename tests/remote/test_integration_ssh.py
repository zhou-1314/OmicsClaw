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

import pytest

from omicsclaw.memory.database import Database
from omicsclaw.remote.plane import RemotePlaneBinding
from omicsclaw.remote.ssh import SystemSshSpawner, build_ssh_argv
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
    outcome = run(plane.submit(
        HOST,
        "printf 'hello-from-remote\\n' > out.txt && sleep 0.3",
        outputs=("out.txt",),
    ))
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
    outcome = run(plane.submit(
        HOST,
        "head -c 4096 /dev/zero | tr '\\0' 'x' > big.txt",
        outputs=("big.txt",),
    ))
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
    assert report.state == "canceled"


def test_declared_inputs_really_travel(plane, tmp_path):
    # The end-to-end half of the review's probe (a): the file is read
    # back through the remote command that consumed it.
    workspace = plane._workspace
    (workspace / "data").mkdir(exist_ok=True)
    (workspace / "data" / "greet.txt").write_text("hello-input\n")
    outcome = run(plane.submit(
        HOST,
        "cat greet.txt > echoed.txt",
        inputs=(("data/greet.txt", "greet.txt"),),
        outputs=("echoed.txt",),
    ))
    deadline = time.time() + 60
    while time.time() < deadline:
        if run(plane.status(outcome.job_ref)).state == "done":
            break
        time.sleep(0.5)
    fetched = run(plane.fetch(
        outcome.job_ref, str(workspace / "results"), 100.0
    ))
    assert fetched.downloaded
    echoed = workspace / "results" / "echoed.txt"
    assert echoed.read_text() == "hello-input\n"


def test_a_fetch_dest_outside_the_workspace_is_refused(plane, tmp_path):
    from omicsclaw.remote.ssh import RemotePathRefused

    with pytest.raises(RemotePathRefused):
        run(plane.fetch("remote://" + HOST + "//root/whatever", str(tmp_path)))
    with pytest.raises(RemotePathRefused):
        run(plane.fetch("remote://" + HOST + "//root/whatever", "../outside"))
