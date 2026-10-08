"""Running a campaign with the fake adapter: records, resuming, retrying."""

from __future__ import annotations

import fcntl
import json
import time

import pytest

from omicsclaw.bench.layout import read_jsonl
from omicsclaw.bench.manifest import ManifestError
from omicsclaw.bench.run import CampaignLocked, run_campaign, stage_campaign
from omicsclaw.bench.stage import StageError

from ._support import OK_EVIDENCE, Toy, ok

INFRA = {
    "exit": 1,
    "evidence": {"infra_reason": "provider_error: ProviderError status=500"},
}
QUIET = {"report": lambda message: None}
LOGGED = {"write": "right", "evidence": OK_EVIDENCE}
"""Delivers the right answer; a test adds the log file it appends to."""


def test_a_finished_run_leaves_its_record_and_one_row_in_each_file(tmp_path):
    toy = Toy(tmp_path)

    summary = run_campaign(toy.manifest, toy.campaign, **QUIET)

    key = "a/m/sum-a/r1"
    done = toy.done(key)
    assert summary.executed == 1 and summary.outcomes == {"completed": 1}
    assert done["outcome"] == "completed" and done["attempt"] == 1
    assert done["exit_code"] == 0 and done["wall_s"] >= 0
    assert done["deliverables"] == [
        {"path": "output/answer.json", "exists": True, "bytes": 12}
    ]
    predictions = read_jsonl(toy.campaign.predictions)
    usage = read_jsonl(toy.campaign.usage)
    assert [row["run"] for row in predictions] == [key]
    assert predictions[0]["workspace"] == f"cells/{key}"
    assert predictions[0]["meta"] == f"meta/{key}"
    assert [row["run"] for row in usage] == [key]
    assert usage[0]["input_tokens"] == 120
    assert usage[0]["cached_input_tokens"] == 20
    assert usage[0]["output_tokens"] == 7
    assert usage[0]["llm_calls"] == 2
    assert usage[0]["outcome"] == "completed"
    assert usage[0]["model_resolved"] == predictions[0]["model_resolved"] == (
        "fake-model"
    )
    command = json.loads(toy.paths(key).command.read_text())
    assert done["agent_code"] == command["provenance"] == {"source_root": "fake"}
    assert usage[0]["wall_s"] == done["wall_s"]


def test_the_run_record_holds_no_inherited_environment_values(tmp_path):
    """``command.json`` keeps the values the harness set and only the names
    of what the process inherited, which is where a credential would be.
    """
    toy = Toy(tmp_path)

    run_campaign(
        toy.manifest,
        toy.campaign,
        base_env={"PATH": "/usr/bin:/bin", "LLM_API_KEY": "sk-not-for-disk"},
        **QUIET,
    )

    paths = toy.paths("a/m/sum-a/r1")
    command = json.loads(paths.command.read_text())
    assert command["harness_env"] == {"FAKE_RUN": "a/m/sum-a/r1"}
    assert "LLM_API_KEY" in command["inherited_env_names"]
    for path in toy.out.rglob("*"):
        if path.is_file():
            assert "sk-not-for-disk" not in path.read_text(errors="replace")


def test_running_again_skips_what_is_finished(tmp_path):
    """The second invocation starts no process: the stand-in appends to a
    log every time it starts, and the log does not grow.
    """
    toy = Toy(tmp_path, cases=("sum-a", "sum-b"))
    log = tmp_path / "starts.log"
    toy.play({"default": [LOGGED | {"log": str(log)}]})

    first = run_campaign(toy.manifest, toy.campaign, **QUIET)
    started = log.read_text()
    second = run_campaign(toy.manifest, toy.campaign, **QUIET)

    assert (first.executed, first.skipped) == (2, 0)
    assert (second.executed, second.skipped) == (0, 2)
    assert log.read_text() == started
    assert len(read_jsonl(toy.campaign.predictions)) == 2


def test_a_run_interrupted_earlier_is_started_over_in_a_new_workspace(tmp_path):
    """A run with a launch record and no ``done.json`` was cut short. Its
    directories are kept under ``.incomplete1`` and the run starts clean.
    """
    toy = Toy(tmp_path)
    stage_campaign(toy.manifest, toy.campaign, **QUIET)
    paths = toy.paths("a/m/sum-a/r1")
    paths.command.write_text("{}")
    (paths.workspace / "half-written.txt").write_text("from the cut-short attempt")

    summary = run_campaign(toy.manifest, toy.campaign, **QUIET)

    assert summary.executed == 1
    assert toy.done("a/m/sum-a/r1")["attempt"] == 2
    assert not (paths.workspace / "half-written.txt").exists()
    assert (paths.workspace.with_name("r1.incomplete1") / "half-written.txt").exists()
    assert paths.meta.with_name("r1.incomplete1").is_dir()


def test_a_staged_run_is_used_as_staged(tmp_path):
    toy = Toy(tmp_path)

    assert stage_campaign(toy.manifest, toy.campaign, **QUIET) == 1
    assert stage_campaign(toy.manifest, toy.campaign, **QUIET) == 0
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    assert toy.done("a/m/sum-a/r1")["attempt"] == 1
    assert not toy.paths("a/m/sum-a/r1").meta.with_name("r1.incomplete1").exists()


def test_retry_infra_reruns_only_infrastructure_failures(tmp_path):
    """Two runs: one fails for infrastructure reasons, one delivers a wrong
    answer. Without the flag neither runs again. With it the failed one is
    run again under attempt 2, its first attempt kept as ``r1.infra1``,
    and the wrong answer is left exactly as it was.
    """
    toy = Toy(tmp_path, cases=("sum-a", "sum-b"))
    wrong = {"write": {"output/answer.json": '{"sum": 1}'}, "evidence": OK_EVIDENCE}
    toy.play({"a/m/sum-a/r1": [INFRA, ok("sum-a")], "a/m/sum-b/r1": [wrong]})

    first = run_campaign(toy.manifest, toy.campaign, **QUIET)
    wrong_record = toy.paths("a/m/sum-b/r1").done.read_text()
    plain = run_campaign(toy.manifest, toy.campaign, **QUIET)
    retried = run_campaign(toy.manifest, toy.campaign, retry_infra=True, **QUIET)

    assert first.outcomes == {"completed": 1, "infra_failure": 1}
    assert (plain.executed, plain.skipped) == (0, 2)
    assert (retried.executed, retried.skipped) == (1, 1)
    assert retried.outcomes == {"completed": 2}
    done = toy.done("a/m/sum-a/r1")
    assert done["attempt"] == 2 and done["outcome"] == "completed"
    aside = toy.paths("a/m/sum-a/r1").meta.with_name("r1.infra1")
    assert json.loads((aside / "done.json").read_text())["outcome"] == "infra_failure"
    assert toy.paths("a/m/sum-a/r1").workspace.with_name("r1.infra1").is_dir()
    assert toy.paths("a/m/sum-b/r1").done.read_text() == wrong_record
    rows = read_jsonl(toy.campaign.predictions)
    assert [(row["run"], row["attempt"]) for row in rows] == [
        ("a/m/sum-a/r1", 2), ("a/m/sum-b/r1", 1),
    ]


def test_every_attempt_is_listed_so_the_total_spend_can_be_added_up(tmp_path):
    """``usage.jsonl`` has one row per run and so leaves out what earlier
    attempts cost. ``attempts.jsonl`` has one row per attempt on disk: two
    infrastructure failures set aside, an attempt cut short before it left
    a record, and the attempt that counted.
    """
    toy = Toy(tmp_path)
    spent = {"usage": {"input_tokens": 500, "output_tokens": 5, "llm_calls": 1}}
    failed = {"exit": 1, "evidence": {"infra_reason": "provider_error: x", **spent}}
    toy.play({"default": [failed, ok("sum-a"), ok("sum-a")]})
    run_campaign(toy.manifest, toy.campaign, **QUIET)
    run_campaign(toy.manifest, toy.campaign, retry_infra=True, **QUIET)
    toy.paths("a/m/sum-a/r1").done.unlink()  # the harness died before recording
    run_campaign(toy.manifest, toy.campaign, **QUIET)

    rows = read_jsonl(toy.out / "attempts.jsonl")

    assert [(row["attempt_dir"], row["outcome"], row["current"]) for row in rows] == [
        ("r1.infra1", "infra_failure", False),
        ("r1.incomplete1", "incomplete", False),
        ("r1", "completed", True),
    ]
    assert [row["attempt"] for row in rows] == [1, 2, 3]
    assert [row["input_tokens"] for row in rows] == [500, None, 120]
    assert {row["run"] for row in rows} == {"a/m/sum-a/r1"}
    assert len(read_jsonl(toy.campaign.usage)) == 1


def test_an_infrastructure_failure_is_not_recorded_as_a_result(tmp_path):
    """The stand-in delivers the right answer and exits ``0`` while
    reporting an unrecovered model failure. The row says ``infra_failure``.
    """
    toy = Toy(tmp_path)
    toy.play({"default": [ok("sum-a", evidence={
        "stop_reason": "converged",
        "infra_reason": "provider_error: ProviderError in a sub-agent",
    })]})

    summary = run_campaign(toy.manifest, toy.campaign, **QUIET)

    assert summary.outcomes == {"infra_failure": 1}
    row = read_jsonl(toy.campaign.predictions)[0]
    assert row["outcome"] == "infra_failure"
    assert "provider_error" in row["reason"]
    assert row["deliverables"][0]["exists"] is True


def test_a_run_that_outlives_its_budget_is_a_timeout(tmp_path):
    toy = Toy(tmp_path, wall_clock_s=1, kill_grace_s=2)
    toy.play({"default": [{"sleep": 60, "evidence": OK_EVIDENCE}]})

    run_campaign(toy.manifest, toy.campaign, **QUIET)

    done = toy.done("a/m/sum-a/r1")
    assert done["outcome"] == "timeout" and done["timed_out"] is True
    assert done["wall_s"] < 6


def test_runs_start_in_the_manifest_order_one_at_a_time(tmp_path):
    toy = Toy(tmp_path, arms=("a", "b"), cases=("sum-a", "sum-b"), repeats=2)
    log = tmp_path / "order.log"
    toy.play({"default": [LOGGED | {"log": str(log)}]})

    run_campaign(toy.manifest, toy.campaign, jobs=1, **QUIET)

    lines = [line.split() for line in log.read_text().splitlines()]
    started = [label for event, label, _ in lines if event == "start"]
    assert started == [run.key for run in toy.manifest.runs()]
    assert [event for event, _, _ in lines] == ["start", "end"] * 8


def test_jobs_sets_how_many_runs_are_in_progress_at_once(tmp_path):
    toy = Toy(tmp_path, arms=("a", "b"), cases=("sum-a", "sum-b"), repeats=2)
    log = tmp_path / "overlap.log"
    toy.play({"default": [LOGGED | {"log": str(log), "sleep": 0.4}]})

    summary = run_campaign(toy.manifest, toy.campaign, jobs=3, **QUIET)

    events = sorted(
        (float(clock), 1 if event == "start" else -1)
        for event, _, clock in (line.split() for line in log.read_text().splitlines())
    )
    running = peak = 0
    for _, change in events:
        running += change
        peak = max(peak, running)
    assert summary.executed == 8 and summary.outcomes == {"completed": 8}
    assert peak == 3
    assert len(read_jsonl(toy.campaign.usage)) == 8


def test_an_interrupt_stops_the_campaign_and_leaves_open_runs_open(tmp_path):
    """The harness is interrupted when the first run reports in. The run
    then in progress is killed without a ``done.json`` and the ones not
    yet started never start, so the next invocation picks all three up.
    """
    toy = Toy(tmp_path, cases=("sum-a", "sum-b"), repeats=2)
    first = toy.manifest.runs()[0].key
    toy.play({"default": [LOGGED | {"sleep": 30}], first: [LOGGED]})

    def interrupt(message: str) -> None:
        if message.startswith("[done]"):
            raise KeyboardInterrupt

    began = time.monotonic()
    summary = run_campaign(toy.manifest, toy.campaign, jobs=2, report=interrupt)

    assert time.monotonic() - began < 10
    assert summary.interrupted and summary.executed == 1
    assert (summary.outcomes, summary.unfinished) == ({"completed": 1}, 3)
    finished = sorted(path.parent.name for path in toy.out.rglob("done.json"))
    assert len(finished) == 1 and toy.paths(first).done.exists()


def test_a_held_lock_refuses_stage_and_run_until_it_is_released(tmp_path):
    toy = Toy(tmp_path)
    toy.out.mkdir()
    with open(toy.out / ".bench.lock", "a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CampaignLocked, match="in use by another"):
            run_campaign(toy.manifest, toy.campaign, **QUIET)
        with pytest.raises(CampaignLocked):
            stage_campaign(toy.manifest, toy.campaign, **QUIET)
        assert not (toy.out / "cells").exists()

    assert run_campaign(toy.manifest, toy.campaign, **QUIET).executed == 1


def test_select_narrows_what_is_run(tmp_path):
    toy = Toy(tmp_path, arms=("a", "b"), cases=("sum-a", "sum-b"))

    summary = run_campaign(toy.manifest, toy.campaign, select="b/*/sum-b/r1", **QUIET)

    assert summary.executed == 1 and summary.unfinished == 3
    assert [row["run"] for row in read_jsonl(toy.campaign.predictions)] == [
        "b/m/sum-b/r1"
    ]


def test_a_flagged_run_is_marked_and_still_counted(tmp_path):
    """The stand-in names the cases root in a command and leaves a script
    that names it too. The run completes and is graded like any other; it
    is only marked.
    """
    toy = Toy(tmp_path)
    oracle = toy.cases / "sum-a" / "oracle" / "truth.json"
    toy.play({"default": [ok("sum-a", evidence={
        **OK_EVIDENCE, "commands": [["bash", f"cat {oracle}"]],
    }) | {"write": {
        "output/answer.json": '{"sum": 189}',
        "peek.py": f"open({str(oracle)!r}).read()\n",
    }}]})

    run_campaign(toy.manifest, toy.campaign, **QUIET)

    done = toy.done("a/m/sum-a/r1")
    assert done["outcome"] == "completed"
    assert done["access"] == {
        "flagged": True, "matches": 2, "commands_scanned": 1,
    }
    report = json.loads(toy.paths("a/m/sum-a/r1").access.read_text())
    assert {hit["source"] for hit in report["hits"]} == {"command", "file"}
    assert {hit["pattern"] for hit in report["hits"]} == {"cases_root"}
    assert read_jsonl(toy.campaign.predictions)[0]["access_flagged"] is True


def test_a_harness_error_leaves_the_run_open(tmp_path):
    """A run the harness itself failed on gets no ``done.json``, so the
    next invocation tries it again instead of reading a made-up outcome.
    """
    toy = Toy(tmp_path)
    toy.playbook.write_text("not json")

    summary = run_campaign(toy.manifest, toy.campaign, **QUIET)

    assert summary.executed == 0 and summary.unfinished == 1
    assert len(summary.errors) == 1 and "a/m/sum-a/r1" in summary.errors[0]
    assert not toy.paths("a/m/sum-a/r1").done.exists()


def test_a_missing_case_stops_the_campaign_before_any_run(tmp_path):
    toy = Toy(tmp_path, cases=("sum-a", "sum-b"))
    (toy.cases / "sum-b" / "public").rename(toy.cases / "sum-b" / "gone")

    with pytest.raises(StageError, match="sum-b"):
        run_campaign(toy.manifest, toy.campaign, **QUIET)

    assert not (toy.out / "cells").exists()


def test_an_arm_may_not_set_a_variable_its_adapter_owns(tmp_path):
    toy = Toy(tmp_path)
    text = toy.manifest_path.read_text().replace(
        "options =", 'env = { FAKE_RESERVED = "1" }\noptions ='
    )
    toy.manifest_path.write_text(text)

    with pytest.raises(ManifestError, match="FAKE_RESERVED"):
        run_campaign(toy.manifest, toy.campaign, **QUIET)
