"""Job scripts, submission commands and the status state machine.

Everything pure in ``omicsclaw/remote/jobs.py``: what a work directory
holds, how ``#SBATCH`` promotes a command, what the submit command
prints and how it is read back, and how squeue/sacct/kill -0 answers
map onto the five states.
"""

from __future__ import annotations

import pytest

from omicsclaw.remote.jobs import (
    KIND_NOHUP,
    KIND_SLURM,
    RemoteJobHandle,
    build_cancel_command,
    build_job_scripts,
    build_mkdir_command,
    build_size_command,
    build_status_command,
    build_submit_command,
    detect_scheduler,
    parse_status,
    parse_submit_output,
)


class TestSchedulerDetection:
    def test_plain_command_is_nohup(self):
        assert detect_scheduler("echo hi") == KIND_NOHUP

    def test_sbatch_directive_promotes(self):
        assert detect_scheduler("#SBATCH --gres=gpu:1\npython t.py") == KIND_SLURM

    def test_indented_directive_promotes(self):
        assert detect_scheduler("  #SBATCH -N1\ntrue") == KIND_SLURM

    def test_a_mention_in_a_string_does_not_promote(self):
        assert detect_scheduler("echo '#SBATCH' > notes.txt") == KIND_NOHUP

    def test_explicit_overrides_win_both_ways(self):
        assert detect_scheduler("echo hi", "slurm") == KIND_SLURM
        assert detect_scheduler("#SBATCH -N1", "none") == KIND_NOHUP

    def test_unknown_choice_is_refused(self):
        with pytest.raises(ValueError):
            detect_scheduler("x", "pbs")


class TestJobScripts:
    def test_nohup_job_is_three_files(self):
        files = build_job_scripts("echo hi", KIND_NOHUP)
        assert set(files) == {"cmd.sh", "job.sh", "_omicsclaw_launch.sh"}
        assert "echo hi" in files["cmd.sh"]
        assert files["cmd.sh"].startswith("#!")

    def test_wrapper_records_the_exit_code_on_every_path(self):
        # No set -e and no trap: the two lines after the command must
        # run whether it succeeded, failed or was signalled.
        wrapper = build_job_scripts("x", KIND_NOHUP)["job.sh"]
        assert "set -e" not in wrapper
        assert "_omicsclaw_exit_code" in wrapper
        assert "bash ./cmd.sh" in wrapper

    def test_launcher_records_its_pgid_before_exec(self):
        launcher = build_job_scripts("x", KIND_NOHUP)["_omicsclaw_launch.sh"]
        assert '"$$" > ./_omicsclaw_pgid' in launcher
        assert launcher.rstrip().endswith("2>&1")

    def test_slurm_command_is_job_sh_verbatim(self):
        script = "#!/bin/bash\n#SBATCH -N1\necho hi\n"
        files = build_job_scripts(script, KIND_SLURM)
        assert list(files) == ["job.sh"]
        assert files["job.sh"] == script

    def test_slurm_gets_a_shebang_when_the_command_had_none(self):
        files = build_job_scripts("#SBATCH -N1\necho hi", KIND_SLURM)
        assert files["job.sh"].startswith("#!/usr/bin/env bash\n")


class TestCommands:
    def test_mkdir_is_700_and_refuses_odd_arguments(self):
        command = build_mkdir_command("/scratch/my dir")
        assert "mkdir -p -m 700" in command
        assert "'/scratch/my dir'" in command
        assert "--" in command

    def test_slurm_submit_is_parsable(self):
        command = build_submit_command("/w", KIND_SLURM)
        assert "sbatch --parsable ./job.sh" in command
        assert "cd -- '/w'" in command

    def test_nohup_submit_setsid_and_waits_for_the_pgid(self):
        command = build_submit_command("/w", KIND_NOHUP)
        assert "setsid nohup bash ./_omicsclaw_launch.sh" in command
        assert "_omicsclaw_pgid" in command
        assert "sleep 0.1" in command
        assert command.rstrip().endswith("echo 0")

    def test_nohup_status_probes_the_group_then_the_exit_file(self):
        handle = RemoteJobHandle(kind=KIND_NOHUP, host="h", workdir="/w", pgid=4242)
        command = build_status_command(handle)
        assert "kill -0 -- -4242" in command
        assert "_omicsclaw_exit_code" in command
        assert "__OMICSCLAW_LOST__" in command

    def test_slurm_status_queues_then_accounts(self):
        handle = RemoteJobHandle(kind=KIND_SLURM, host="h", workdir="/w", job_id="99")
        command = build_status_command(handle)
        assert "squeue -h -j 99 -o %T" in command
        assert "sacct -n -X -j 99 -o State" in command

    def test_cancel_matches_the_kind(self):
        slurm = RemoteJobHandle(kind=KIND_SLURM, host="h", workdir="/w", job_id="7")
        nohup = RemoteJobHandle(kind=KIND_NOHUP, host="h", workdir="/w", pgid=77)
        assert build_cancel_command(slurm) == "scancel 7"
        assert build_cancel_command(nohup) == "kill -TERM -- -77"

    def test_size_command_falls_back_to_wc(self):
        command = build_size_command(["/w/a", "/w/b"])
        assert command.count("stat -c %s --") == 2
        assert command.count("wc -c <") == 2


class TestSubmitOutput:
    def test_slurm_job_id_with_cluster_suffix(self):
        assert parse_submit_output(KIND_SLURM, "12345;cluster-a\n") == 12345

    def test_slurm_bare_job_id(self):
        assert parse_submit_output(KIND_SLURM, "12345\n") == 12345

    def test_slurm_noise_is_refused(self):
        with pytest.raises(ValueError):
            parse_submit_output(KIND_SLURM, "Submitted batch job maybe\n")

    def test_nohup_takes_the_last_integer(self):
        assert parse_submit_output(KIND_NOHUP, "tick\n4242") == 4242

    def test_nohup_zero_is_still_a_number(self):
        assert parse_submit_output(KIND_NOHUP, "0") == 0


class TestStatusParsing:
    @pytest.mark.parametrize(
        ("output", "state"),
        [
            ("RUNNING", "running"),
            ("PENDING", "pending"),
            ("COMPLETING", "running"),
            ("SUSPENDED", "running"),
            ("COMPLETED", "done"),
            ("FAILED", "failed"),
            ("TIMEOUT", "failed"),
            ("OUT_OF_MEMORY", "failed"),
            ("NODE_FAIL", "failed"),
            ("CANCELLED by 1000", "canceled"),
            ("CANCELLED+", "canceled"),
            ("SOMETHING_NEW", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_slurm_states(self, output, state):
        assert parse_status(KIND_SLURM, output) == (state, None)

    def test_slurm_gone_from_everything_reads_done(self):
        assert parse_status(KIND_SLURM, "__OMICSCLAW_LOST__") == ("done", None)

    def test_nohup_running(self):
        assert parse_status(KIND_NOHUP, "__OMICSCLAW_RUNNING__") == ("running", None)

    def test_nohup_exit_zero_is_done(self):
        assert parse_status(KIND_NOHUP, "0") == ("done", 0)

    def test_nohup_nonzero_is_failed_with_the_code(self):
        assert parse_status(KIND_NOHUP, "137") == ("failed", 137)

    def test_nohup_lost_group_is_unknown(self):
        assert parse_status(KIND_NOHUP, "__OMICSCLAW_LOST__") == ("unknown", None)

    def test_nohup_garbage_is_unknown(self):
        assert parse_status(KIND_NOHUP, "what") == ("unknown", None)


class TestHandle:
    def test_json_round_trip_keeps_every_member(self):
        handle = RemoteJobHandle(
            kind=KIND_SLURM,
            host="hpc1",
            workdir="/scratch/omicsclaw/x",
            job_id="4242",
            submitted_at=1759000000.5,
            local_job_id="abc",
        )
        again = RemoteJobHandle.from_json(handle.to_json())
        assert again == handle

    def test_identity_is_the_host_side_number(self):
        slurm = RemoteJobHandle(kind=KIND_SLURM, host="h", workdir="/w", job_id="9")
        nohup = RemoteJobHandle(kind=KIND_NOHUP, host="h", workdir="/w", pgid=31)
        assert slurm.identity() == "9"
        assert nohup.identity() == "31"
