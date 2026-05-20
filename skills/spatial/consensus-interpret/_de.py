"""Inline per-cluster differential expression.

Wraps ``scanpy.tl.rank_genes_groups`` so Slice 4 (marker → cell-type
candidate ranking) gets a tidy DataFrame to consume. Small clusters
(< ``min_cells_per_cluster``) are flagged ``de_unavailable`` (T2 per
ADR 0012 §"Failure semantics") rather than crashing; downstream
coverage check escalates to T1 exit 8 if too many clusters degrade.
"""

from __future__ import annotations

import logging

import anndata as ad
import numpy as np
import pandas as pd
import scanpy as sc

logger = logging.getLogger("consensus-interpret.de")

_DEFAULT_TOP_K = 20
_DEFAULT_MIN_CELLS = 3


def per_cluster_de(
    adata: ad.AnnData,
    consensus_labels: pd.DataFrame,
    *,
    consensus_label_column: str,
    top_k: int = _DEFAULT_TOP_K,
    min_cells_per_cluster: int = _DEFAULT_MIN_CELLS,
    de_method: str = "wilcoxon",
) -> tuple[pd.DataFrame, dict[int, str]]:
    """Compute top-K markers per consensus cluster.

    Parameters
    ----------
    adata
        The input adata used to produce the typed consensus run.
    consensus_labels
        DataFrame with ``observation`` and ``consensus_label_column``
        columns. observations must be a subset of ``adata.obs.index``
        (already validated at Slice 1 load time).
    consensus_label_column
        e.g. ``"consensus_kmode"``.

    Returns
    -------
    de_df
        Long-form ``(cluster, rank, gene, score, pval_adj)``. ranks are
        1-indexed; cluster ids match the labels' integer values.
    status
        ``cluster_id -> "ok"`` or ``"de_unavailable: <reason>"``. A
        T2 signal — coverage check in Slice 8 escalates to T1 if too
        many clusters degrade.
    """
    if consensus_label_column not in consensus_labels.columns:
        raise KeyError(
            f"consensus_labels missing column '{consensus_label_column}'"
        )

    obs_to_label: dict[str, int] = dict(
        zip(
            consensus_labels["observation"].astype(str),
            consensus_labels[consensus_label_column].astype(int),
        )
    )
    aligned_obs = [o for o in adata.obs.index.astype(str) if o in obs_to_label]
    sub = adata[aligned_obs, :].copy()
    sub.obs[consensus_label_column] = [obs_to_label[o] for o in aligned_obs]
    sub.obs[consensus_label_column] = sub.obs[consensus_label_column].astype("category")

    # Per-cluster small-cluster degradation BEFORE calling rank_genes_groups
    # (scanpy would crash on a singleton cluster).
    cluster_sizes = sub.obs[consensus_label_column].value_counts().to_dict()
    status: dict[int, str] = {}
    valid_clusters: list[int] = []
    for cluster_id, size in cluster_sizes.items():
        cid = int(cluster_id)
        if size < min_cells_per_cluster:
            status[cid] = f"de_unavailable: cluster has {size} cells (< {min_cells_per_cluster})"
            logger.warning(
                "cluster %d has %d cells (< %d); skipping DE (T2 degrade)",
                cid, size, min_cells_per_cluster,
            )
        else:
            status[cid] = "ok"
            valid_clusters.append(cid)

    if not valid_clusters:
        return pd.DataFrame(columns=["cluster", "rank", "gene", "score", "pval_adj"]), status

    # Restrict to valid clusters
    keep_mask = sub.obs[consensus_label_column].isin(valid_clusters).values
    sub_de = sub[keep_mask].copy()
    sub_de.obs[consensus_label_column] = sub_de.obs[consensus_label_column].astype("category")

    sc.tl.rank_genes_groups(
        sub_de,
        groupby=consensus_label_column,
        method=de_method,
        n_genes=top_k,
    )

    rgg = sub_de.uns["rank_genes_groups"]
    rows: list[dict] = []
    for cluster_id in valid_clusters:
        key = str(cluster_id)
        try:
            genes = list(rgg["names"][key])
            scores = list(rgg["scores"][key])
            pvals = list(rgg.get("pvals_adj", rgg.get("pvals"))[key])
        except (KeyError, ValueError) as exc:
            status[cluster_id] = f"de_unavailable: scanpy did not emit results ({exc})"
            continue
        for rank, (gene, score, pval) in enumerate(zip(genes, scores, pvals), start=1):
            rows.append({
                "cluster": cluster_id,
                "rank": rank,
                "gene": str(gene),
                "score": float(score),
                "pval_adj": float(pval) if not np.isnan(pval) else np.nan,
            })

    return pd.DataFrame(rows), status
