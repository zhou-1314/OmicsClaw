"""Spatial condition comparison functions.

Provides pseudobulk aggregation and PyDESeq2/Wilcoxon condition comparison.

Input matrix convention (per-component):
  - pseudobulk aggregation: adata.layers["counts"] (raw) — sum aggregation
                            requires integer-like counts, not log-normalized
  - PyDESeq2:              pseudobulk raw integer counts — NB/GLM model
  - Wilcoxon:              pseudobulk counts internally converted to log-CPM

Usage::

    from skills.spatial._lib.condition import run_condition_comparison, pseudobulk_aggregate
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import sparse, stats

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("pydeseq2", "wilcoxon")

# Pseudobulk + PyDESeq2 require raw counts; Wilcoxon internally normalizes.
COUNT_BASED_METHODS = ("pydeseq2",)


def pseudobulk_aggregate(adata, *, sample_key: str, cluster_key: str = "leiden") -> dict[str, pd.DataFrame]:
    """Aggregate raw counts to pseudobulk per (sample, cluster).

    Uses ``adata.layers["counts"]`` (raw integer counts) for sum aggregation.
    Pseudobulk must be computed from raw counts — summing log-normalized values
    is statistically invalid because log(a) + log(b) != log(a + b).

    Falls back to ``adata.raw`` then ``adata.X`` with a warning if no counts
    layer is available.  Values are clipped to non-negative and rounded to int.
    """
    if sample_key not in adata.obs.columns:
        raise ValueError(f"Sample key '{sample_key}' not in adata.obs")
    if cluster_key not in adata.obs.columns:
        raise ValueError(f"Cluster key '{cluster_key}' not in adata.obs")

    # Select raw counts source with fallback chain.
    if "counts" in adata.layers:
        X = adata.layers["counts"]
        logger.info("Pseudobulk: using adata.layers['counts'] (raw counts)")
    elif adata.raw is not None:
        X = adata.raw.X
        logger.warning(
            "Pseudobulk: no 'counts' layer found; using adata.raw. "
            "Ensure adata.raw contains raw counts, not log-normalized values."
        )
    else:
        X = adata.X
        logger.warning(
            "Pseudobulk: no 'counts' layer or adata.raw found; using adata.X. "
            "If adata.X is log-normalized, pseudobulk sums will be statistically invalid. "
            "Ensure preprocessing saves raw counts: adata.layers['counts'] = adata.X.copy()"
        )
    if sparse.issparse(X): X = X.toarray()
    X = np.asarray(X)
    if np.any(X < 0): X = np.clip(X, 0, None)
    if X.dtype.kind == "f": X = np.round(X).astype(int)

    clusters = sorted(adata.obs[cluster_key].unique().tolist(), key=str)
    samples = sorted(adata.obs[sample_key].unique().tolist(), key=str)

    result: dict[str, pd.DataFrame] = {}
    for cl in clusters:
        rows, row_labels = [], []
        for samp in samples:
            mask = (adata.obs[cluster_key].values == cl) & (adata.obs[sample_key].values == samp)
            if np.sum(mask) == 0: continue
            row_labels.append(samp)
            rows.append(X[mask].sum(axis=0))
        if len(rows) >= 2:
            result[str(cl)] = pd.DataFrame(rows, index=row_labels, columns=adata.var_names)
    return result


def run_pydeseq2(count_df: pd.DataFrame, condition_labels: pd.Series, reference: str, other: str) -> pd.DataFrame:
    """Run PyDESeq2 on pseudobulk counts.

    Requires raw integer counts — PyDESeq2's negative-binomial GLM model
    validates that input values are non-negative integers via
    ``test_valid_counts``.  Do NOT pass log-normalized, CPM, or TPM values.
    """
    from .dependency_manager import require
    require("pydeseq2", feature="DESeq2-style pseudobulk analysis")
    from pydeseq2.dds import DeseqDataSet
    from pydeseq2.ds import DeseqStats

    metadata = pd.DataFrame({"condition": condition_labels.astype(str)}, index=count_df.index)
    dds = DeseqDataSet(counts=count_df.astype(int), metadata=metadata, design_factors="condition", refit_cooks=True)
    dds.deseq2()
    stat = DeseqStats(dds, contrast=["condition", other, reference])
    stat.summary()
    res = stat.results_df.copy()
    res = res.rename(columns={"log2FoldChange": "log2fc", "padj": "pvalue_adj"})
    res["gene"] = res.index
    return res[["gene", "log2fc", "pvalue_adj"]].reset_index(drop=True)


def run_wilcoxon_pseudobulk(count_df: pd.DataFrame, condition_labels: pd.Series, reference: str) -> pd.DataFrame:
    """Wilcoxon rank-sum on pseudobulk log-CPM values.

    Takes pseudobulk raw count matrix and internally converts to log-CPM
    before running the non-parametric test.  This is the correct approach:
    the Wilcoxon test needs approximately continuous values, so the function
    handles the transformation internally rather than expecting pre-normalized
    input.  Suitable as a fallback when sample counts are too low for PyDESeq2.
    """
    lib_size = count_df.sum(axis=1).replace(0, 1)
    log_cpm = np.log1p(count_df.div(lib_size, axis=0) * 1e6)
    ref_mask, other_mask = condition_labels == reference, condition_labels != reference

    if np.sum(ref_mask) < 1 or np.sum(other_mask) < 1:
        return pd.DataFrame(columns=["gene", "log2fc", "pvalue_adj"])

    records = []
    for gene in count_df.columns:
        a, b = log_cpm.loc[other_mask, gene].values, log_cpm.loc[ref_mask, gene].values
        if np.std(a) < 1e-10 and np.std(b) < 1e-10: continue
        try:
            _, pval = stats.ranksums(a, b)
        except Exception: continue
        records.append({"gene": gene, "log2fc": float(np.mean(a) - np.mean(b)), "pvalue_adj": pval})

    df = pd.DataFrame(records)
    if not df.empty:
        from statsmodels.stats.multitest import multipletests
        try:
            _, adj, _, _ = multipletests(df["pvalue_adj"], method="fdr_bh")
            df["pvalue_adj"] = adj
        except Exception: pass
        df = df.sort_values("pvalue_adj").reset_index(drop=True)
    return df


def run_condition_comparison(adata, *, condition_key: str, sample_key: str,
                             reference_condition: str | None = None, cluster_key: str = "leiden",
                             method: str = "pydeseq2") -> dict:
    """Run pseudobulk condition comparison.

    Pseudobulk aggregation always uses raw counts from ``adata.layers["counts"]``.
    The DE method is selected by ``method``:
      - ``pydeseq2``: negative-binomial GLM on raw integer pseudobulk counts
      - ``wilcoxon``: non-parametric rank-sum on internally computed log-CPM

    Falls back to Wilcoxon automatically if PyDESeq2 fails for a cluster.
    """
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"Unknown method '{method}'. Choose from: {SUPPORTED_METHODS}")

    conditions = sorted(adata.obs[condition_key].unique().tolist(), key=str)
    if len(conditions) < 2:
        raise ValueError(f"Need >= 2 conditions in '{condition_key}', found {conditions}")
    ref = reference_condition or conditions[0]
    if ref not in conditions:
        raise ValueError(f"Reference '{ref}' not in conditions: {conditions}")

    samples = sorted(adata.obs[sample_key].unique().tolist(), key=str)
    pb_dict = pseudobulk_aggregate(adata, sample_key=sample_key, cluster_key=cluster_key)
    sample_condition = adata.obs[[sample_key, condition_key]].drop_duplicates().set_index(sample_key)[condition_key]

    all_de: dict[str, pd.DataFrame] = {}
    method_used = method
    for cl, count_df in pb_dict.items():
        cond_strs = sample_condition.loc[count_df.index].astype(str)
        unique_conds = cond_strs.unique().tolist()
        if len(unique_conds) < 2 or str(ref) not in unique_conds: continue

        filtered = count_df.loc[:, count_df.sum(axis=0) >= 10]
        if filtered.shape[1] < 5: continue

        for other_c in unique_conds:
            if other_c == str(ref): continue
            mask = cond_strs.isin([str(ref), other_c])
            de_df = None

            if method == "pydeseq2":
                try:
                    de_df = run_pydeseq2(filtered[mask], cond_strs[mask], str(ref), other_c)
                    de_df["method"] = "pydeseq2"
                except Exception as exc:
                    logger.warning("PyDESeq2 failed for cluster %s (%s), falling back to Wilcoxon: %s", cl, other_c, exc)
                    try:
                        de_df = run_wilcoxon_pseudobulk(filtered[mask], cond_strs[mask], str(ref))
                        de_df["method"] = "wilcoxon"
                        method_used = "pydeseq2+wilcoxon_fallback"
                    except Exception as exc2:
                        logger.error("Wilcoxon also failed for cluster %s: %s", cl, exc2)
            elif method == "wilcoxon":
                try:
                    de_df = run_wilcoxon_pseudobulk(filtered[mask], cond_strs[mask], str(ref))
                    de_df["method"] = "wilcoxon"
                except Exception as exc:
                    logger.error("Wilcoxon failed for cluster %s: %s", cl, exc)

            if de_df is not None and not de_df.empty:
                de_df["cluster"] = cl
                de_df["contrast"] = f"{other_c}_vs_{ref}"
                all_de[f"{cl}_{other_c}"] = de_df

    global_de = pd.concat(all_de.values(), ignore_index=True) if all_de else pd.DataFrame()
    sig_count = int((global_de["pvalue_adj"] < 0.05).sum()) if not global_de.empty else 0

    return {
        "n_cells": adata.n_obs, "n_genes": adata.n_vars, "conditions": conditions,
        "reference": ref, "n_samples": len(samples), "n_clusters_tested": len(all_de),
        "n_de_genes_total": len(global_de), "n_significant": sig_count,
        "method": method_used,
        "global_de": global_de, "per_cluster_de": all_de,
        "cluster_key": cluster_key, "condition_key": condition_key, "sample_key": sample_key,
    }
