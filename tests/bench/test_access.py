"""The access audit: what it reads, what it reports, and that it only reports."""

from __future__ import annotations

import os
from pathlib import Path

from omicsclaw.bench.access import (
    MAX_FILE_BYTES,
    MAX_HITS,
    audit_access,
    campaign_patterns,
)
from omicsclaw.bench.outcome import Command


def layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create and return the cases root, the meta root and a workspace."""
    roots = (tmp_path / "cases", tmp_path / "out" / "meta", tmp_path / "ws")
    for directory in roots:
        directory.mkdir(parents=True)
    return roots


def audit(tmp_path, *, commands=(), log="", extra=(), unchanged=None, skip=()):
    """Audit the workspace :func:`layout` made under *tmp_path*."""
    cases, meta = tmp_path / "cases", tmp_path / "out" / "meta"
    workspace = tmp_path / "ws"
    audit_log = meta / "audit.jsonl"
    if log:
        audit_log.write_text(log)
    return audit_access(
        patterns=campaign_patterns(cases, meta, extra),
        commands=[Command(tool, text) for tool, text in commands],
        audit_log=audit_log,
        workspace=workspace,
        unchanged=unchanged or {},
        skip=skip,
    )


def test_a_clean_run_is_not_flagged(tmp_path):
    cases, meta, workspace = layout(tmp_path)
    (workspace / "analysis.py").write_text("print(sum([1, 2, 3]))\n")

    report = audit(tmp_path, commands=[("bash", "python analysis.py")])

    assert report["flagged"] is False and report["hits"] == []
    assert report["files_read"] == 1
    assert report["patterns"] == ["cases_root", "meta_root"]


def test_each_source_is_searched(tmp_path):
    """A command, a line of the audit log and a script in the workspace
    each name a place the run had no business in.
    """
    cases, meta, workspace = layout(tmp_path)
    (workspace / "peek.py").write_text(f"open('{cases}/c1/oracle/truth.json')\n")

    report = audit(
        tmp_path,
        commands=[("bash", f"ls {meta}/a/m/c1/r1")],
        log=f'{{"tool": "bash", "detail": "denied: {cases}"}}\n',
    )

    assert report["flagged"] is True and report["matches"] == 3
    found = {(hit["source"], hit["where"], hit["pattern"]) for hit in report["hits"]}
    assert found == {
        ("command", "bash", "meta_root"),
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


def test_the_cases_root_is_matched_under_its_real_path_too(tmp_path):
    cases, meta, workspace = layout(tmp_path)
    alias = tmp_path / "alias"
    os.symlink(cases, alias)
    patterns = campaign_patterns(alias, meta)

    report = audit_access(
        patterns=patterns,
        commands=[Command("bash", f"cat {cases}/c1/oracle/truth.json")],
        audit_log=meta / "none.jsonl",
        workspace=workspace,
        unchanged={},
    )

    assert report["flagged"] is True


def test_a_staged_file_is_read_only_once_the_run_changed_it(tmp_path):
    """The case's own input may mention anything. It is skipped while its
    size and modification time are the staged ones.
    """
    cases, meta, workspace = layout(tmp_path)
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
    cases, meta, workspace = layout(tmp_path)
    (workspace / ".state").mkdir()
    (workspace / ".state" / "memory.db").write_text(str(cases))
    (workspace / "big.txt").write_text(str(cases) + "x" * MAX_FILE_BYTES)
    (workspace / "matrix.bin").write_bytes(b"\0\1" + str(cases).encode())

    report = audit(tmp_path, skip=(".state",))

    assert report["flagged"] is False
    assert (report["files_read"], report["files_skipped"]) == (0, 2)


def test_the_report_is_capped_and_says_so(tmp_path):
    cases, meta, workspace = layout(tmp_path)
    (workspace / "many.txt").write_text(f"{cases}\n" * (MAX_HITS + 5))

    report = audit(tmp_path)

    assert report["matches"] == MAX_HITS + 5
    assert len(report["hits"]) == MAX_HITS and report["truncated"] is True


def test_the_audit_changes_nothing(tmp_path):
    cases, meta, workspace = layout(tmp_path)
    script = workspace / "peek.py"
    script.write_text(f"open('{cases}')\n")
    before = (script.read_text(), script.stat().st_mtime_ns)

    audit(tmp_path)

    assert (script.read_text(), script.stat().st_mtime_ns) == before
    assert sorted(path.name for path in workspace.iterdir()) == ["peek.py"]
