"""Slice 3 — Inline per-cluster DE.

Wraps ``scanpy.tl.rank_genes_groups`` so Slice 4 candidate ranking gets
a tidy ``(cluster, rank, gene, score, pval_adj)`` frame to consume.
Small-cluster degradation (T2) is detected here, not downstream.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _make_3_cluster_adata(n_per_cluster: int = 30, n_genes: int = 50, seed: int = 0) -> ad.AnnData:
    """Synthetic adata where genes 0,1,2 are markers of cluster 0,1,2 respectively."""
    rng = np.random.default_rng(seed)
    n = 3 * n_per_cluster
    X = rng.poisson(0.5, size=(n, n_genes)).astype("float32")
    for cluster in range(3):
        start = cluster * n_per_cluster
        end = (cluster + 1) * n_per_cluster
        X[start:end, cluster] += rng.poisson(10, size=n_per_cluster)  # strong cluster-specific
    adata = ad.AnnData(
        X=X,
        obs=pd.DataFrame(index=[f"obs_{i}" for i in range(n)]),
        var=pd.DataFrame(index=[f"gene_{i}" for i in range(n_genes)]),
    )
    return adata


def _labels_for(adata: ad.AnnData, label_per_cluster: list[int]) -> pd.DataFrame:
    """Build a consensus_labels DataFrame with one label per cluster of cells."""
    n_per = len(adata) // len(label_per_cluster)
    labels: list[int] = []
    for L in label_per_cluster:
        labels.extend([L] * n_per)
    return pd.DataFrame({
        "observation": adata.obs.index.astype(str),
        "consensus_kmode": labels,
    })


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #

def test_per_cluster_de_returns_top_k_per_cluster() -> None:
    from _de import per_cluster_de  # type: ignore[import-not-found]

    adata = _make_3_cluster_adata()
    labels = _labels_for(adata, [0, 1, 2])

    de_df, status = per_cluster_de(
        adata, labels, consensus_label_column="consensus_kmode", top_k=5
    )

    # Schema
    assert set(de_df.columns) >= {"cluster", "rank", "gene", "score", "pval_adj"}

    # Each cluster gets top_k rows
    for c in (0, 1, 2):
        sub = de_df[de_df["cluster"] == c]
        assert len(sub) == 5
        assert (sub["rank"].values == np.arange(1, 6)).all()

    # All clusters status 'ok'
    assert status == {0: "ok", 1: "ok", 2: "ok"}


def test_per_cluster_de_marker_gene_is_rank_1() -> None:
    """For our synthetic data, gene_<cluster> is the strongest marker."""
    from _de import per_cluster_de  # type: ignore[import-not-found]

    adata = _make_3_cluster_adata()
    labels = _labels_for(adata, [0, 1, 2])

    de_df, _ = per_cluster_de(
        adata, labels, consensus_label_column="consensus_kmode", top_k=5
    )

    for c in (0, 1, 2):
        rank1 = de_df[(de_df["cluster"] == c) & (de_df["rank"] == 1)].iloc[0]
        assert rank1["gene"] == f"gene_{c}", \
            f"cluster {c} top marker expected gene_{c}, got {rank1['gene']!r}"


# --------------------------------------------------------------------------- #
# T2 degrade — small cluster                                                  #
# --------------------------------------------------------------------------- #

def test_per_cluster_de_flags_small_cluster_as_unavailable() -> None:
    """Clusters with < min_cells_per_cluster cells are marked
    'de_unavailable' in the status map and produce no rows in de_df."""
    from _de import per_cluster_de  # type: ignore[import-not-found]

    adata = _make_3_cluster_adata(n_per_cluster=10)
    labels = _labels_for(adata, [0, 1, 2])
    # Mutate labels so cluster 2 only has 2 cells
    labels.loc[labels["consensus_kmode"] == 2, "consensus_kmode"] = 0
    obs_indices = labels["observation"].iloc[:2].tolist()
    labels.loc[labels["observation"].isin(obs_indices), "consensus_kmode"] = 2

    de_df, status = per_cluster_de(
        adata, labels, consensus_label_column="consensus_kmode",
        top_k=5, min_cells_per_cluster=3,
    )

    assert status[2].startswith("de_unavailable")
    assert (de_df["cluster"] == 2).sum() == 0
    # Other clusters still good
    assert status[0] == "ok"
    assert status[1] == "ok"


def test_per_cluster_de_label_column_assertion() -> None:
    """Misspelled consensus_label_column → KeyError (config bug, not T2)."""
    from _de import per_cluster_de  # type: ignore[import-not-found]

    adata = _make_3_cluster_adata()
    labels = _labels_for(adata, [0, 1, 2])

    with pytest.raises(KeyError, match="consensus_typo"):
        per_cluster_de(adata, labels, consensus_label_column="consensus_typo")


def test_per_cluster_de_observation_alignment() -> None:
    """labels with a subset of adata.obs.index should align (no error;
    just DE on the subset)."""
    from _de import per_cluster_de  # type: ignore[import-not-found]

    adata = _make_3_cluster_adata(n_per_cluster=30)
    labels = _labels_for(adata, [0, 1, 2])
    # Drop the last 30 rows
    labels = labels.iloc[:60].reset_index(drop=True)

    de_df, status = per_cluster_de(
        adata, labels, consensus_label_column="consensus_kmode", top_k=3
    )

    # Cluster 2 dropped (was last 30 cells)
    assert 2 not in status or status[2].startswith("de_unavailable")
    assert status[0] == "ok"
    assert status[1] == "ok"
