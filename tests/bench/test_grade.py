"""Grading: the grader's controls, the health check, and the rows written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omicsclaw.bench.example import ANSWER, SumGrader
from omicsclaw.bench.grade import (
    Control,
    Grade,
    GraderError,
    Submission,
    grade_campaign,
    self_check,
)
from omicsclaw.bench.layout import read_jsonl
from omicsclaw.bench.run import run_campaign

from ._support import OK_EVIDENCE, Toy, ok

QUIET = {"report": lambda message: None}
WRONG = {"write": {ANSWER: '{"sum": 1}'}, "evidence": OK_EVIDENCE}
INFRA = {"exit": 1, "evidence": {"infra_reason": "provider_error: ProviderError"}}

ORACLE = {"truth.json": '{"sum": 10}'}
RIGHT = Control("right", True, {ANSWER: '{"sum": 10}'}, ORACLE)
BAD = Control("bad", False, {ANSWER: '{"sum": 11}'}, ORACLE)


class Lenient(SumGrader):
    """Passes everything, which its failing control must catch."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        return Grade(True, 1.0)


class OnlyPositive(SumGrader):
    def controls(self):
        return (RIGHT,)


class OnlyNegative(SumGrader):
    def controls(self):
        return (BAD,)


class Crashing(SumGrader):
    def grade(self, submission: Submission, oracle: Path) -> Grade:
        raise KeyError("boom")


class CrashesOnRuns(SumGrader):
    """Passes its controls and raises on a real submission."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        if submission.case != "control":
            raise RuntimeError("cannot read this one")
        return super().grade(submission, oracle)


# ---- the grader's own controls ---------------------------------------------


def test_the_example_grader_passes_its_controls():
    assert self_check(SumGrader()) == []


def test_a_grader_that_passes_a_wrong_answer_fails_its_controls():
    problems = self_check(Lenient())

    assert problems
    assert all("must fail but passed" in problem for problem in problems)
    assert any("'wrong-sum'" in problem for problem in problems)


def test_a_grader_needs_a_control_of_each_kind():
    assert self_check(OnlyPositive()) == ["no control that must fail"]
    assert self_check(OnlyNegative()) == ["no control that must pass"]


def test_a_grader_that_raises_on_a_control_fails_it():
    problems = self_check(Crashing())

    assert len(problems) == 4 and all("raised KeyError" in p for p in problems)


@pytest.mark.parametrize("grader", ["Lenient", "OnlyPositive", "Crashing"])
def test_a_grader_that_fails_its_controls_grades_nothing(tmp_path, grader):
    """Grading stops before the first run, and no grades file is written."""
    toy = Toy(tmp_path, grader=f"tests.bench.test_grade:{grader}")
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    with pytest.raises(GraderError, match="failed its controls"):
        grade_campaign(toy.manifest, toy.campaign)

    assert not toy.campaign.grades.exists()


# ---- which runs are graded -------------------------------------------------


def test_right_and_wrong_answers_are_graded_as_such(tmp_path):
    toy = Toy(tmp_path, cases=("sum-a", "sum-b"))
    toy.play({"a/m/sum-b/r1": [WRONG]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    summary = grade_campaign(toy.manifest, toy.campaign)

    rows = {row["case"]: row for row in read_jsonl(toy.campaign.grades)}
    assert (summary.graded, summary.passed, summary.skipped) == (2, 1, {})
    assert summary.ready
    assert rows["sum-a"]["graded"] and rows["sum-a"]["passed"] is True
    assert rows["sum-a"]["score"] == 1.0
    assert rows["sum-b"]["passed"] is False and rows["sum-b"]["score"] == 0.0
    assert rows["sum-b"]["metrics"] == {"abs_error": 1005}
    for row in rows.values():
        assert {"arm", "model_id", "case", "repeat", "outcome", "grader"} <= set(row)


def test_infrastructure_failures_and_unfinished_runs_are_not_graded(tmp_path):
    """Three runs: one finished, one failed for infrastructure reasons with
    the right answer on disk, one never run. Each has a row; only the first
    has a grade, and the summary says the campaign is not ready.
    """
    toy = Toy(tmp_path, arms=("a", "b"), cases=("sum-a", "sum-b"))
    toy.play({"a/m/sum-b/r1": [ok("sum-b") | INFRA]})
    run_campaign(toy.manifest, toy.campaign, select="a/*", **QUIET)
    run_campaign(toy.manifest, toy.campaign, select="b/m/sum-a/r1", **QUIET)

    summary = grade_campaign(toy.manifest, toy.campaign)

    rows = {row["run"]: row for row in read_jsonl(toy.campaign.grades)}
    assert len(rows) == 4
    assert rows["a/m/sum-a/r1"]["graded"] is True
    infra = rows["a/m/sum-b/r1"]
    assert (infra["graded"], infra["skip"], infra["passed"]) == (
        False, "infra_failure", None,
    )
    assert "provider_error" in infra["detail"]
    unfinished = rows["b/m/sum-b/r1"]
    assert (unfinished["graded"], unfinished["skip"], unfinished["outcome"]) == (
        False, "unfinished", "unfinished",
    )
    assert summary.skipped == {"infra_failure": 1, "unfinished": 1}
    assert not summary.ready


def test_a_run_without_its_deliverable_keeps_a_row_and_no_score(tmp_path):
    toy = Toy(tmp_path)
    toy.play({"default": [{"evidence": OK_EVIDENCE}]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    summary = grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert (row["outcome"], row["graded"], row["skip"], row["score"]) == (
        "no_deliverable", False, "no_deliverable", None,
    )
    assert summary.ready


def test_a_timed_out_run_with_a_deliverable_is_graded_and_keeps_its_outcome(tmp_path):
    toy = Toy(tmp_path, wall_clock_s=1, kill_grace_s=2)
    toy.play({"default": [ok("sum-a", sleep=60)]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert (row["outcome"], row["graded"], row["passed"]) == ("timeout", True, True)


# ---- the health check ------------------------------------------------------


def test_a_record_the_evidence_no_longer_supports_is_not_graded(tmp_path):
    """The run was recorded as completed. Its ``done.json`` is then edited
    to hide an infrastructure failure the evidence still shows; the health
    check reads the evidence again and refuses the run.
    """
    toy = Toy(tmp_path)
    toy.play({"default": [ok("sum-a") | INFRA]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    paths = toy.paths("a/m/sum-a/r1")
    record = json.loads(paths.done.read_text())
    assert record["outcome"] == "infra_failure"
    record.update(outcome="completed", reason="", exit_code=0)
    paths.done.write_text(json.dumps(record))

    summary = grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert (row["graded"], row["skip"]) == (False, "unhealthy")
    assert "evidence now says infra_failure" in row["health"][0]
    assert not summary.ready


def test_a_deliverable_removed_after_the_run_is_noticed(tmp_path):
    toy = Toy(tmp_path)
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    (toy.paths("a/m/sum-a/r1").workspace / ANSWER).unlink()

    grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert row["skip"] == "unhealthy"
    assert "evidence now says no_deliverable" in row["health"][0]


@pytest.mark.parametrize("lost", ["stdout.txt", "stderr.txt"])
def test_a_run_missing_its_output_files_is_not_graded(tmp_path, lost):
    toy = Toy(tmp_path)
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    (toy.paths("a/m/sum-a/r1").meta / lost).unlink()

    grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert row["skip"] == "unhealthy" and row["health"] == [f"{lost} is missing"]


def test_a_record_copied_from_another_run_is_not_graded(tmp_path):
    toy = Toy(tmp_path, cases=("sum-a", "sum-b"))
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    other = toy.paths("a/m/sum-a/r1").done.read_text()
    toy.paths("a/m/sum-b/r1").done.write_text(other)

    grade_campaign(toy.manifest, toy.campaign)

    rows = {row["case"]: row for row in read_jsonl(toy.campaign.grades)}
    assert rows["sum-a"]["graded"] is True
    assert rows["sum-b"]["skip"] == "unhealthy"
    assert "the record is for 'a/m/sum-a/r1'" in rows["sum-b"]["health"]


# ---- the file --------------------------------------------------------------


def test_grading_twice_writes_the_same_file_and_touches_no_run(tmp_path):
    toy = Toy(tmp_path, arms=("a", "b"), cases=("sum-a", "sum-b"))
    toy.play({"b/m/sum-a/r1": [WRONG]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    before = {
        path: path.read_bytes()
        for path in sorted(toy.out.rglob("*"))
        if path.is_file()
    }

    grade_campaign(toy.manifest, toy.campaign)
    first = toy.campaign.grades.read_bytes()
    grade_campaign(toy.manifest, toy.campaign)

    assert toy.campaign.grades.read_bytes() == first
    for path, content in before.items():
        assert path.read_bytes() == content


def test_a_grader_that_raises_on_a_run_is_recorded_on_its_row(tmp_path):
    toy = Toy(tmp_path, grader="tests.bench.test_grade:CrashesOnRuns")
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    summary = grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert (row["graded"], row["skip"]) == (False, "grader_error")
    assert row["detail"] == "RuntimeError: cannot read this one"
    assert not summary.ready


def test_a_case_without_an_oracle_cannot_be_graded(tmp_path):
    toy = Toy(tmp_path)
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    (toy.cases / "sum-a" / "oracle").rename(toy.cases / "sum-a" / "elsewhere")

    with pytest.raises(GraderError, match="no oracle directory"):
        grade_campaign(toy.manifest, toy.campaign)


def test_a_flagged_run_is_graded_and_carries_its_mark(tmp_path):
    toy = Toy(tmp_path)
    toy.play({"default": [ok("sum-a", evidence={
        **OK_EVIDENCE, "commands": [["bash", f"ls {toy.cases}"]],
    })]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    grade_campaign(toy.manifest, toy.campaign)

    row = read_jsonl(toy.campaign.grades)[0]
    assert (row["graded"], row["passed"], row["access_flagged"]) == (True, True, True)
