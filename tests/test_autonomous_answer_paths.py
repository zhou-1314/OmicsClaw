"""The mini-agent's answer must not send callers to paths that do not exist.

Diagnosis 2026-07-25. A real run finished successfully and reported:

    Outputs saved to <workspace>/output/synthetic_analysis/:
      - pca_clusters.png
      - top10_markers.csv

but its kernel is confined to the run workspace, so that sibling directory was
never written. The outer agent believed the answer, listed the path, got
"Directory not found", and retried it three times — burning the turn's whole
tool-iteration budget without ever reaching the real artifacts.

Nothing checked the answer's path claims against the filesystem. These tests
pin that check.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.autonomous.answer_paths import unresolved_answer_paths


def test_answer_claiming_a_nonexistent_directory_is_flagged(tmp_path: Path):
    ghost = tmp_path / "output" / "synthetic_analysis"
    answer = (
        "Analysis complete. KMeans clusters match ground truth with ARI = 0.988.\n"
        f"Outputs saved to {ghost}/:\n  - pca_clusters.png\n  - top10_markers.csv"
    )

    assert unresolved_answer_paths(answer, workspace_root=tmp_path) == [str(ghost)]


def test_answer_naming_real_artifacts_is_not_flagged(tmp_path: Path):
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "pca.png").write_bytes(b"x")
    answer = f"Done. See {figures / 'pca.png'} and the run workspace {tmp_path}."

    assert unresolved_answer_paths(answer, workspace_root=tmp_path) == []
