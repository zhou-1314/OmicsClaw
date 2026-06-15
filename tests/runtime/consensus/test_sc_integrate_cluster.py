"""Smoke test for the sc-integrate-cluster member skill (artifact contract).

Runs the skill as a real subprocess (the production fan-out path) for the
``none`` baseline — no external integration deps, fast — and asserts the
artifact schema the consensus reader + driver panel depend on. Harmony /
Scanorama / scVI backends are exercised by the end-to-end smoke, not here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from tests.runtime.consensus._sc_integration_synth import make_multibatch_adata

_SKILL = (
    Path(__file__).resolve().parents[3]
    / "skills/singlecell/scrna/sc-integrate-cluster/sc_integrate_cluster.py"
)


def test_none_baseline_emits_reader_and_panel_artifacts(tmp_path: Path) -> None:
    adata = make_multibatch_adata(n_per_group=20, n_batches=2, n_types=2, n_genes=100, seed=0)
    inp = tmp_path / "synth.h5ad"
    adata.write_h5ad(inp)
    out = tmp_path / "member_none"

    proc = subprocess.run(
        [sys.executable, str(_SKILL), "--input", str(inp), "--output", str(out),
         "--method", "none", "--batch-key", "batch", "--resolution", "1.0", "--seed", "0"],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, f"skill failed:\n{proc.stdout}\n{proc.stderr}"

    # Labels artifact the ScClusteringArtifactReader reads.
    points = pd.read_csv(out / "figure_data" / "embedding_points.csv")
    assert {"cell_id", "leiden"}.issubset(points.columns)
    assert len(points) == adata.n_obs

    # Summary the driver's k-report / panel keying reads.
    summary = pd.read_csv(out / "figure_data" / "clustering_summary.csv")
    rep = dict(zip(summary["metric"], summary["value"]))
    assert rep["representation_used"] == "X_pca"  # unintegrated baseline
    assert int(rep["n_clusters"]) >= 1
    assert int(rep["n_batches"]) == 2

    # Member processed.h5ad carries the embedding + X_pca baseline for the panel.
    import anndata as ad

    member_adata = ad.read_h5ad(out / "processed.h5ad")
    assert "X_pca" in member_adata.obsm
    assert "batch" in member_adata.obs.columns


@pytest.mark.parametrize("missing_batch", [True])
def test_integration_method_requires_batches(tmp_path: Path, missing_batch: bool) -> None:
    # A single-batch input must make a real integration method fail loudly
    # (the member is then dropped by fan-out, consensus proceeds on survivors).
    adata = make_multibatch_adata(n_per_group=20, n_batches=1, n_types=2, n_genes=100, seed=1)
    inp = tmp_path / "one_batch.h5ad"
    adata.write_h5ad(inp)
    out = tmp_path / "member_harmony"

    proc = subprocess.run(
        [sys.executable, str(_SKILL), "--input", str(inp), "--output", str(out),
         "--method", "harmony", "--batch-key", "batch", "--seed", "0"],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode != 0
    assert "batch" in (proc.stdout + proc.stderr).lower()
