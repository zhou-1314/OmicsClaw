"""The access audit: what it reads, what it reports, and that it only reports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omicsclaw.bench.access import (
    MAX_FILE_BYTES,
    MAX_HITS,
    audit_access,
    run_patterns,
)
from omicsclaw.bench.outcome import Command

KEY = "a/m/sum-a/r1"


def layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create and return a cases root, an output root and one workspace."""
    cases, out = tmp_path / "cases", tmp_path / "out"
    workspace = out / "cells" / KEY
    for directory in (cases, out / "meta" / KEY, workspace):
        directory.mkdir(parents=True)
    return cases, out, workspace


def audit(tmp_path, *, commands=(), log="", extra=(), unchanged=None, skip=()):
    """Audit the workspace :func:`layout` made under *tmp_path*."""
    cases, out = tmp_path / "cases", tmp_path / "out"
    workspace = out / "cells" / KEY
    audit_log = out / "meta" / KEY / "audit.jsonl"
    if log:
        audit_log.write_text(log)
    return audit_access(
        patterns=run_patterns(cases, out, workspace, extra),
        commands=(
            None
            if commands is None
            else [Command(tool, text) for tool, text in commands]
        ),
        audit_log=audit_log,
        workspace=workspace,
        unchanged=unchanged or {},
        skip=skip,
    )


def labels(report) -> set[str]:
    return {hit["pattern"] for hit in report["hits"]}


def test_a_clean_run_is_not_flagged(tmp_path):
    """Commands that stay in the workspace, by relative or absolute path,
    match nothing, although the workspace sits under the output root.
    """
    cases, out, workspace = layout(tmp_path)
    (workspace / "analysis.py").write_text("print(sum([1, 2, 3]))\n")

    report = audit(tmp_path, commands=[
        ("bash", "python analysis.py && cat data/../data/numbers.txt"),
        ("write_file", f'{{"path": "{workspace}/output/answer.json"}}'),
        ("bash", f"ls {workspace}"),
    ])

    assert report["flagged"] is False and report["hits"] == []
    assert report["files_read"] == 1 and report["commands_scanned"] == 3
    assert report["patterns"] == ["cases_root", "out_root", "leaves_workspace"]


def test_each_source_is_searched(tmp_path):
    """A command, a line of the audit log and a script in the workspace
    each name a place the run had no business in.
    """
    cases, out, workspace = layout(tmp_path)
    (workspace / "peek.py").write_text(f"open('{cases}/c1/oracle/truth.json')\n")

    report = audit(
        tmp_path,
        commands=[("bash", f"ls {out}/meta/{KEY}")],
        log=f'{{"tool": "bash", "detail": "denied: {cases}"}}\n',
    )

    assert report["flagged"] is True and report["matches"] == 3
    found = {(hit["source"], hit["where"], hit["pattern"]) for hit in report["hits"]}
    assert found == {
        ("command", "bash", "out_root"),
        ("audit_log", "line 1", "cases_root"),
        ("file", "peek.py", "cases_root"),
    }
    assert all(hit["excerpt"] for hit in report["hits"])


def test_patterns_from_the_manifest_are_added(tmp_path):
    layout(tmp_path)

    report = audit(
        tmp_path,
        commands=[("bash", "pip install scanpy"), ("bash", "echo pipeline")],
        extra=[r"\bpip3? install\b"],
    )

    assert report["matches"] == 1
    assert report["hits"][0]["pattern"] == r"\bpip3? install\b"


@pytest.mark.parametrize(
    "target",
    [
        "cells/b/m/sum-a/r1/output/answer.json",
        "cells/a/m/sum-a/r1.infra1/output/answer.json",
        "cells/a/m/sum-a/r10/output/answer.json",
        "grades.jsonl",
        "meta/a/m/sum-a/r1/done.json",
        "",
    ],
    ids=["other-arm", "earlier-attempt", "other-repeat", "grades", "meta", "root"],
)
def test_the_output_root_outside_the_own_workspace_is_matched(tmp_path, target):
    """Another arm's workspace, an earlier attempt kept beside this one,
    the grades file and the run records all live under the output root. A
    mention of any of them is recorded; the run's own workspace is the one
    place under that root it may name.
    """
    cases, out, workspace = layout(tmp_path)

    report = audit(tmp_path, commands=[("bash", f"cat {out}/{target}".rstrip("/"))])

    assert labels(report) == {"out_root"}


@pytest.mark.parametrize(
    "command",
    [
        "ls ..",
        "ls -la .. ../..",
        "cat ../r1.infra1/output/answer.json",
        "cat ../../../../b/m/sum-a/r1/output/answer.json",
        "cat ../../../../../../cases/sum-a/oracle/truth.json",
        "cat data/../../r2/output/answer.json",
        'python -c "open(\'../r2/output/answer.json\').read()"',
    ],
)
def test_a_relative_path_that_leaves_the_workspace_is_matched(tmp_path, command):
    """Commands run with the workspace as their directory, so ``..`` is the
    way out of it without naming any root: to the same case under another
    arm, to an earlier attempt, to the oracle.
    """
    layout(tmp_path)

    report = audit(tmp_path, commands=[("bash", command)])

    assert labels(report) == {"leaves_workspace"}


def test_an_absolute_path_that_climbs_out_of_the_workspace_is_matched(tmp_path):
    cases, out, workspace = layout(tmp_path)

    report = audit(tmp_path, commands=[("bash", f"cat {workspace}/../r1.infra1/x")])

    assert labels(report) == {"leaves_workspace"}


@pytest.mark.parametrize(
    "text",
    ["seq 1..5", "echo wait...", "cat data/../data/numbers.txt", "from ..pkg import x"],
)
def test_dots_that_are_not_a_way_out_are_not_matched(tmp_path, text):
    layout(tmp_path)

    assert audit(tmp_path, commands=[("bash", text)])["flagged"] is False


def test_a_path_in_a_file_is_judged_from_the_files_own_directory(tmp_path):
    """A report two directories down may link to ``../figures`` and stay
    inside the workspace; three levels up from there it does not.
    """
    cases, out, workspace = layout(tmp_path)
    nested = workspace / "results" / "01"
    nested.mkdir(parents=True)
    (nested / "inside.md").write_text("![plot](../figures/a.png)\n")
    (nested / "outside.md").write_text("see ../../../r2/output/answer.json\n")

    report = audit(tmp_path)

    assert [(hit["where"], hit["pattern"]) for hit in report["hits"]] == [
        ("results/01/outside.md", "leaves_workspace")
    ]


def test_sibling_roots_that_share_a_name_prefix_are_told_apart(tmp_path):
    """With cases in ``X/bench`` and results in ``X/bench-out``, the
    workspace path starts with the text of the cases root. It is another
    directory, and an agent naming its own deliverable is not flagged.
    """
    cases, out = tmp_path / "bench", tmp_path / "bench-out"
    workspace = out / "cells" / KEY
    cases.mkdir()
    workspace.mkdir(parents=True)
    patterns = run_patterns(cases, out, workspace)

    def flagged(text: str) -> set[str]:
        report = audit_access(
            patterns=patterns,
            commands=[Command("bash", text)],
            audit_log=tmp_path / "none.jsonl",
            workspace=workspace,
            unchanged={},
        )
        return labels(report)

    assert flagged(f"cat {workspace}/output/answer.json") == set()
    assert flagged(f"cat {cases}/sum-a/oracle/truth.json") == {"cases_root"}
    assert flagged(f"ls {cases}") == {"cases_root"}
    assert flagged(f"ls {out}") == {"out_root"}


def test_the_cases_root_is_matched_under_its_real_path_too(tmp_path):
    cases, out, workspace = layout(tmp_path)
    alias = tmp_path / "alias"
    os.symlink(cases, alias)
    patterns = run_patterns(alias, out, workspace)

    report = audit_access(
        patterns=patterns,
        commands=[Command("bash", f"cat {cases}/c1/oracle/truth.json")],
        audit_log=tmp_path / "none.jsonl",
        workspace=workspace,
        unchanged={},
    )

    assert report["flagged"] is True


def test_having_no_commands_to_scan_is_recorded_as_such(tmp_path):
    """An adapter that could not recover the tool calls says so with
    ``None``. The report then reads "not scanned", which is a different
    thing from a run that made no call or a run that was clean.
    """
    layout(tmp_path)

    unknown = audit(tmp_path, commands=None)
    none_made = audit(tmp_path, commands=())

    assert unknown["commands_scanned"] is None and unknown["flagged"] is False
    assert none_made["commands_scanned"] == 0


def test_a_staged_file_is_read_only_once_the_run_changed_it(tmp_path):
    """The case's own input may mention anything. It is skipped while its
    size and modification time are the staged ones.
    """
    cases, out, workspace = layout(tmp_path)
    staged = workspace / "README.txt"
    staged.write_text(f"The data came from {cases}.\n")
    status = staged.stat()
    unchanged = {"README.txt": (status.st_size, status.st_mtime_ns)}

    before = audit(tmp_path, unchanged=unchanged)
    staged.write_text(f"The data came from {cases}. Edited.\n")
    after = audit(tmp_path, unchanged=unchanged)

    assert before["flagged"] is False and before["files_read"] == 0
    assert after["flagged"] is True and after["files_read"] == 1


def test_the_agents_own_state_large_files_and_binaries_are_not_read(tmp_path):
    cases, out, workspace = layout(tmp_path)
    (workspace / ".state").mkdir()
    (workspace / ".state" / "memory.db").write_text(str(cases))
    (workspace / "big.txt").write_text(str(cases) + "x" * MAX_FILE_BYTES)
    (workspace / "matrix.bin").write_bytes(b"\0\1" + str(cases).encode())

    report = audit(tmp_path, skip=(".state",))

    assert report["flagged"] is False
    assert (report["files_read"], report["files_skipped"]) == (0, 2)


def test_the_report_is_capped_and_says_so(tmp_path):
    cases, out, workspace = layout(tmp_path)
    (workspace / "many.txt").write_text(f"{cases}\n" * (MAX_HITS + 5))

    report = audit(tmp_path)

    assert report["matches"] == MAX_HITS + 5
    assert len(report["hits"]) == MAX_HITS and report["truncated"] is True


def test_the_audit_changes_nothing(tmp_path):
    cases, out, workspace = layout(tmp_path)
    script = workspace / "peek.py"
    script.write_text(f"open('{cases}')\n")
    before = (script.read_text(), script.stat().st_mtime_ns)

    audit(tmp_path)

    assert (script.read_text(), script.stat().st_mtime_ns) == before
    assert sorted(path.name for path in workspace.iterdir()) == ["peek.py"]
