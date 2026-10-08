"""``python -m omicsclaw.bench``: the commands, their files and exit codes."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omicsclaw.bench.__main__ import _env_file, _stop_signals, main
from omicsclaw.bench.layout import read_jsonl
from omicsclaw.bench.run import run_campaign

from ._support import OK_EVIDENCE, Toy, alive, ok, wait_for

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = {"exit": 1, "evidence": {"infra_reason": "provider_error: ProviderError"}}


def arguments(toy: Toy) -> list[str]:
    return [str(toy.manifest_path), "--cases", str(toy.cases), "--out", str(toy.out)]


def test_the_command_runs_stage_run_and_grade_as_a_process(tmp_path):
    """The literal command, in a subprocess, over the toy suite: it leaves
    the three result files with one row per run.
    """
    toy = Toy(tmp_path, arms=("a", "b"), cases=("sum-a", "sum-b"))

    def command(*words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "omicsclaw.bench", *words],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )

    plan = command("plan", str(toy.manifest_path))
    staged = command("stage", *arguments(toy))
    ran = command("run", *arguments(toy), "--jobs", "2")
    graded = command("grade", *arguments(toy))

    assert plan.returncode == 0 and len(plan.stdout.splitlines()) == 4
    assert staged.returncode == 0 and json.loads(staged.stdout) == {"staged": 4}
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)["outcomes"] == {"completed": 4}
    assert graded.returncode == 0, graded.stderr
    assert json.loads(graded.stdout)["graded"] == 4
    for name in ("predictions.jsonl", "usage.jsonl", "grades.jsonl"):
        assert len(read_jsonl(toy.out / name)) == 4


def test_run_reports_infrastructure_failures_in_its_exit_status(tmp_path, capsys):
    toy = Toy(tmp_path)
    toy.play({"default": [INFRA, ok("sum-a")]})

    assert main(["run", *arguments(toy)]) == 1
    assert main(["grade", *arguments(toy)]) == 1
    assert main(["run", *arguments(toy)]) == 1
    assert main(["run", *arguments(toy), "--retry-infra"]) == 0
    assert main(["grade", *arguments(toy)]) == 0
    capsys.readouterr()


@pytest.mark.parametrize(("name", "status"), [("SIGTERM", 143), ("SIGHUP", 129)])
def test_a_signal_to_the_harness_stops_its_agent(tmp_path, name, status):
    """A harness stopped by its supervisor, or by its terminal going away,
    takes the agent in progress with it and leaves the run unfinished,
    the way Ctrl-C does. Left alone, the agent would run on, spending
    model calls into a workspace nobody is recording.
    """
    toy = Toy(tmp_path)
    pid_file = tmp_path / "agent.pid"
    toy.play({"default": [
        {"pid": str(pid_file), "sleep": 60, "evidence": OK_EVIDENCE}
    ]})
    harness = subprocess.Popen(
        [sys.executable, "-m", "omicsclaw.bench", "run", *arguments(toy)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    agent = 0
    try:
        agent = int(wait_for(pid_file))
        harness.send_signal(getattr(signal, name))
        output, _ = harness.communicate(timeout=20)
        gone = not alive(agent)
    finally:
        if harness.poll() is None:
            harness.kill()
        if agent and alive(agent):
            os.kill(agent, signal.SIGKILL)

    assert harness.returncode == status
    assert gone
    assert json.loads(output)["interrupted"] is True
    assert not toy.paths("a/m/sum-a/r1").done.exists()


def test_only_the_first_stop_signal_interrupts(tmp_path):
    """A supervisor that sends SIGTERM twice, or a terminal that hangs up
    during the stop, must not cut the stop short: the first signal starts
    it and later ones are absorbed. The handlers in place before are put
    back afterwards.
    """
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))

    with _stop_signals() as received:
        if signal.getsignal(signal.SIGTERM) is before[0]:
            pytest.skip("signal handlers can only be installed in the main thread")
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)  # the handler interrupts this
        try:
            os.kill(os.getpid(), signal.SIGHUP)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
        except KeyboardInterrupt:
            pytest.fail("a later stop signal interrupted the stop")

    assert received == [signal.SIGTERM]
    assert (
        signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)
    ) == before


def test_a_second_run_on_the_same_output_root_is_refused(tmp_path):
    """Two invocations sharing an output root would set each other's runs
    aside as interrupted and start them twice. The second one is turned
    away at once and the first finishes undisturbed.
    """
    toy = Toy(tmp_path)
    pid_file, log = tmp_path / "agent.pid", tmp_path / "starts.log"
    toy.play({"default": [
        {"pid": str(pid_file), "log": str(log), "sleep": 3,
         "write": "right", "evidence": OK_EVIDENCE}
    ]})
    command = [sys.executable, "-m", "omicsclaw.bench", "run", *arguments(toy)]
    first = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT),
    )
    try:
        wait_for(pid_file)
        second = subprocess.run(
            command, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30
        )
        output, _ = first.communicate(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()

    assert second.returncode == 2
    assert "is in use by another" in second.stderr
    assert first.returncode == 0
    assert json.loads(output)["outcomes"] == {"completed": 1}
    assert log.read_text().count("start ") == 1
    attempts = toy.paths("a/m/sum-a/r1").meta.parent
    assert sorted(path.name for path in attempts.iterdir()) == ["r1"]


def test_results_and_cases_inside_the_repository_are_refused(tmp_path, capsys):
    """Case data and results belong outside the checkout. A ``.git`` above
    the manifest marks the checkout.
    """
    (tmp_path / ".git").mkdir()
    toy = Toy(tmp_path)

    status = main(["run", *arguments(toy)])

    assert status == 2
    assert "--cases" in capsys.readouterr().err
    assert not toy.out.exists()


def test_an_output_root_inside_the_repository_is_refused_by_name(tmp_path, capsys):
    """The cases are outside the checkout and only the results would land
    in it. That is refused too, and the message names ``--out``.
    """
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    toy = Toy(repository)
    outside = tmp_path / "cases"
    shutil.copytree(toy.cases, outside)

    status = main([
        "run", str(toy.manifest_path), "--cases", str(outside), "--out", str(toy.out),
    ])

    error = capsys.readouterr().err
    assert status == 2
    assert "--out" in error and "inside the repository" in error
    assert not toy.out.exists()


def test_an_output_root_inside_the_cases_root_is_refused(tmp_path, capsys):
    toy = Toy(tmp_path)
    inside = toy.cases / "runs"

    status = main(
        ["run", str(toy.manifest_path), "--cases", str(toy.cases), "--out", str(inside)]
    )

    assert status == 2
    assert "overlap" in capsys.readouterr().err
    assert not inside.exists()


def test_grading_before_anything_ran_says_so(tmp_path, capsys):
    toy = Toy(tmp_path)

    status = main(["grade", *arguments(toy)])

    assert status == 2
    assert "nothing has been run" in capsys.readouterr().err
    assert not toy.out.exists()


def test_a_case_that_cannot_be_staged_is_a_message_not_a_traceback(tmp_path):
    toy = Toy(tmp_path)
    os.symlink(tmp_path / "nowhere", toy.cases / "sum-a" / "public" / "dangling")

    done = subprocess.run(
        [sys.executable, "-m", "omicsclaw.bench", "stage", *arguments(toy)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )

    assert done.returncode == 2
    assert "dangling" in done.stderr and "Traceback" not in done.stderr


def test_a_bad_manifest_is_exit_status_two(tmp_path, capsys):
    toy = Toy(tmp_path)
    toy.manifest_path.write_text("name = ")

    assert main(["run", *arguments(toy)]) == 2
    assert "not valid TOML" in capsys.readouterr().err


def test_the_env_file_fills_in_only_what_is_not_set(tmp_path, monkeypatch, capsys):
    """Like the ``.env`` the agent reads for itself: a variable the shell
    already exported keeps its value.
    """
    toy = Toy(tmp_path)
    env_file = tmp_path / "bench.env"
    env_file.write_text(
        "# a comment\nexport BENCH_FROM_FILE='from file'\nBENCH_ALREADY_SET=file\n"
    )
    monkeypatch.setenv("BENCH_ALREADY_SET", "shell")
    seen: dict[str, str] = {}

    def spy(manifest, campaign, **options):
        seen.update(options["base_env"])
        return run_campaign(manifest, campaign, **options)

    monkeypatch.setattr("omicsclaw.bench.__main__.run_campaign", spy)

    assert main(["run", *arguments(toy), "--env-file", str(env_file)]) == 0
    assert seen["BENCH_FROM_FILE"] == "from file"
    assert seen["BENCH_ALREADY_SET"] == "shell"
    capsys.readouterr()


ENV_SAMPLE = """\
# a comment line

A=1 # trailing comment
export B="two words"
C=plain
D='q'
E = spaced
F=
G="a#b"
H=a#b
"""
"""Lines the agent's own ``.env`` loader reads the same way with and without
python-dotenv installed."""

ENV_EXPECTED = {
    "A": "1", "B": "two words", "C": "plain", "D": "q", "E": "spaced",
    "F": "", "G": "a#b", "H": "a#b",
}


def test_the_env_file_is_parsed_into_names_and_values(tmp_path):
    """A comment after a value is not part of the value. Read as
    ``'1 # trailing comment'``, a key with a note beside it would be sent
    to the backend whole and refused.
    """
    path = tmp_path / "sample.env"
    path.write_text(ENV_SAMPLE + 'I="quoted" # note\n')

    assert _env_file(path) == {**ENV_EXPECTED, "I": "quoted"}


def test_the_env_file_is_read_the_way_the_agent_reads_its_own(tmp_path):
    """The same file is given to the agent's loader in a clean process, and
    the two agree on every line of the sample.
    """
    path = tmp_path / "sample.env"
    path.write_text(ENV_SAMPLE)
    probe = (
        "import json, os, sys\n"
        "from omicsclaw.common.runtime_env import load_env_file\n"
        "load_env_file(sys.argv[1], override=True)\n"
        "print(json.dumps({k: os.environ.get(k) for k in sys.argv[2:]}))\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", probe, str(path), *ENV_EXPECTED],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == _env_file(path) == ENV_EXPECTED


def test_example_writes_the_toy_cases(tmp_path, capsys):
    assert main(["example", str(tmp_path / "cases")]) == 0

    capsys.readouterr()
    case = tmp_path / "cases" / "sum-a"
    truth = json.loads((case / "oracle" / "truth.json").read_text())
    numbers = (case / "public" / "data" / "numbers.txt").read_text()
    assert truth == {"sum": sum(int(line) for line in numbers.split())}
