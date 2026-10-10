"""The plane over a fake transport: submit, track, cancel, fetch.

``RemotePlaneBinding`` stitched over ``FakeSpawner`` — the state machine
as the tools and the bridge will drive it: probe once and remember,
create the work directory, upload the scripts, read the number back,
persist the handle, and re-estimate on demand.
"""

from __future__ import annotations

import pytest

from omicsclaw.memory.database import Database
from omicsclaw.remote.plane import OUTPUTS_FILE, RemotePlaneBinding
from omicsclaw.remote.ssh import RemoteHostUnreachable

from tests.remote._fakes import PROBE_OK, FakeSpawner, run


@pytest.fixture()
def spawner() -> FakeSpawner:
    fake = FakeSpawner()
    fake.on("base64 -d", PROBE_OK)
    fake.on("mkdir -p", "")
    fake.on("setsid nohup", "4242")
    fake.on("sbatch --parsable", "5150;cluster-a")
    fake.on("kill -0", "__OMICSCLAW_RUNNING__")
    fake.on("squeue", "RUNNING")
    fake.on("stat -c %s", "12345")
    fake.on("_omicsclaw_outputs", "out.txt\n")
    return fake


@pytest.fixture()
def plane(spawner: FakeSpawner) -> RemotePlaneBinding:
    return RemotePlaneBinding(Database(":memory:"), spawner=spawner)


def test_submit_probes_then_lands_under_the_scratch(plane, spawner):
    outcome = run(plane.submit("hpc1", "echo hi", outputs=("out.txt",)))
    assert outcome.kind == "nohup"
    assert outcome.pgid == 4242
    assert outcome.host == "hpc1"
    assert outcome.workdir.startswith("/scratch/fake/omicsclaw/")
    commands = spawner.ssh_commands()
    assert any("base64 -d" in c for c in commands), commands
    assert any("mkdir -p -m 700" in c for c in commands)
    assert any("setsid nohup" in c for c in commands)


def test_submit_uploads_scripts_and_declared_outputs(plane, spawner):
    run(plane.submit("hpc1", "echo hi", outputs=("out.txt",)))
    batches = spawner.sftp_batches()
    assert len(batches) == 1
    batch = batches[0]
    assert "cmd.sh" in batch
    assert "job.sh" in batch
    assert "_omicsclaw_launch.sh" in batch
    assert OUTPUTS_FILE in batch


def test_a_stored_probe_is_reused_on_the_second_submit(plane, spawner):
    run(plane.submit("hpc1", "echo one"))
    probes_after_first = [c for c in spawner.ssh_commands() if "base64 -d" in c]
    run(plane.submit("hpc1", "echo two"))
    probes_after_second = [c for c in spawner.ssh_commands() if "base64 -d" in c]
    assert len(probes_after_first) == 1
    assert len(probes_after_second) == 1  # no second probe round-trip


def test_sbatch_directives_submit_through_slurm(plane, spawner):
    outcome = run(plane.submit("hpc1", "#SBATCH -N1\necho hi"))
    assert outcome.kind == "slurm"
    assert outcome.job_id == "5150"
    assert any("sbatch --parsable" in c for c in spawner.ssh_commands())


def test_status_walks_the_state_machine_and_persists(plane, spawner):
    outcome = run(plane.submit("hpc1", "echo hi"))
    report = run(plane.status(outcome.job_ref))
    assert report.state == "running"
    spawner.on("kill -0", "0")
    report = run(plane.status(outcome.job_ref))
    assert report.state == "done"
    assert report.exit_code == 0
    assert plane.job_store().in_flight() == []  # done is terminal; row remains


def test_failed_exit_code_flows_through(plane, spawner):
    outcome = run(plane.submit("hpc1", "echo hi"))
    spawner.on("kill -0", "137")
    report = run(plane.status(outcome.job_ref))
    assert report.state == "failed"
    assert report.exit_code == 137


def test_cancel_asks_the_host(plane, spawner):
    outcome = run(plane.submit("hpc1", "sleep 100"))
    report = run(plane.cancel(outcome.job_ref))
    assert report.state == "canceled"
    assert any("kill -TERM -- -4242" in c for c in spawner.ssh_commands())


def test_fetch_by_job_ref_brings_declared_outputs(plane, spawner, tmp_path):
    outcome = run(plane.submit("hpc1", "echo hi", outputs=("out.txt",)))
    result = run(plane.fetch(outcome.job_ref, str(tmp_path), 100.0))
    assert result.downloaded == (str(tmp_path / "out.txt"),)
    get_lines = [
        line
        for line in spawner.sftp_batches()[-1].splitlines()
        if line.startswith("get")
    ]
    assert len(get_lines) == 1
    assert get_lines[0].endswith(f"out.txt\" \"{tmp_path / 'out.txt'}\"")


def test_fetch_over_the_threshold_stays_remote(plane, spawner, tmp_path):
    outcome = run(plane.submit("hpc1", "echo hi", outputs=("out.txt",)))
    result = run(plane.fetch(outcome.job_ref, str(tmp_path), max_mb=0.000001))
    assert result.downloaded == ()
    assert result.remote_ref is not None
    assert result.remote_ref.startswith("remote://hpc1/")
    assert result.remote_ref.endswith("out.txt")
    assert "exceeds" in result.skipped_reason
    # nothing travelled: the only sftp batch is the upload
    assert len(spawner.sftp_batches()) == 1


def test_fetch_by_remote_url_names_host_and_path(plane, spawner, tmp_path):
    result = run(
        plane.fetch("remote://hpc1//scratch/fake/big.h5ad", str(tmp_path), 100.0)
    )
    assert result.downloaded == (str(tmp_path / "big.h5ad"),)


def test_unknown_status_when_the_host_is_unreachable_keeps_the_row(plane, spawner):
    outcome = run(plane.submit("hpc1", "echo hi"))
    spawner.on("kill -0", RemoteHostUnreachable)
    report = run(plane.status(outcome.job_ref))
    assert report.state == "unknown"
    row = plane.job_store().get(outcome.job_ref)
    assert row is not None
    assert row.status == "unknown"


def test_exec_runs_the_command_verbatim(plane, spawner):
    outcome = run(plane.exec("hpc1", "squeue -u me", 30.0))
    assert outcome.exit_code == 0
    assert "squeue -u me" in spawner.ssh_commands()


def test_probe_card_stores_and_answers(plane, spawner):
    card = run(plane.probe_card("hpc1"))
    assert card.hostname == "fake-n01"
    assert card.nproc == 16
    assert card.scratch_root == "/scratch/fake"
    assert card.memory_mb == 65536
    stored = plane.host_card("hpc1")
    assert stored is not None and stored.hostname == "fake-n01"


def test_note_answer_lands_in_the_knowledge_base(plane):
    plane.note_answer("hpc1", "which partition?", "gpu-long")
    card = plane.host_card("hpc1")
    assert card is not None
    assert card.notes[0]["answer"] == "gpu-long"
