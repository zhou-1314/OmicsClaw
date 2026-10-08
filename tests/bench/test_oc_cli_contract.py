"""The OmicsClaw adapter against the real ``oc cli`` process.

The adapter's rules rest on how ``oc cli --prompt-file`` behaves: which
exit code it returns, what it writes to standard output, which telemetry
spans it emits. These tests start that command for real, once per
scenario, and check the outcome the harness records. If the command's
behaviour changes, the test for that behaviour fails here.

No model is called. The backend is the scripted one from
``_support.scripted_backend``, and the model name picks its script. The
permission gate, the tools, the sub-agent runner, telemetry and the audit
log are the product's own.

All scenarios run in one campaign, started once for the module.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from omicsclaw.bench.example import ANSWER, write_cases
from omicsclaw.bench.grade import grade_campaign
from omicsclaw.bench.layout import Campaign, read_jsonl
from omicsclaw.bench.manifest import load_manifest
from omicsclaw.bench.run import run_campaign

from ._support import scripted_backend

REPO_ROOT = Path(__file__).resolve().parents[2]

SCENARIOS = (
    "write", "subagent", "subfail", "fail", "loop", "danger", "web", "sleepy", "hang",
)
WALL_CLOCK_S = 6

MANIFEST = """\
name = "contract"
seed = 1

[budget]
wall_clock_s = {wall}
kill_grace_s = 10
max_turns = 4

{models}

[[arms]]
id = "oc"
adapter = "omicsclaw"
permission_rules = "rules.json"

[[cases]]
id = "sum-a"
prompt = "Add the numbers in data/numbers.txt and write output/answer.json."
deliverables = ["{answer}"]
grader = "omicsclaw.bench.example:SumGrader"
"""


class Contract:
    """The finished campaign and readers for one scenario's files."""

    def __init__(self, campaign: Campaign, manifest_path: Path) -> None:
        self.campaign = campaign
        self.manifest = load_manifest(manifest_path)

    def meta(self, scenario: str) -> Path:
        return self.campaign.meta / "oc" / scenario / "sum-a" / "r1"

    def workspace(self, scenario: str) -> Path:
        return self.campaign.cells / "oc" / scenario / "sum-a" / "r1"

    def done(self, scenario: str) -> dict:
        return json.loads((self.meta(scenario) / "done.json").read_text())

    def audited_tools(self, scenario: str) -> list[str]:
        log = self.meta(scenario) / "audit.jsonl"
        if not log.exists():
            return []
        return [json.loads(line)["tool"] for line in log.read_text().splitlines()]


@pytest.fixture(scope="module")
def contract(tmp_path_factory) -> Contract:
    root = tmp_path_factory.mktemp("contract")
    cases = root / "cases"
    write_cases(cases)
    shim = scripted_backend(root)
    suite = root / "suite"
    suite.mkdir()
    (suite / "rules.json").write_text(
        json.dumps({"permissions": {"deny": ["web_fetch", "web_search"]}})
    )
    models = "\n".join(
        f'[[models]]\nid = "{name}"\nprovider = "custom"\nmodel = "stub-{name}"\n'
        for name in SCENARIOS
    )
    manifest_path = suite / "manifest.toml"
    manifest_path.write_text(
        MANIFEST.format(wall=WALL_CLOCK_S, models=models, answer=ANSWER)
    )
    campaign = Campaign(out=root / "out", cases=cases)
    summary = run_campaign(
        load_manifest(manifest_path),
        campaign,
        jobs=4,
        base_env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(shim),
            "BENCH_ORACLE": str(cases / "sum-a" / "oracle"),
        },
        report=lambda message: None,
    )
    assert summary.executed == len(SCENARIOS), summary
    return Contract(campaign, manifest_path)


def test_the_process_runs_this_checkouts_code(contract):
    """The conda environment's ``oc`` is an editable install of another
    checkout. Every run says on standard error which ``omicsclaw`` it
    imported, and that is this tree.
    """
    here = str(REPO_ROOT / "omicsclaw" / "__init__.py")
    for scenario in SCENARIOS:
        done = contract.done(scenario)
        assert done["notes"]["omicsclaw_file"] == here, scenario
        command = json.loads((contract.meta(scenario) / "command.json").read_text())
        assert command["argv"][0] == sys.executable
        assert command["argv"][4] == str(REPO_ROOT)


def test_a_converged_run_is_completed_with_usage_from_telemetry(contract):
    done = contract.done("write")

    assert (done["outcome"], done["exit_code"], done["stop_reason"]) == (
        "completed", 0, "converged",
    )
    assert done["turns"] == 2 and done["approvals"]["required"] == 0
    usage = done["usage"]
    assert (usage["llm_calls"], usage["llm_errors"]) == (2, 0)
    assert (usage["input_tokens"], usage["cached_input_tokens"]) == (2000, 800)
    assert usage["output_tokens"] == 20 and usage["calls_without_usage"] == 0
    assert usage["includes_subagents"] is True
    assert done["notes"]["meter"] == "agrees"
    assert (contract.workspace("write") / ANSWER).read_text() == '{"sum": 189}'


def test_the_run_records_are_outside_the_workspace(contract):
    """The audit log, the telemetry and the transcript of a real run are
    written under ``meta``, and the workspace holds none of them.
    """
    meta, workspace = contract.meta("write"), contract.workspace("write")

    assert contract.audited_tools("write") == ["write_file"]
    assert (meta / "stderr.txt").stat().st_size > 0
    assert workspace not in meta.parents
    names = {path.name for path in workspace.rglob("*")}
    assert not names & {"audit.jsonl", "stderr.txt", "stdout.txt", "done.json"}


def test_sub_agent_calls_are_in_the_usage_and_not_in_the_transcript(contract):
    """One delegation makes four model calls: three by the parent and one
    by the sub-agent. The transcript's per-turn lines add up to the
    parent's three; the usage row has all four.
    """
    done = contract.done("subagent")
    transcript = (contract.meta("subagent") / "stdout.txt").read_text()
    printed = [
        int(tokens)
        for tokens in re.findall(r"Turn \d+ done, tokens: (\d+) in", transcript)
    ]

    assert done["outcome"] == "completed"
    assert sum(printed) == 3000
    assert done["usage"]["llm_calls"] == 4
    assert done["usage"]["subagent_llm_calls"] == 1
    assert done["usage"]["input_tokens"] == 4000
    assert done["notes"]["meter"] == "agrees"


def test_a_sub_agents_provider_error_is_infrastructure_despite_exit_zero(contract):
    """The sub-agent's backend refuses the call. The parent is handed a
    tool error, writes the answer itself and finishes: exit code ``0``,
    ``converged``, the right file on disk. The run is still recorded as an
    infrastructure failure, because a model call failed and nothing
    recovered it.
    """
    done = contract.done("subfail")

    assert (done["exit_code"], done["stop_reason"]) == (0, "converged")
    assert done["deliverables"][0]["exists"] is True
    assert done["outcome"] == "infra_failure"
    assert done["reason"] == "provider_error: ProviderError status=401 in a sub-agent"
    assert done["usage"]["llm_errors"] == 1


def test_a_provider_error_in_the_main_loop_is_infrastructure(contract):
    done = contract.done("fail")
    transcript = (contract.meta("fail") / "stdout.txt").read_text()

    assert done["exit_code"] == 1 and "Failed: ProviderError" in transcript
    assert done["outcome"] == "infra_failure"
    assert done["reason"] == "provider_error: ProviderError status=401"


def test_a_run_out_of_turns_exits_zero_and_is_recorded_as_max_turns(contract):
    done = contract.done("loop")

    assert done["exit_code"] == 0
    assert (done["outcome"], done["stop_reason"], done["turns"]) == (
        "max_turns", "max_turns", 4,
    )


def test_an_approval_card_is_refused_at_once_and_marks_the_run(contract):
    """In auto-approve mode a recursive delete still asks. With nobody to
    answer, the command refuses the card immediately: the run neither
    hangs nor counts as completed. The refused call never ran, so the audit
    log has no line for it, and the access audit still sees the path it
    named because the model's own output was captured.
    """
    done = contract.done("danger")
    transcript = (contract.meta("danger") / "stdout.txt").read_text()

    assert "no operator at the" in transcript
    assert done["approvals"] == {"required": 1, "denied": 1, "pending": 0}
    assert (done["outcome"], done["exit_code"]) == ("approval_denied", 0)
    assert done["timed_out"] is False and done["wall_s"] < WALL_CLOCK_S
    assert done["deliverables"][0]["exists"] is True
    assert "bash" not in contract.audited_tools("danger")
    assert (contract.campaign.cases / "sum-a" / "oracle" / "truth.json").exists()
    access = json.loads((contract.meta("danger") / "access_audit.json").read_text())
    assert done["access"]["flagged"] is True
    assert {(hit["source"], hit["pattern"]) for hit in access["hits"]} == {
        ("command", "cases_root")
    }


def test_a_rule_file_denies_a_tool_without_a_card(contract):
    """The arm's rule file denies ``web_fetch``. The call is refused by
    rule, which shows no card, and the run goes on to complete.
    """
    done = contract.done("web")
    command = json.loads((contract.meta("web") / "command.json").read_text())
    assert command["harness_env"]["OMICSCLAW_PERMISSION_RULES"].endswith("rules.json")
    assert "web_fetch" not in contract.audited_tools("web")
    assert done["approvals"]["required"] == 0
    assert done["outcome"] == "completed"


def test_the_wall_clock_stops_a_run_and_the_command_it_was_running(contract):
    """The agent is inside a ``sleep 120`` when the budget runs out. The
    process group is asked to stop, exits ``143``, and takes its shell
    command with it; nothing is left for the sweep to kill.
    """
    done = contract.done("sleepy")

    assert done["timed_out"] is True and done["exit_code"] == 143
    assert done["killed"] is False and done["strays_killed"] == 0
    assert done["outcome"] == "timeout"
    assert done["usage"]["llm_calls"] == 1
    assert WALL_CLOCK_S <= done["wall_s"] < WALL_CLOCK_S + 10


def test_a_backend_that_never_answers_is_infrastructure_not_a_timeout(contract):
    done = contract.done("hang")

    assert done["timed_out"] is True
    assert done["usage"]["llm_calls"] == 1 and done["usage"]["llm_cancelled"] == 1
    assert done["outcome"] == "infra_failure"
    assert done["reason"] == "timeout_before_any_model_response"


def test_grading_real_runs_skips_the_infrastructure_failures(contract):
    """The health check reads each real run's evidence again and agrees
    with what was recorded; the three infrastructure failures get a row
    and no grade.
    """
    summary = grade_campaign(contract.manifest, contract.campaign)

    rows = {row["model_id"]: row for row in read_jsonl(contract.campaign.grades)}
    assert summary.skipped == {"infra_failure": 3, "no_deliverable": 2}
    assert {name for name, row in rows.items() if row["skip"] == "infra_failure"} == {
        "subfail", "fail", "hang",
    }
    assert all(not row["health"] for row in rows.values())
    for graded in ("write", "subagent", "danger", "web"):
        assert rows[graded]["graded"] and rows[graded]["passed"] is True, graded
    assert rows["danger"]["outcome"] == "approval_denied"
    assert rows["danger"]["access_flagged"] is True
