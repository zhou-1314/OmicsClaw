"""Run-dir layout schema — the single source of truth (ADR 0032).

These lock the deepening: the eager set, the completion contract, and the path
accessors all derive from one declaration, so the workspace layer and the
artifact contract cannot drift apart again (the clutter bug, where required dirs
the engine never wrote shipped empty).
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.autonomous import run_layout
from omicsclaw.autonomous.runner import autonomous_requirements


def test_eager_dirs_are_eager_directory_entries():
    eager = set(run_layout.eager_dirs())
    assert eager == {"inputs", "upstream"}
    for entry in run_layout.ENTRIES:
        if entry.relpath in eager:
            assert entry.kind is run_layout.Kind.DIR
            assert entry.lifecycle is run_layout.Lifecycle.EAGER


def test_contract_excludes_sentinel_provenance_and_rerun():
    contract = {entry.key for entry in run_layout.contract_entries()}
    assert "answer" not in contract  # sentinel
    assert "skill_calls_log" not in contract  # provenance
    assert "rerun" not in contract  # transient re-run dir
    assert {
        "result_summary",
        "analysis",
        "figures",
        "tables",
        "skill_calls",
        "inputs",
        "upstream",
    } <= contract


def test_completion_contract_is_derived_from_the_schema_not_hand_maintained():
    """The drift guard: the artifact requirements MUST equal the schema's
    deliverable entries, so create_workspace's eager set and the completion
    contract can never disagree (the RC2 clutter bug)."""
    requirement_paths = [r.path for r in autonomous_requirements()]
    schema_paths = [entry.relpath for entry in run_layout.contract_entries()]
    assert requirement_paths == schema_paths


def test_no_directory_is_required_so_no_empty_placeholder_can_ship():
    for entry in run_layout.ENTRIES:
        if entry.kind is run_layout.Kind.DIR:
            assert entry.required is False, f"required dir {entry.relpath} would ship empty"


def test_runpaths_accessors_resolve_under_the_root(tmp_path: Path):
    paths = run_layout.RunPaths(tmp_path)
    assert paths.answer == tmp_path / "_oc_answer.txt"
    assert paths.analysis == tmp_path / "analysis.py"
    assert paths.skill_calls == tmp_path / "skill_calls"
    assert paths.result_summary == tmp_path / "result_summary.md"
    assert paths.rerun == tmp_path / "rerun"
    assert paths.inputs == tmp_path / "inputs"
