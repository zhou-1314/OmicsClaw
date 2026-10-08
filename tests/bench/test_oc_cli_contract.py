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
    "hollow", "errhang", "peek",
)
WALL_CLOCK_S = 10
"""Budget of every scenario. Three of them run into it on purpose, so it
sets this module's duration. The ``sleepy`` scenario must get its first
model answer before it runs out; a process starts in well under a second
here, and the rest is room for a loaded CI runner."""

DEADLINE_S = 2
"""The command's own exchange deadline in the ``deadline`` arm."""

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

[[arms]]
id = "deadline"
adapter = "omicsclaw"
provider = "custom"
model = "stub-hang"
model_id = "hang"
env = {{ OMICSCLAW_TURN_TIMEOUT_S = "{deadline}" }}

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

    def meta(self, scenario: str, arm: str = "oc") -> Path:
        return self.campaign.meta / arm / scenario / "sum-a" / "r1"

    def workspace(self, scenario: str, arm: str = "oc") -> Path:
        return self.campaign.cells / arm / scenario / "sum-a" / "r1"

    def done(self, scenario: str, arm: str = "oc") -> dict:
        return json.loads((self.meta(scenario, arm) / "done.json").read_text())

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
        MANIFEST.format(
            wall=WALL_CLOCK_S, models=models, answer=ANSWER, deadline=DEADLINE_S
        )
    )
    campaign = Campaign(out=root / "out", cases=cases)
    summary = run_campaign(
        load_manifest(manifest_path),
        campaign,
        jobs=5,
        base_env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(shim),
            "BENCH_ORACLE": str(cases / "sum-a" / "oracle"),
        },
        report=lambda message: None,
    )
    assert summary.executed == len(SCENARIOS) + 1, summary
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
    assert done["model_resolved"] == "stub-write"
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


def test_looking_outside_the_workspace_is_recorded(contract):
    """The agent lists the directories above its workspace and reads an
    earlier attempt's answer by a relative path. No root is named, and the
    run completes and is graded like any other; the access audit marks it.
    """
    done = contract.done("peek")
    access = json.loads((contract.meta("peek") / "access_audit.json").read_text())

    assert done["outcome"] == "completed"
    assert done["access"]["flagged"] is True
    assert done["access"]["commands_scanned"] > 0
    counts = done["access"]["by_pattern"]
    assert counts["leaves_workspace"] > 0
    assert (counts["cases_root"], counts["out_root"]) == (0, 0)
    assert {(hit["source"], hit["pattern"]) for hit in access["hits"]} == {
        ("command", "leaves_workspace")
    }
    assert contract.done("write")["access"]["flagged"] is False


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


def test_an_empty_reply_is_infrastructure_though_the_command_converged(contract):
    """The backend returns an answer with no text and no tokens, which is
    what a ``200`` with an error body looks like once it has passed through
    the provider layer. The command takes it as the agent's last word and
    exits ``0``.
    """
    done = contract.done("hollow")

    assert (done["exit_code"], done["stop_reason"]) == (0, "converged")
    assert done["usage"]["llm_calls"] == 1 and done["usage"]["llm_errors"] == 0
    assert done["usage"]["calls_without_usage"] == 1
    assert (done["outcome"], done["reason"]) == (
        "infra_failure", "provider_error: incomplete_response (no usage reported)",
    )


def test_a_provider_error_followed_by_the_wall_clock_is_infrastructure(contract):
    """The second model call fails with a 500 and the command's retry never
    comes back, so the wall clock ends the run. The run was already broken
    by the failed call; it is not recorded as a timeout.
    """
    done = contract.done("errhang")

    assert done["timed_out"] is True and done["exit_code"] == 143
    assert (done["usage"]["llm_errors"], done["usage"]["llm_cancelled"]) == (1, 1)
    assert (done["outcome"], done["reason"]) == (
        "infra_failure", "provider_error: ProviderError status=500",
    )


def test_the_commands_own_deadline_with_no_answer_is_infrastructure(contract):
    """``OMICSCLAW_TURN_TIMEOUT_S`` ends an exchange whose backend never
    answered. The command exits ``1`` long before the wall clock, and the
    run is recorded the way the wall clock would have recorded it.
    """
    done = contract.done("hang", arm="deadline")
    transcript = (contract.meta("hang", arm="deadline") / "stdout.txt").read_text()

    assert done["exit_code"] == 1 and "Failed: TimeoutError" in transcript
    assert done["timed_out"] is False and done["wall_s"] < WALL_CLOCK_S
    assert (done["outcome"], done["reason"]) == (
        "infra_failure", "timeout_before_any_model_response",
    )


def test_real_runs_can_be_graded_from_another_checkout(contract, tmp_path):
    """The same campaign, graded by an adapter that believes the source
    tree is somewhere else, as it would after the results were copied to
    another machine. Every run is still healthy, because each is checked
    against the source tree its own record names.
    """
    record = contract.done("write")["agent_code"]
    moved = tmp_path / "manifest.toml"
    text = contract.manifest.path.read_text().replace(
        'permission_rules = "rules.json"',
        f'options = {{ source_root = "{tmp_path}" }}',
    )
    moved.write_text(text)

    grade_campaign(load_manifest(moved), contract.campaign)

    rows = read_jsonl(contract.campaign.grades)
    assert record["source_root"] == str(REPO_ROOT)
    assert re.fullmatch(r"[0-9a-f]{40}", record["git_commit"])
    assert [row["run"] for row in rows if row["health"]] == []
    assert sum(row["graded"] for row in rows) == 5


def test_grading_real_runs_skips_the_infrastructure_failures(contract):
    """The health check reads each real run's evidence again and agrees
    with what was recorded; the infrastructure failures get a row and no
    grade.
    """
    summary = grade_campaign(contract.manifest, contract.campaign)

    rows = {
        row["run"].removesuffix("/sum-a/r1"): row
        for row in read_jsonl(contract.campaign.grades)
    }
    assert summary.skipped == {"infra_failure": 6, "no_deliverable": 2}
    assert {name for name, row in rows.items() if row["skip"] == "infra_failure"} == {
        "oc/subfail", "oc/fail", "oc/hang", "oc/hollow", "oc/errhang",
        "deadline/hang",
    }
    assert all(not row["health"] for row in rows.values())
    for graded in ("oc/write", "oc/subagent", "oc/danger", "oc/web", "oc/peek"):
        assert rows[graded]["graded"] and rows[graded]["passed"] is True, graded
    assert rows["oc/danger"]["outcome"] == "approval_denied"
    assert rows["oc/danger"]["access_flagged"] is True
