"""planning: plan_write, the injected plan block, the planning gate."""

from __future__ import annotations

import pytest

from omicsclaw.evals import NoError, ScriptedProvider, ScriptedTurn, ToolCalled, tool_call
from omicsclaw.planning.injector import PLANNING_GATE_TEXT
from omicsclaw.planning.render import INJECTION_HEADER

from ._checks import CountIs, SentContains, occurrences
from ._harness import check, seed


def _steps(*statuses: str) -> dict:
    names = ("Load the AnnData", "Run QC and clustering", "Write the report")
    return {
        "steps": [
            {"id": f"s{i + 1}", "content": content, "status": status}
            for i, (content, status) in enumerate(zip(names, statuses))
        ]
    }


def _plan_then_execute():
    return ScriptedProvider(
        ScriptedTurn(tool_calls=(tool_call("plan_write", _steps("in_progress", "pending", "pending")),)),
        ScriptedTurn(tool_calls=(tool_call("plan_write", _steps("completed", "in_progress", "pending")),)),
        ScriptedTurn(tool_calls=(tool_call("plan_write", _steps("completed", "completed", "completed")),)),
        ScriptedTurn(text="All three steps are done."),
    )


def _read_only_exploration():
    return ScriptedProvider(
        ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "a.txt"}),)),
        ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "b.txt"}),)),
        ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "a.txt"}),)),
        ScriptedTurn(text="Explored."),
    )


def _three_short_answers():
    return ScriptedProvider(
        ScriptedTurn(text="2"),
        ScriptedTurn(text="5"),
        ScriptedTurn(text="Paris"),
    )


def _plan_files(result) -> int:
    plans = result.workspace / ".omicsclaw" / "plans"
    return sum(1 for path in plans.rglob("*") if path.is_file()) if plans.is_dir() else 0


def _gate_texts_sent(result) -> int:
    return sum(
        occurrences(result, PLANNING_GATE_TEXT, call=call)
        for call in range(len(result.provider_calls))
    )


CASES = [
    seed(
        "planning/plan_then_execute",
        "Preprocess and cluster this dataset, then write a report.",
        _plan_then_execute,
        ToolCalled("plan_write", min_times=2),
        SentContains(INJECTION_HEADER, call=1),
        SentContains("Run QC and clustering", call=2),
        CountIs("plan files under .omicsclaw/plans", _plan_files, 1, at_least=True),
        NoError(),
    ),
    seed(
        "planning/gate_nudges_read_only_exploration",
        "Look around the workspace.",
        _read_only_exploration,
        SentContains(PLANNING_GATE_TEXT, call=2, times=1),
        CountIs("gate text on call 3", lambda r: occurrences(r, PLANNING_GATE_TEXT, call=3), 0),
        CountIs("gate text on call 1", lambda r: occurrences(r, PLANNING_GATE_TEXT, call=1), 0),
        files={"a.txt": "alpha\n", "b.txt": "beta\n"},
        config={"planning_gate_turns": 2},
    ),
    seed(
        "planning/gate_ignores_earlier_exchanges",
        "What is 1+1? Answer with the number only.",
        _three_short_answers,
        CountIs("model calls", lambda r: len(r.provider_calls), 3),
        CountIs("gate text on any call", _gate_texts_sent, 0),
        followups=("And 2+3?", "What is the capital of France?"),
        config={"planning_gate_turns": 2},
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case(case, tmp_path, eval_results):
    check(case, tmp_path, eval_results)
