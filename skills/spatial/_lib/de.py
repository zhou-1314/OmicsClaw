"""Spatial differential expression analysis.

Provides scanpy-based DE (wilcoxon / t-test) and pseudobulk PyDESeq2.

Includes marker gene filtering (min_in_group_fraction, min_fold_change,
max_out_group_fraction) from community best practices, and pseudobulk
design validation to prevent invalid analyses.

Usage::

    from skills.spatial._lib.de import run_de, run_pydeseq2, SUPPORTED_METHODS

    summary = run_de(adata, groupby="leiden", method="wilcoxon")
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import scanpy as sc

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("wilcoxon", "t-test", "pydeseq2")


def _get_raw_counts(adata) -> np.ndarray:
    """Extract raw integer counts, preferring adata.layers['counts'] or adata.raw."""
    if "counts" in adata.layers:
        logger.info("Using raw counts from adata.layers['counts']")
        X = adata.layers["counts"]
    elif adata.raw is not None:
        logger.info("Using raw counts from adata.raw.X")
        X = adata.raw.X
    else:
        logger.warning(
            "No 'counts' layer or .raw found. Falling back to adata.X. "
            "WARNING: Summing log-normalized values for pseudobulk is mathematically invalid. "
            "Please ensure adata.X contains raw counts if using PyDESeq2."
        )
        X = adata.X

    from scipy import sparse
    if sparse.issparse(X):
        X = X.toarray()
    X = np.asarray(X)

    if np.any(X < 0):
        raise ValueError("PyDESeq2 requires non-negative counts. Scaled data detected.")

    if X.dtype.kind == "f":
        sample_vals = X.ravel()[:1000]
        if np.allclose(sample_vals, np.round(sample_vals)):
            X = np.round(X).astype(int)
        else:
            logger.warning("Non-integer counts detected in extracted matrix. Rounding for PyDESeq2 (may be log-normalized data improperly supplied).")
            X = np.round(X).astype(int)

    return X


def run_de(
    adata, *, groupby: str = "leiden", method: str = "wilcoxon",
    n_top_genes: int = 10, group1: str | None = None, group2: str | None = None,
    filter_markers: bool = True,
    min_in_group_fraction: float = 0.25,
    min_fold_change: float = 1.0,
    max_out_group_fraction: float = 0.5,
) -> dict:
    """Run scanpy DE analysis with optional marker filtering.
    
    Note: Requires log-normalized expression data in adata.X (e.g., from sc.pp.normalize_total + sc.pp.log1p).

    Parameters
    ----------
    filter_markers : bool
        If True, apply ``sc.tl.filter_rank_genes_groups`` to remove
        non-specific markers (default True).
    min_in_group_fraction : float
        Gene must be expressed in at least this fraction of the group (0.25).
    min_fold_change : float
        Minimum natural log fold change (1.0).
    max_out_group_fraction : float
        Gene must be in less than this fraction of other groups (0.5).

    Returns
    -------
    dict
        Summary with markers_df (filtered) and full_df (unfiltered).
    """
    if groupby not in adata.obs.columns:
        raise ValueError(f"Groupby column '{groupby}' not found in adata.obs")

    n_cells, n_genes = adata.n_obs, adata.n_vars
    n_groups = adata.obs[groupby].nunique()
    groups_list = sorted(adata.obs[groupby].unique().tolist(), key=str)
    logger.info("Input: %d cells x %d genes, %d groups in '%s'", n_cells, n_genes, n_groups, groupby)

    # Validate log-normalized data for wilcoxon/t-test
    X_data = adata.X.data if hasattr(adata.X, "data") else np.ravel(adata.X)
    if len(X_data) > 0:
        sample_vals = X_data[:1000] if len(X_data) > 1000 else X_data
        is_integer = np.issubdtype(X_data.dtype, np.integer) or np.allclose(sample_vals, np.round(sample_vals))
        if is_integer and (X_data.max() > 50):
            logger.warning(
                f"adata.X appears to contain raw integer counts (max={X_data.max():.1f}). "
                f"Method '{method}' expects logarithmized data. "
                "Results/P-values may be inaccurate if data is not log-normalized."
            )

    if n_groups < 2:
        raise ValueError(f"Need at least 2 groups for DE, found {n_groups}")

    two_group = group1 is not None and group2 is not None

    if two_group:
        for label, grp in [("group1", group1), ("group2", group2)]:
            if str(grp) not in [str(g) for g in groups_list]:
                raise ValueError(f"--{label} '{grp}' not found in '{groupby}'. Available: {groups_list}")
        sc.tl.rank_genes_groups(adata, groupby=groupby, groups=[group1], reference=group2, method=method, n_genes=n_top_genes)
    else:
        sc.tl.rank_genes_groups(adata, groupby=groupby, method=method, n_genes=n_top_genes)

    # Apply marker filtering to remove non-specific genes
    n_filtered = 0
    if filter_markers and not two_group:
        try:
            sc.tl.filter_rank_genes_groups(
                adata,
                min_in_group_fraction=min_in_group_fraction,
                min_fold_change=min_fold_change,
                max_out_group_fraction=max_out_group_fraction,
            )
            logger.info(
                "Filtered markers: min_in_group=%.0f%%, min_fc=%.1f, max_out_group=%.0f%%",
                min_in_group_fraction * 100, min_fold_change, max_out_group_fraction * 100,
            )
            # Remove the filtered key to avoid h5ad serialization issues
            # (masked arrays from filter_rank_genes_groups can't be written to h5py)
            if "rank_genes_groups_filtered" in adata.uns:
                del adata.uns["rank_genes_groups_filtered"]
        except Exception as e:
            logger.warning("Marker filtering failed (using unfiltered): %s", e)

    tested_groups = list(adata.uns["rank_genes_groups"]["names"].dtype.names)
    group_dfs = []
    for grp in tested_groups:
        grp_df = sc.get.rank_genes_groups_df(adata, group=grp)
        grp_df.insert(0, "group", grp)
        group_dfs.append(grp_df)
    markers_df = pd.concat(group_dfs, ignore_index=True) if group_dfs else pd.DataFrame()

    # Remove NaN gene names (filtered out by filter_rank_genes_groups)
    if "names" in markers_df.columns:
        n_before = len(markers_df)
        markers_df = markers_df.dropna(subset=["names"])
        n_filtered = n_before - len(markers_df)
        if n_filtered > 0:
            logger.info("Removed %d non-specific marker entries after filtering", n_filtered)

    full_df = markers_df.copy()
    top_df = markers_df.groupby("group", sort=False).head(n_top_genes).reset_index(drop=True)

    return {
        "n_cells": n_cells, "n_genes": n_genes, "n_groups": n_groups,
        "groups": groups_list, "groupby": groupby, "method": method,
        "n_top_genes": n_top_genes, "two_group": two_group,
        "group1": group1, "group2": group2,
        "n_de_genes": len(markers_df), "markers_df": top_df, "full_df": full_df,
        "filter_markers": filter_markers, "n_filtered_out": n_filtered,
    }


def run_pydeseq2(
    adata, *, groupby: str = "leiden", group1: str, group2: str,
    n_top_genes: int = 10, min_cells_per_sample: int = 10,
) -> dict:
    """Run pseudobulk DE using PyDESeq2.
    
    Requires raw integer counts. Will extract from adata.layers['counts'] or
    adata.raw, compute pseudobulk summation per condition, and run DESeq2.
    """
    from .dependency_manager import require
    require("pydeseq2", feature="PyDESeq2 pseudobulk differential expression")

    from pydeseq2.dds import DeseqDataSet
    from pydeseq2.ds import DeseqStats

    if groupby not in adata.obs.columns:
        raise ValueError(f"Groupby column '{groupby}' not found")

    groups_list = sorted(adata.obs[groupby].unique().tolist(), key=str)
    for label, grp in [("group1", group1), ("group2", group2)]:
        if str(grp) not in [str(g) for g in groups_list]:
            raise ValueError(f"--{label} '{grp}' not found in '{groupby}'")

    raw_counts = _get_raw_counts(adata)
    logger.info("Aggregating pseudobulk samples for PyDESeq2 ...")

    mask = adata.obs[groupby].isin([str(group1), str(group2)])
    adata_sub = adata[mask].copy()
    raw_sub = raw_counts[mask.values]

    group_labels = adata_sub.obs[groupby].astype(str).values
    unique_groups = [str(group1), str(group2)]

    # Design validation: check sample counts per condition
    for grp in unique_groups:
        grp_count = (group_labels == str(grp)).sum()
        n_potential_samples = max(1, grp_count // min_cells_per_sample)
        if n_potential_samples < 2:
            logger.warning(
                "Condition '%s' has %d cells, yielding ~%d pseudobulk sample(s). "
                "DESeq2 requires >= 2 replicates per condition for reliable dispersion estimation.",
                grp, grp_count, n_potential_samples,
            )

    sample_ids, sample_conditions, pseudobulk_counts = [], [], []

    for grp in unique_groups:
        grp_mask = group_labels == str(grp)
        grp_counts = raw_sub[grp_mask]
        n_cells_in_group = grp_counts.shape[0]
        n_samples = min(max(1, n_cells_in_group // min_cells_per_sample), 10)

        indices = np.arange(n_cells_in_group)
        np.random.RandomState(42).shuffle(indices)

        for i, split_idx in enumerate(np.array_split(indices, n_samples)):
            if len(split_idx) < 3:
                continue
            pseudobulk_counts.append(grp_counts[split_idx].sum(axis=0))
            sample_ids.append(f"{grp}_rep{i}")
            sample_conditions.append(str(grp))

    if len(sample_ids) < 4:
        raise ValueError(f"Insufficient pseudobulk samples ({len(sample_ids)})")

    counts_df = pd.DataFrame(np.vstack(pseudobulk_counts), index=sample_ids, columns=adata_sub.var_names)
    metadata = pd.DataFrame({"condition": sample_conditions}, index=sample_ids)
    counts_df = counts_df.loc[:, counts_df.sum(axis=0) > 10]

    logger.info("PyDESeq2: %d samples, %d genes", len(sample_ids), counts_df.shape[1])

    dds = DeseqDataSet(counts=counts_df, metadata=metadata, design_factors="condition", refit_cooks=True)
    dds.deseq2()

    stat_res = DeseqStats(dds, contrast=["condition", str(group1), str(group2)])
    stat_res.summary()

    results_df = stat_res.results_df.copy()
    results_df["gene"] = results_df.index
    results_df = results_df.rename(columns={
        "log2FoldChange": "logfoldchanges", "pvalue": "pvals",
        "padj": "pvals_adj", "baseMean": "scores",
    })
    results_df["names"] = results_df["gene"]
    results_df["group"] = str(group1)
    results_df = results_df.sort_values("pvals_adj", na_position="last")

    n_sig = (results_df["pvals_adj"].dropna() < 0.05).sum()
    logger.info("PyDESeq2: %d significant DE genes", n_sig)

    return {
        "n_cells": adata.n_obs, "n_genes": adata.n_vars,
        "n_groups": len(unique_groups), "groups": unique_groups,
        "groupby": groupby, "method": "pydeseq2",
        "n_top_genes": n_top_genes, "two_group": True,
        "group1": group1, "group2": group2,
        "n_de_genes": len(results_df), "n_significant": n_sig,
        "n_pseudobulk_samples": len(sample_ids),
        "markers_df": results_df.head(n_top_genes).copy(),
        "full_df": results_df.copy(),
    }
