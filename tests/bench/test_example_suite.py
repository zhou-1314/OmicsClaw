"""The example suite shipped in ``bench/example``, run through the command.

The suite's manifest is the one in the repository. Its cases are written
to a temporary directory, and the agent is the real ``oc cli`` with the
scripted backend from ``_support``, so the whole path from the command
line to ``grades.jsonl`` runs without a model.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from omicsclaw.bench.adapters import build_adapter
from omicsclaw.bench.example import ANSWER, CASES
from omicsclaw.bench.grade import load_grader, self_check
from omicsclaw.bench.layout import read_jsonl
from omicsclaw.bench.manifest import load_manifest

from ._support import scripted_backend

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "bench" / "example" / "manifest.toml"


def test_the_shipped_manifest_is_usable_as_written():
    manifest = load_manifest(MANIFEST)

    assert {case.id for case in manifest.cases} == set(CASES)
    assert all(case.deliverables == (ANSWER,) for case in manifest.cases)
    assert all("data/numbers.txt" in case.prompt for case in manifest.cases)
    for arm in manifest.arms:
        build_adapter(arm)
        rules = json.loads(arm.permission_rules.read_text())
        assert rules == {"permissions": {"deny": ["web_search", "web_fetch"]}}
    for reference in {case.grader for case in manifest.cases}:
        assert self_check(load_grader(reference)) == []
    assert len(manifest.runs()) == 2


def test_the_example_suite_runs_from_the_command_line_to_grades(tmp_path):
    """``example``, ``stage``, ``run`` and ``grade`` as four processes over
    the shipped manifest. Both runs complete, both are graded as passed,
    and each of the three result files has one row per run.
    """
    cases, out = tmp_path / "cases", tmp_path / "out"
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"PYTHONPATH={scripted_backend(tmp_path)}\n")
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    where = [str(MANIFEST), "--cases", str(cases), "--out", str(out)]

    def command(*words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "omicsclaw.bench", *words],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=environment,
            timeout=180,
        )

    made = command("example", str(cases))
    staged = command("stage", *where)
    ran = command("run", *where, "--jobs", "2", "--env-file", str(env_file))
    graded = command("grade", *where)

    assert made.returncode == 0, made.stderr
    assert staged.returncode == 0 and json.loads(staged.stdout) == {"staged": 2}
    assert ran.returncode == 0, ran.stderr + ran.stdout
    assert json.loads(ran.stdout)["outcomes"] == {"completed": 2}
    assert graded.returncode == 0, graded.stderr + graded.stdout
    assert json.loads(graded.stdout) == {"graded": 2, "passed": 2, "skipped": {}}

    predictions = read_jsonl(out / "predictions.jsonl")
    usage = read_jsonl(out / "usage.jsonl")
    grades = read_jsonl(out / "grades.jsonl")
    assert [row["case"] for row in predictions] == ["sum-a", "sum-b"]
    assert all(row["outcome"] == "completed" for row in predictions)
    assert all(row["llm_calls"] == 2 and row["input_tokens"] == 2000 for row in usage)
    assert all(row["includes_subagents"] is True for row in usage)
    assert [(row["case"], row["passed"], row["score"]) for row in grades] == [
        ("sum-a", True, 1.0), ("sum-b", True, 1.0),
    ]
    for case, numbers in CASES.items():
        answer = out / "cells" / "oc" / "default" / case / "r1" / ANSWER
        assert json.loads(answer.read_text()) == {"sum": sum(numbers)}
