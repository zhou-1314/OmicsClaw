"""The OmicsClaw adapter: the command it builds and how it reads a run back.

The telemetry lines below have the shape ``oc cli`` writes with
``OTEL_EXPORTER_TYPE=stdout``; ``test_oc_cli_contract.py`` checks that
shape against the real command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omicsclaw.bench.adapters import build_adapter
from omicsclaw.bench.adapters.omicsclaw import OmicsClawAdapter, read_trace
from omicsclaw.bench.layout import RunPaths
from omicsclaw.bench.manifest import Arm, Budget, Case, ManifestError, Model, RunSpec
from omicsclaw.bench.outcome import (
    APPROVAL_DENIED,
    COMPLETED,
    INFRA_FAILURE,
    MAX_TURNS,
    TIMEOUT,
    ProcessExit,
    classify,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = str(REPO_ROOT / "omicsclaw" / "__init__.py")
EXITED = ProcessExit(started=True, returncode=0)


def run_spec(arm: Arm, provider: str = "deepseek", model: str = "v4") -> RunSpec:
    case = Case("c1", "Do it.", ("output/a.json",))
    return RunSpec(arm, Model("m", provider, model), case, 1)


def paths(tmp_path: Path) -> RunPaths:
    found = RunPaths(tmp_path / "cells" / "r1", tmp_path / "meta" / "r1")
    found.workspace.mkdir(parents=True)
    found.meta.mkdir(parents=True)
    return found


# ---- telemetry lines, as the product writes them ---------------------------


def span(name, span_id, parent="", error="", **attributes) -> str:
    record = {
        "name": f"omicsclaw.{name}",
        "span_id": span_id,
        "parent_span_id": parent,
        "trace_id": "t",
        "duration_s": 0.1,
        "attributes": attributes,
    }
    if error:
        record["error"] = error
    return json.dumps(record)


def llm(span_id, parent, *, tokens=(1000, 10, 400), error="", status=None, output=""):
    attributes = {"llm.model": "v4"}
    if tokens and not error:
        attributes.update({
            "llm.tokens.input": tokens[0],
            "llm.tokens.output": tokens[1],
            "llm.tokens.cache_read": tokens[2],
        })
    if error:
        attributes["error.type"] = error
    if status is not None:
        attributes["error.status_code"] = status
    if output:
        attributes["langfuse.observation.output"] = output
    return span("llm_request", span_id, parent, error, **attributes)


def tool(span_id, parent, name, *, status="ok", error="", arguments=""):
    attributes = {"tool.name": name, "tool.status": status}
    if arguments:
        attributes["langfuse.observation.input"] = arguments
    return span("tool", span_id, parent, error, **attributes)


def interaction(stop_reason="converged", turns=1, error=""):
    attributes = {"agent.type": "main", "session.id": "s1"}
    if stop_reason:
        attributes.update({"agent.stop_reason": stop_reason, "agent.turns": turns})
    return span("interaction", "root", "", error, **attributes)


def collected(tmp_path, lines, *, stdout="", exit=EXITED, arm=None, launched=HERE):
    found = paths(tmp_path)
    head = [f"bench-launch omicsclaw={launched}"] if launched else []
    found.stderr.write_text("\n".join([*head, *lines]) + "\n")
    found.stdout.write_text(stdout)
    arm = arm or Arm("oc", "omicsclaw")
    return OmicsClawAdapter(arm).collect(run_spec(arm), found, exit)


# ---- the command -----------------------------------------------------------


def test_the_command_is_one_oc_cli_exchange_from_this_checkout(tmp_path):
    arm = Arm("oc", "omicsclaw")
    found = paths(tmp_path)

    launch = OmicsClawAdapter(arm).launch(
        run_spec(arm), found, Budget(60, max_turns=30), {"PATH": "/bin", "KEY": "v"}
    )

    argv = list(launch.argv)
    assert argv[:3] == [sys.executable, "-P", "-c"]
    assert argv[4] == str(REPO_ROOT)
    assert argv[5:] == [
        "cli", "--workspace", str(found.workspace),
        "--provider", "deepseek", "--model", "v4",
        "--", "--prompt-file", str(found.prompt),
    ]
    assert launch.cwd == found.workspace
    assert launch.env["KEY"] == "v"
    assert launch.harness_env == {
        "OMICSCLAW_PERMISSION_MODE": "auto-approve",
        "OMICSCLAW_SKILLS_DIR": str(REPO_ROOT / "skills"),
        "OMICSCLAW_MAX_TURNS": "30",
        "OMICSCLAW_AUDIT_LOG": str(found.meta / "audit.jsonl"),
        "OTEL_ENABLED": "true",
        "OTEL_EXPORTER_TYPE": "stdout",
        "OMICSCLAW_OTEL_CAPTURE_CONTENT": "true",
    }
    assert found.workspace not in Path(launch.env["OMICSCLAW_AUDIT_LOG"]).parents


def test_an_arm_sets_the_skills_root_rules_environment_and_interpreter(tmp_path):
    arm = Arm(
        "bare",
        "omicsclaw",
        skills_dir=tmp_path / "roots" / "bare",
        env={"OMICSCLAW_PERMISSION_MODE": "default", "EXTRA": "1"},
        permission_rules=tmp_path / "rules.json",
        options={
            "python": "/env/bin/python",
            "source_root": "",
            "capture_content": False,
        },
    )

    launch = OmicsClawAdapter(arm).launch(
        run_spec(arm, provider="", model=""),
        paths(tmp_path),
        Budget(60),
        {"OMICSCLAW_WORKSPACE": "/somewhere/else"},
    )

    assert launch.argv[0] == "/env/bin/python" and launch.argv[4] == ""
    assert "--provider" not in launch.argv and "--model" not in launch.argv
    assert launch.env["OMICSCLAW_SKILLS_DIR"] == str(tmp_path / "roots" / "bare")
    assert launch.env["OMICSCLAW_PERMISSION_RULES"] == str(tmp_path / "rules.json")
    assert launch.env["OMICSCLAW_PERMISSION_MODE"] == "default"
    assert launch.env["EXTRA"] == "1"
    assert launch.env["OMICSCLAW_OTEL_CAPTURE_CONTENT"] == "false"
    assert "OMICSCLAW_MAX_TURNS" not in launch.env
    assert "OMICSCLAW_WORKSPACE" not in launch.env


@pytest.mark.parametrize(
    "name", ["OMICSCLAW_AUDIT_LOG", "OTEL_ENABLED", "OTEL_EXPORTER_TYPE"]
)
def test_an_arm_cannot_turn_off_what_the_harness_reads(name):
    """Usage and the access audit come from telemetry and the audit log.
    An arm that redirected either would produce runs with nothing to read.
    """
    with pytest.raises(ManifestError, match=name):
        build_adapter(Arm("oc", "omicsclaw", env={name: "x"}))


def test_an_unknown_option_is_refused():
    with pytest.raises(ManifestError, match="pyton"):
        OmicsClawAdapter(Arm("oc", "omicsclaw", options={"pyton": "x"}))


# ---- usage -----------------------------------------------------------------


def test_usage_is_summed_over_every_model_call(tmp_path):
    evidence = collected(tmp_path, [
        llm("a", "turn1"),
        tool("t1", "turn1", "bash"),
        span("turn", "turn1", "root"),
        llm("b", "turn2", tokens=(1500, 30, 1000)),
        span("turn", "turn2", "root"),
        interaction(turns=2),
    ])

    usage = evidence.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (
        2500, 1400, 40,
    )
    assert (usage.llm_calls, usage.llm_errors, usage.llm_cancelled) == (2, 0, 0)
    assert usage.subagent_llm_calls == 0 and usage.calls_without_usage == 0
    assert usage.includes_subagents is True
    assert evidence.turns == 2 and evidence.stop_reason == "converged"
    assert evidence.notes["model_resolved"] == "v4"
    assert evidence.notes["session_id"] == "s1"


def test_a_sub_agents_calls_are_counted_though_the_transcript_omits_them(tmp_path):
    """One delegation: the parent makes two calls and the sub-agent two,
    under the ``task`` tool's span. The transcript's per-turn lines and the
    interaction span both report the parent's 2,000 input tokens. The
    usage row reports all four calls and 4,000.
    """
    transcript = (
        "Turn 1 done, tokens: 1000 in / 10 out\n"
        "Turn 2 done, tokens: 1000 in / 10 out\n"
    )
    evidence = collected(tmp_path, [
        llm("a", "turn1"),
        llm("s1", "task1"),
        llm("s2", "task1"),
        tool("task1", "turn1", "task"),
        span("turn", "turn1", "root"),
        llm("b", "turn2"),
        span("turn", "turn2", "root"),
        interaction(turns=2),
    ], stdout=transcript)

    usage = evidence.usage
    assert usage.llm_calls == 4 and usage.subagent_llm_calls == 2
    assert usage.input_tokens == 4000 and usage.output_tokens == 40
    assert usage.includes_subagents is True


def test_calls_that_reported_no_tokens_are_counted_as_such(tmp_path):
    evidence = collected(tmp_path, [
        llm("a", "turn1"),
        llm("b", "turn2", tokens=None),
        llm("c", "turn3", error="CancelledError"),
        interaction(),
    ])

    usage = evidence.usage
    assert usage.llm_calls == 3 and usage.calls_without_usage == 2
    assert usage.llm_cancelled == 1 and usage.llm_errors == 0
    assert usage.input_tokens == 1000 and usage.answered() == 2


def test_the_processes_own_counters_are_compared_with_the_spans(tmp_path):
    def metrics(total):
        return json.dumps({"metrics": {
            "omicsclaw.llm.tokens.input": [{"total": total, "count": 2}],
            "omicsclaw.llm.tokens.output": [{"total": 20.0, "count": 2}],
        }})

    lines = [llm("a", "turn1"), llm("b", "turn2"), interaction(turns=2)]

    agree = collected(tmp_path / "1", [*lines, metrics(2000.0)])
    differ = collected(tmp_path / "2", [*lines, metrics(3000.0)])
    absent = collected(tmp_path / "3", lines)

    assert agree.notes["meter"] == "agrees"
    assert differ.notes["meter"] == "differs"
    assert absent.notes["meter"] == "absent"


def test_noise_on_standard_error_does_not_break_the_reading(tmp_path):
    found = paths(tmp_path)
    found.stderr.write_text(
        "a log line\n{not json\n[1, 2]\n" + llm("a", "turn1") + '\n{"name": 3}\n'
        + interaction()[:40]
    )

    trace = read_trace(found.stderr)

    assert len(trace.spans) == 1
    assert read_trace(tmp_path / "absent.txt").spans == ()


# ---- infrastructure failures ----------------------------------------------


def test_a_provider_error_that_ends_the_exchange_is_infrastructure(tmp_path):
    evidence = collected(
        tmp_path,
        [
            llm("a", "turn1", error="ProviderError", status=500),
            llm("b", "turn1", error="ProviderError", status=500),
            span("turn", "turn1", "root"),
            interaction(stop_reason="", error="ProviderError"),
        ],
        stdout="Context: 1 tokens\nFailed: ProviderError\n",
        exit=ProcessExit(started=True, returncode=1),
    )

    assert evidence.infra_reason == "provider_error: ProviderError status=500"
    assert evidence.failure == "Failed: ProviderError"
    assert evidence.usage.llm_errors == 2 and evidence.usage.answered() == 0
    outcome, _ = classify(ProcessExit(started=True, returncode=1), evidence, ["x"])
    assert outcome == INFRA_FAILURE


def test_a_sub_agent_that_dies_of_a_provider_error_is_infrastructure(tmp_path):
    """The run this adapter must not let through. The sub-agent's model
    call fails, the ``task`` tool hands the parent an error, the parent
    answers anyway, the process exits ``0`` and says it converged, and the
    deliverable is on disk. The exit code and the transcript both look
    like success; only the spans under the tool call show the failure.
    """
    evidence = collected(tmp_path, [
        llm("a", "turn1"),
        llm("s1", "task1", error="ProviderError", status=500),
        llm("s2", "task1", error="ProviderError", status=500),
        tool("task1", "turn1", "task", status="error", error="ProviderError"),
        span("turn", "turn1", "root"),
        llm("b", "turn2"),
        span("turn", "turn2", "root"),
        interaction(turns=2),
    ], stdout="Turn 2 done, tokens: 1000 in / 10 out\n")

    assert evidence.stop_reason == "converged"
    assert evidence.infra_reason == (
        "provider_error: ProviderError status=500 in a sub-agent"
    )
    assert classify(EXITED, evidence, [])[0] == INFRA_FAILURE


def test_a_model_call_that_failed_and_was_retried_is_not_a_failure(tmp_path):
    evidence = collected(tmp_path, [
        llm("a", "turn1", error="ProviderError", status=500),
        llm("b", "turn1"),
        span("turn", "turn1", "root"),
        interaction(),
    ])

    assert evidence.infra_reason == ""
    assert evidence.usage.llm_errors == 1 and evidence.usage.calls_without_usage == 1
    assert classify(EXITED, evidence, [])[0] == COMPLETED


def test_a_call_cut_short_by_stopping_the_run_is_not_a_provider_error(tmp_path):
    exit = ProcessExit(started=True, returncode=143, timed_out=True)
    evidence = collected(tmp_path, [
        llm("a", "turn1"),
        span("turn", "turn1", "root"),
        llm("b", "turn2", error="CancelledError"),
        span("turn", "turn2", "root"),
        interaction(stop_reason="", error="CancelledError"),
    ], exit=exit)

    assert evidence.infra_reason == ""
    assert classify(exit, evidence, ["x"])[0] == TIMEOUT


def test_a_process_running_other_code_is_infrastructure(tmp_path):
    """The run imported ``omicsclaw`` from a different checkout than the
    one the arm names, so it did not test what the manifest says.
    """
    evidence = collected(
        tmp_path, [llm("a", "turn1"), interaction()],
        launched="/elsewhere/omicsclaw/__init__.py",
    )
    namespace = collected(
        tmp_path / "ns", [llm("a", "turn1"), interaction()], launched="None"
    )

    assert evidence.infra_reason.startswith("source_mismatch")
    assert namespace.infra_reason.startswith("source_mismatch")
    assert classify(EXITED, evidence, [])[0] == INFRA_FAILURE


def test_a_run_that_left_no_telemetry_is_not_a_result(tmp_path):
    evidence = collected(tmp_path, [], stdout="All done.\n")

    assert evidence.stop_reason == ""
    assert classify(EXITED, evidence, [])[0] == INFRA_FAILURE


# ---- how the agent's own loop ended ---------------------------------------


def test_a_turn_limit_is_read_from_telemetry_because_the_exit_code_is_zero(tmp_path):
    evidence = collected(
        tmp_path,
        [llm("a", "turn1"), interaction(stop_reason="max_turns", turns=30)],
        stdout="Turn 30 done, tokens: 1000 in / 10 out\n",
    )

    assert classify(EXITED, evidence, [])[0] == MAX_TURNS


def test_the_agents_own_deadline_is_a_timeout(tmp_path):
    exit = ProcessExit(started=True, returncode=1)
    evidence = collected(
        tmp_path,
        [llm("a", "turn1"), interaction(stop_reason="", error="CancelledError")],
        stdout="Failed: TimeoutError\n",
        exit=exit,
    )

    assert classify(exit, evidence, ["x"]) == (TIMEOUT, "agent_deadline")


# ---- approval cards --------------------------------------------------------

CARD = (
    "Approval required [62f3#1]: bash (risk high) - \n"
    "deletes files and directories recursively\n"
    '(1) -> bash  command="rm -rf /tmp/x"\n'
    "Approval denied [62f3#1]: no operator at the \n"
    "terminal\n"
    "(1) <- bash error, 0.0s elapsed (includes any approval wait)\n"
)


def test_a_refused_approval_keeps_the_run_from_counting_as_completed(tmp_path):
    """A card nobody could answer was refused and the agent went on to
    deliver. The denied call has no tool span and no audit line; the
    transcript is the only place it shows.
    """
    evidence = collected(tmp_path, [llm("a", "turn1"), interaction()], stdout=CARD)

    assert (evidence.approvals_required, evidence.approvals_denied) == (1, 1)
    assert evidence.approvals_pending == 0
    assert classify(EXITED, evidence, [])[0] == APPROVAL_DENIED


def test_a_card_still_open_when_the_run_was_stopped_is_counted(tmp_path):
    coloured = "\x1b[2mApproval required [9#1]: bash (risk high)\x1b[0m\n"
    evidence = collected(tmp_path, [llm("a", "turn1"), interaction()], stdout=coloured)

    assert (evidence.approvals_required, evidence.approvals_pending) == (1, 1)


def test_the_word_approval_inside_an_answer_is_not_a_card(tmp_path):
    answer = (
        "      Approval required [x]: quoted from a tool's output\n"
        "No Approval denied [\n"
    )
    evidence = collected(tmp_path, [llm("a", "turn1"), interaction()], stdout=answer)

    assert evidence.approvals_required == 0 and evidence.approvals_denied == 0


# ---- commands for the access audit ----------------------------------------


def test_commands_are_the_calls_that_ran_plus_the_ones_refused(tmp_path):
    """A call that ran is taken from its tool span. A refused call has no
    span, so it is taken from the model's own output; a call present in
    both is listed once.
    """
    ran = '{"command": "ls data"}'
    refused = '{"command": "rm -rf /cases/c1/oracle"}'
    requested = json.dumps([
        {"id": "1", "name": "bash", "arguments": ran},
        {"id": "2", "name": "bash", "arguments": refused},
    ])
    evidence = collected(tmp_path, [
        llm("a", "turn1", output=requested),
        tool("t1", "turn1", "bash", arguments=ran),
        llm("b", "turn2", output="A plain answer."),
        interaction(turns=2),
    ])

    assert [(c.tool, c.text) for c in evidence.commands] == [
        ("bash", ran), ("bash", refused),
    ]


def test_model_output_that_was_cut_is_kept_as_text(tmp_path):
    cut = (
        '[{"id":"1","name":"bash","arguments":"{\\"command\\": \\"cat /cases/c1'
        "…[truncated]"
    )
    evidence = collected(tmp_path, [llm("a", "turn1", output=cut), interaction()])

    assert [(c.tool, c.text) for c in evidence.commands] == [("model_output", cut)]
