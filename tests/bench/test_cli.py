"""``python -m omicsclaw.bench``: the commands, their files and exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from omicsclaw.bench.__main__ import main
from omicsclaw.bench.layout import read_jsonl
from omicsclaw.bench.run import run_campaign

from ._support import Toy, ok

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


def test_results_and_cases_inside_the_repository_are_refused(tmp_path, capsys):
    """Case data and results belong outside the checkout. A ``.git`` above
    the manifest marks the checkout.
    """
    (tmp_path / ".git").mkdir()
    toy = Toy(tmp_path)

    status = main(["run", *arguments(toy)])

    assert status == 2
    assert "inside the repository" in capsys.readouterr().err
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


def test_example_writes_the_toy_cases(tmp_path, capsys):
    assert main(["example", str(tmp_path / "cases")]) == 0

    capsys.readouterr()
    case = tmp_path / "cases" / "sum-a"
    truth = json.loads((case / "oracle" / "truth.json").read_text())
    numbers = (case / "public" / "data" / "numbers.txt").read_text()
    assert truth == {"sum": sum(int(line) for line in numbers.split())}
