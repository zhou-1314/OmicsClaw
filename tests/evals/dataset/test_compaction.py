"""compaction: offloading at WARN, summarizing at FULL, truncating at SOFT.

Every case sizes the window with a ``Headroom``. The ``trigger_tokens``
values are the growth the token estimator reports for these scripts,
rounded down a little so the trigger call lands inside the target tier
and no compaction writes back before the trigger call. If the estimator
changes, the compaction records' ``pressure`` shows which way the tier
moved, and the Runner's ``headroom_missed`` names a compaction that came
too early.

``summary_replaces_head`` reaches ``FULL`` by reading two tables in one
turn after three small reads. The first call's baseline is about 10k
tokens (the full skill index), so ``FULL`` needs more than a third of
that in growth; one table (about 2k tokens) is not enough.

``degraded_truncation_keeps_requests_and_recent_rounds`` has no
``side_replies``, so the scripted provider answers the summarizer with
an empty string. That is what the real summarizer returns after its
timeout, and the compaction falls back to truncation and writes nothing
back. The six panel files are read once each and stay under the offload
threshold, so the head still holds them when ``SOFT`` arrives on call 14
and the truncation has to choose which to drop. The growth to call 13
is 6,032 tokens and to call 14 is 6,214.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from omicsclaw.context import (
    COMPACTION_MARKER,
    MISSING_TOOL_RESULT,
    OFFLOAD_MARKER,
    Pressure,
    offload_key,
)
from omicsclaw.evals import Headroom, NoError, ScriptedProvider, ScriptedTurn, ToolCalled, tool_call
from omicsclaw.tools._workspace import Workspace
from omicsclaw.tools.builtin.read import read_tool

from ._checks import CountIs, SentContains, occurrences
from ._harness import check, seed


def _table(tag: str, rows: int = 400) -> str:
    header = "gene\t" + "\t".join(f"sample_{j}" for j in range(1, 8))
    body = [
        f"{tag}{i:04d}\t" + "\t".join(f"{(i * 7 + j * 13 + len(tag)) % 1000 / 10:.1f}" for j in range(1, 8))
        for i in range(rows)
    ]
    return "\n".join([header, *body]) + "\n"


BIG_TABLE = _table("GENE")
SMALL_NOTE = "".join(f"note {k}: clustering at resolution 1.0 kept 12 clusters.\n" for k in range(12))


def _offload_reference(path: str, content: str, call_id: str) -> str:
    """Where the offloader will keep *call_id*'s ``read_file`` result for *path*."""
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / path
        target.parent.mkdir(parents=True)
        target.write_text(content, encoding="utf-8")
        output = asyncio.run(read_tool(Workspace(Path(scratch))).execute(json.dumps({"path": path})))
    return f".omicsclaw/tool_results/eval/{offload_key(call_id, output)}.txt"


OFFLOADED = _offload_reference("data/big_table.tsv", BIG_TABLE, "call_1")

SUMMARY = (
    "### User Intent\nReview two expression tables.\n\n"
    "### Execution Progress\nRead small.txt three times, then data/a.tsv and data/b.tsv.\n\n"
    "### Next Steps\nCompare the two tables.\n\n"
    "## Summary\nThe user asked for a review of two tables; both were read (SUMMARY-SEVEN)."
)


def _large_result_offloaded():
    return ScriptedProvider(
        ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "data/big_table.tsv"}),)),
        *(ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "small.txt"}),)) for _ in range(3)),
        ScriptedTurn(tool_calls=(tool_call("read_file", {"path": OFFLOADED}),)),
        ScriptedTurn(text="The table has 400 genes."),
    )


def _summary_replaces_head():
    return ScriptedProvider(
        *(ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "small.txt"}),)) for _ in range(3)),
        ScriptedTurn(
            tool_calls=(
                tool_call("read_file", {"path": "data/a.tsv"}),
                tool_call("read_file", {"path": "data/b.tsv"}),
            )
        ),
        ScriptedTurn(text="Both tables reviewed."),
        side_replies=(SUMMARY,),
    )


FIRST_REQUEST = "Read the six panel files under notes/ and tell me whether the panels passed QC. FIRST-REQUEST"
SECOND_REQUEST = "Now read small.txt and tell me how many clusters it reports. SECOND-REQUEST"


def _panel(k: int) -> str:
    """A panel file of about 800 tokens whose first line carries its own mark."""
    lines = [f"MARK-m{k}: marker gene panel {k}"]
    lines += [f"row {r:03d}: panel {k} gene set for cluster {r % 12} passed QC at threshold 0.25." for r in range(46)]
    return "\n".join(lines) + "\n"


def _two_exchanges():
    return ScriptedProvider(
        *(ScriptedTurn(tool_calls=(tool_call("read_file", {"path": f"notes/m{k}.txt"}),)) for k in range(1, 7)),
        ScriptedTurn(text="All six panels passed QC."),
        *(ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "small.txt"}),)) for _ in range(7)),
        ScriptedTurn(text="It reports 12 clusters."),
    )


def _degraded_soft(result) -> int:
    """SOFT compactions that fell back to truncation and were not written back."""
    return sum(
        1
        for record in result.compactions
        if record.pressure is Pressure.SOFT and record.degraded and not record.written_back
    )


def _at(pressure: Pressure, *, written_back: bool = True):
    return lambda r: sum(
        1 for record in r.compactions if record.pressure is pressure and record.written_back is written_back
    )


def _offloaded(result) -> int:
    """Written-back compactions that offloaded something.

    A record's ``pressure`` is the tier measured after offloading, so a
    compaction that only offloads records ``NONE`` even though ``WARN``
    triggered it.
    """
    return sum(1 for record in result.compactions if record.written_back and record.offloaded)


def _table_reads(result) -> int:
    """``read_file`` results carrying table rows: the first read and the retrieval."""
    return sum(1 for item in result.tool_results if "GENE0050\t" in item.output)


CASES = [
    seed(
        "compaction/large_result_offloaded",
        "Read data/big_table.tsv and the notes.",
        _large_result_offloaded,
        SentContains(OFFLOAD_MARKER, call=4),
        SentContains(OFFLOADED, call=4),
        CountIs("offloading compactions written back", _offloaded, 1, at_least=True),
        CountIs("placeholders before call 4", lambda r: sum(
            OFFLOAD_MARKER in m.content for c in r.provider_calls[:4] for m in c.messages), 0),
        ToolCalled("read_file", min_times=5),
        CountIs("read_file results showing the table", _table_reads, 2),
        NoError(),
        compaction=True,
        headroom=Headroom(target=Pressure.WARN, trigger_call=4, trigger_tokens=2600),
        files={"data/big_table.tsv": BIG_TABLE, "small.txt": SMALL_NOTE},
        config={"memory": False},
    ),
    seed(
        "compaction/summary_replaces_head",
        "Review data/a.tsv and data/b.tsv.",
        _summary_replaces_head,
        SentContains((COMPACTION_MARKER, "## Summary", "SUMMARY-SEVEN"), call=4),
        CountIs("system message first on call 4", lambda r: int(r.provider_calls[4].messages[0].role == "system"), 1),
        CountIs("FULL compactions written back", _at(Pressure.FULL), 1),
        CountIs("summarizer calls", lambda r: len(r.side_calls), 1),
        NoError(),
        compaction=True,
        headroom=Headroom(target=Pressure.FULL, trigger_call=4, trigger_tokens=4700),
        files={"data/a.tsv": _table("ALPHA"), "data/b.tsv": _table("BETA"), "small.txt": SMALL_NOTE},
        config={"memory": False},
    ),
    seed(
        "compaction/degraded_truncation_keeps_requests_and_recent_rounds",
        FIRST_REQUEST,
        _two_exchanges,
        SentContains(SECOND_REQUEST, call=14, role="user"),
        SentContains(FIRST_REQUEST, call=14, role="user"),
        SentContains("MARK-m6", call=14, times=1),
        CountIs("the oldest panel on call 14", lambda r: occurrences(r, "MARK-m1", call=14), 0),
        CountIs("placeholder results on call 14", lambda r: occurrences(r, MISSING_TOOL_RESULT, call=14), 0),
        CountIs("system message first on call 14", lambda r: int(r.provider_calls[14].messages[0].role == "system"), 1),
        CountIs("degraded SOFT compactions, not written back", _degraded_soft, 1, at_least=True),
        NoError(),
        followups=(SECOND_REQUEST,),
        compaction=True,
        headroom=Headroom(target=Pressure.SOFT, trigger_call=14, trigger_tokens=6150),
        files={**{f"notes/m{k}.txt": _panel(k) for k in range(1, 7)}, "small.txt": SMALL_NOTE},
        config={"memory": False},
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case(case, tmp_path, eval_results):
    check(case, tmp_path, eval_results)
