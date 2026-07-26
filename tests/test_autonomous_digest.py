"""Regression tests for the autonomous-run result digest (diagnose 2026-06-26).

`autonomous_analysis_execute` used to return only file paths, so the outer agent
satisfied the ADR-0014 "judge the produced artifacts" directive by reading
``completion_report.json`` / ``result_summary.md`` and globbing figures across
~10 turns — a token-heavy "verification storm". The executor now returns a
compact, inline digest of the content the run already produced. These tests pin
that the digest:

- inlines the computed results / answer the model needs to verify,
- lists produced artifacts (so no glob is needed),
- still surfaces the raw paths for optional deep-dive,
- stays under the tool-result inline byte threshold (RESULT_POLICY_SUMMARY_OR_MEDIA
  spills over ~5000 bytes), so the digest itself is not spilled to disk and
  re-fetched — which would re-create the storm.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# agent_executors and agent.state have a pre-existing import cycle
# (state.py imports _available_tool_executors; agent_executors imports state).
# It is normally resolved by load order; importing state first lets this module
# import agent_executors cold without tripping the partial-init ImportError.
import omicsclaw.runtime.agent.state  # noqa: F401  (resolve import cycle)
from omicsclaw.runtime.tools.builders.agent_executors import (
    _AUTONOMOUS_DIGEST_MAX_BYTES,
    _autonomous_artifact_inventory,
    _format_autonomous_digest,
)


def _attempt(index=1, status="succeeded", tier="standard", exit_code=0):
    return SimpleNamespace(
        attempt_index=index,
        status=SimpleNamespace(value=status),
        permission_tier=SimpleNamespace(value=tier),
        exit_code=exit_code,
    )


def _result(**overrides):
    base = dict(
        ok=True,
        run_id="run-1",
        workspace_root="/nonexistent/run",
        manifest_path="/nonexistent/run/manifest.json",
        completion_report_path="/nonexistent/run/completion_report.json",
        error="",
        metadata={
            "computed_results": "median genes/spot=812; median UMI=1043; pct_mito=4.2%",
            "answer": "QC healthy; 8,123 spots retained after filtering.",
            "interpretive_notes": "QC healthy; 8,123 spots retained after filtering.",
        },
        attempts=[_attempt()],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_digest_inlines_results_so_no_file_read_is_needed():
    digest = _format_autonomous_digest(_result())
    # The verification-relevant content is inline — no need to open the report.
    assert "median UMI=1043" in digest
    assert "QC healthy" in digest
    assert "Computed results" in digest
    assert "completed" in digest and "run-1" in digest


def test_digest_still_exposes_raw_paths_for_deep_dive():
    digest = _format_autonomous_digest(_result())
    assert "/nonexistent/run/completion_report.json" in digest
    assert "/nonexistent/run/manifest.json" in digest


def test_digest_corrects_answer_paths_that_do_not_exist():
    """The 2026-07-25 storm: the answer named an output directory the run never
    wrote, and the outer agent listed it three times before the turn hit the
    tool-iteration cap. The digest must contradict the answer in the same
    breath, and point at the directory that really holds the artifacts."""
    digest = _format_autonomous_digest(
        _result(
            metadata={
                "computed_results": "ARI = 0.988",
                "answer": "Outputs saved to /workspace/output/synthetic_analysis/",
                "interpretive_notes": "",
                "unresolved_answer_paths": ["/workspace/output/synthetic_analysis"],
            }
        )
    )
    assert "/workspace/output/synthetic_analysis" in digest
    assert "do not exist" in digest.lower()
    # It must name the authoritative location so the correction is actionable.
    assert "/nonexistent/run" in digest


def test_digest_has_no_correction_when_answer_paths_all_resolve():
    digest = _format_autonomous_digest(
        _result(metadata={**_result().metadata, "unresolved_answer_paths": []})
    )
    assert "do not exist" not in digest.lower()


def test_success_digest_treats_replay_and_artifact_inventory_as_authoritative(
    tmp_path,
):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "pca.png").write_text("x")
    result = _result(workspace_root=str(tmp_path))

    digest = _format_autonomous_digest(result)

    assert "output/pca.png" in digest
    assert "Replay validation and the artifact inventory are authoritative" in digest
    assert "Do not list, glob, or re-read" in digest
    assert "batch-update" in digest


def test_success_digest_discloses_truncated_artifact_inventory(tmp_path):
    for index in range(43):
        (tmp_path / f"artifact_{index:02}.csv").write_text("x", encoding="utf-8")

    digest = _format_autonomous_digest(
        _result(workspace_root=str(tmp_path))
    )

    assert "showing 40 of 43" in digest
    assert "truncated" in digest.lower()
    assert "Replay validation and the artifact count are authoritative" in digest
    assert "Replay validation and the artifact inventory are authoritative" not in digest


def test_artifact_inventory_streams_large_trees_without_path_rglob(
    tmp_path,
    monkeypatch,
):
    for index in range(75):
        directory = tmp_path / f"group_{index % 5}"
        directory.mkdir(exist_ok=True)
        (directory / f"artifact_{index:03}.csv").write_text("x", encoding="utf-8")

    def reject_rglob(self, pattern):
        raise AssertionError("artifact inventory must not materialize Path.rglob")

    monkeypatch.setattr(Path, "rglob", reject_rglob)

    inventory = _autonomous_artifact_inventory(str(tmp_path), limit=40)

    assert inventory.complete is True
    assert inventory.scan_error is None
    assert inventory.total == 75
    assert inventory.truncated is True
    assert len(inventory.paths) == 40
    assert inventory.paths == tuple(sorted(inventory.paths))


def test_artifact_inventory_marks_partial_walk_failure(tmp_path, monkeypatch):
    (tmp_path / "partial.csv").write_text("x", encoding="utf-8")

    def partial_walk(root, *, topdown, onerror, followlinks):
        yield str(root), [], ["partial.csv"]
        onerror(PermissionError("synthetic unreadable directory"))

    monkeypatch.setattr("omicsclaw.autonomous.artifacts.os.walk", partial_walk)

    inventory = _autonomous_artifact_inventory(str(tmp_path))

    assert inventory.paths == ("partial.csv",)
    assert inventory.total == 1
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_scan_failed"


def test_artifact_inventory_does_not_descend_alias_directories(tmp_path):
    run = tmp_path / "run"
    outside = tmp_path / "outside"
    run.mkdir()
    outside.mkdir()
    (run / "owned.csv").write_text("x", encoding="utf-8")
    (outside / "escaped.csv").write_text("x", encoding="utf-8")
    try:
        (run / "alias").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    inventory = _autonomous_artifact_inventory(str(run))

    assert inventory.complete is True
    assert inventory.paths == ("owned.csv",)


def test_artifact_inventory_marks_alias_workspace_root_incomplete(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "owned.csv").write_text("x", encoding="utf-8")
    alias = tmp_path / "run-alias"
    try:
        alias.symlink_to(run, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    inventory = _autonomous_artifact_inventory(str(alias))

    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_alias_root"


def test_artifact_inventory_marks_alias_workspace_ancestor_incomplete(tmp_path):
    real_parent = tmp_path / "real-parent"
    run = real_parent / "run"
    run.mkdir(parents=True)
    (run / "owned.csv").write_text("x", encoding="utf-8")
    alias_parent = tmp_path / "alias-parent"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    inventory = _autonomous_artifact_inventory(str(alias_parent / "run"))

    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_alias_root"


def test_artifact_inventory_marks_windows_reparse_workspace_root_incomplete(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "run"
    root.mkdir()
    root_stat = root.lstat()
    root_identity = (root_stat.st_dev, root_stat.st_ino)

    monkeypatch.setattr(
        "omicsclaw.common.output_claim._is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino) == root_identity,
    )

    inventory = _autonomous_artifact_inventory(str(root))

    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_alias_root"


def test_artifact_inventory_marks_candidate_inspection_failure_incomplete(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "marker_genes.csv"
    candidate.write_text("x", encoding="utf-8")
    real_stat = Path.stat

    def deny_candidate_stat(path, *args, **kwargs):
        if path == candidate:
            raise PermissionError("synthetic candidate inspection denial")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_candidate_stat)

    inventory = _autonomous_artifact_inventory(str(tmp_path))

    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_scan_failed"


def test_artifact_inventory_excludes_candidate_when_claim_identity_stat_fails(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "marker_genes.csv"
    candidate.write_text("x", encoding="utf-8")
    real_stat = Path.stat
    candidate_stat_calls = 0

    def deny_claim_identity_stat(path, *args, **kwargs):
        nonlocal candidate_stat_calls
        if path == candidate:
            candidate_stat_calls += 1
            if candidate_stat_calls == 2:
                raise PermissionError("synthetic claim identity denial")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_claim_identity_stat)

    inventory = _autonomous_artifact_inventory(str(tmp_path))

    assert candidate_stat_calls == 2
    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_scan_failed"


def test_artifact_inventory_excludes_candidate_when_claim_resolve_fails(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "marker_genes.csv"
    candidate.write_text("x", encoding="utf-8")
    real_resolve = Path.resolve
    candidate_resolve_calls = 0

    def deny_claim_resolve(path, *args, **kwargs):
        nonlocal candidate_resolve_calls
        if path == candidate:
            candidate_resolve_calls += 1
            if candidate_resolve_calls == 1:
                raise PermissionError("synthetic claim resolve denial")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", deny_claim_resolve)

    inventory = _autonomous_artifact_inventory(str(tmp_path))

    assert candidate_resolve_calls == 1
    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_scan_failed"


@pytest.mark.parametrize("failure_call", [2, 3])
def test_artifact_inventory_excludes_transiently_missing_candidate(
    tmp_path,
    monkeypatch,
    failure_call,
):
    candidate = tmp_path / "marker_genes.csv"
    candidate.write_text("x", encoding="utf-8")
    real_lstat = os.lstat
    candidate_lstat_calls = 0

    def transient_candidate_missing(path, *args, **kwargs):
        nonlocal candidate_lstat_calls
        if Path(path) == candidate:
            candidate_lstat_calls += 1
            if candidate_lstat_calls == failure_call:
                raise FileNotFoundError("synthetic transient candidate disappearance")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", transient_candidate_missing)

    inventory = _autonomous_artifact_inventory(str(tmp_path))

    assert candidate_lstat_calls >= failure_call
    assert inventory.paths == ()
    assert inventory.total == 0
    assert inventory.complete is False
    assert inventory.scan_error == "filesystem_scan_failed"


def test_digest_tells_the_model_what_a_budget_stop_already_finished():
    digest = _format_autonomous_digest(
        _result(
            ok=False,
            error="mini-agent ran out of budget (step_budget_exhausted) ...",
            metadata={
                "computed_results": "- Steps: 12 (9 accepted)",
                "answer": "",
                "interpretive_notes": "",
                "partial_progress": {
                    "reason": "step_budget_exhausted",
                    "completed_steps": 9,
                    "completed_purposes": ["QC metrics", "normalize", "cluster"],
                    "salvageable_artifacts": ["partial.h5ad"],
                },
            },
        )
    )
    assert "9" in digest
    assert "cluster" in digest
    assert "partial.h5ad" in digest
    assert "incomplete" in digest.lower()
    assert "no live kernel" in digest.lower()
    assert "resume, do not restart" not in digest.lower()


def test_digest_stays_under_inline_threshold_even_when_huge():
    # A pathological, very large multibyte result must still fit inline so it is
    # not spilled to disk (which would force the model to re-fetch it).
    big = "数据" * 5000  # ~30 KB of CJK
    digest = _format_autonomous_digest(
        _result(
            metadata={"computed_results": big, "answer": big, "interpretive_notes": big}
        )
    )
    encoded = len(digest.encode("utf-8"))
    assert encoded <= _AUTONOMOUS_DIGEST_MAX_BYTES
    assert encoded < 5000  # strictly under the SUMMARY_OR_MEDIA inline threshold


def test_digest_omits_interpretive_notes_when_identical_to_answer():
    # interpretive_notes duplicates answer today; do not pay for it twice.
    digest = _format_autonomous_digest(_result())
    assert "Interpretive notes" not in digest


def test_digest_reports_failure_and_error():
    digest = _format_autonomous_digest(
        _result(ok=False, error="kernel died: OOM", metadata={})
    )
    assert "failed" in digest
    assert "kernel died" in digest


def test_autonomous_artifacts_lists_nested_outputs_and_skips_bookkeeping(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "figures").mkdir()
    (tmp_path / "output" / "pca_clusters.png").write_text("x")
    (tmp_path / "figures" / "fig_01.png").write_text("x")
    (tmp_path / "marker_genes.csv").write_text("x")
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "source.csv").write_text("x")
    (tmp_path / "completion_report.json").write_text("x")
    (tmp_path / "analysis.py").write_text("x")

    artifacts = _autonomous_artifact_inventory(str(tmp_path)).paths

    assert artifacts == (
        "figures/fig_01.png",
        "marker_genes.csv",
        "output/pca_clusters.png",
    )


def test_autonomous_artifacts_include_common_scientific_formats(tmp_path):
    for name in (
        "expression.loom",
        "matrix.npz",
        "model.rds",
        "table.parquet",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")

    assert _autonomous_artifact_inventory(str(tmp_path)).paths == (
        "expression.loom",
        "matrix.npz",
        "model.rds",
        "table.parquet",
    )


def test_autonomous_artifacts_handles_missing_dir():
    inventory = _autonomous_artifact_inventory("/nonexistent/path/xyz")

    assert inventory.paths == ()
    assert inventory.complete is False
    assert inventory.scan_error == "workspace_not_found"

    digest = _format_autonomous_digest(
        _result(workspace_root="/nonexistent/path/xyz")
    )
    assert "Artifact inventory scan is incomplete" in digest
    assert "artifact inventory are authoritative" not in digest
    assert "artifact count are authoritative" not in digest


def test_autonomous_artifacts_rejects_claim_aliases(tmp_path):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    (tmp_path / "figures").mkdir()
    claim = tmp_path / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (tmp_path / "claim.csv").hardlink_to(claim)
    (tmp_path / "figures" / "claim.png").hardlink_to(claim)

    assert _autonomous_artifact_inventory(str(tmp_path)).paths == ()
