"""Spatial trajectory analysis functions.

Trajectory inference using DPT and CellRank, with support for:
- Root cell selection by cell type (progenitor specification)
- Trajectory gene correlation (Spearman + FDR correction)
- Enhanced CellRank fate mapping with driver gene identification

Usage::

    from skills.spatial._lib.trajectory import run_trajectory, SUPPORTED_METHODS
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import scanpy as sc

from .adata_utils import ensure_neighbors, ensure_pca

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("dpt", "cellrank", "palantir")


def find_trajectory_genes(
    adata, *, n_top: int = 200, fdr_threshold: float = 0.05,
) -> pd.DataFrame:
    """Find genes correlated with pseudotime using Spearman rank correlation.

    Parameters
    ----------
    adata : AnnData
        Must have ``dpt_pseudotime`` in ``adata.obs``.
    n_top : int
        Maximum number of top genes to return.
    fdr_threshold : float
        FDR threshold for significance (default 0.05).

    Returns
    -------
    pd.DataFrame
        Columns: gene, correlation, pvalue, fdr, direction.
    """
    if "dpt_pseudotime" not in adata.obs.columns:
        logger.warning("No pseudotime found; cannot compute trajectory genes")
        return pd.DataFrame()

    from scipy import sparse, stats

    pseudotime = adata.obs["dpt_pseudotime"].values
    finite_mask = np.isfinite(pseudotime)
    if finite_mask.sum() < 10:
        logger.warning("Too few cells with finite pseudotime (%d)", finite_mask.sum())
        return pd.DataFrame()

    pt = pseudotime[finite_mask]
    X = adata.X[finite_mask]
    if sparse.issparse(X):
        X = X.toarray()
    X = np.asarray(X)

    n_genes = X.shape[1]
    correlations = np.zeros(n_genes)
    pvalues = np.ones(n_genes)

    for i in range(n_genes):
        expr = X[:, i]
        if np.std(expr) < 1e-10:
            continue
        rho, pval = stats.spearmanr(pt, expr)
        correlations[i] = rho
        pvalues[i] = pval

    # Benjamini-Hochberg FDR correction
    try:
        from statsmodels.stats.multitest import multipletests
        _, fdr_vals, _, _ = multipletests(pvalues, method="fdr_bh")
    except ImportError:
        # Manual BH correction
        n = len(pvalues)
        sorted_idx = np.argsort(pvalues)
        fdr_vals = np.ones(n)
        for rank, idx in enumerate(sorted_idx, 1):
            fdr_vals[idx] = pvalues[idx] * n / rank
        fdr_vals = np.minimum.accumulate(fdr_vals[np.argsort(sorted_idx)][::-1])[::-1]
        fdr_vals = np.clip(fdr_vals, 0, 1)

    results = pd.DataFrame({
        "gene": adata.var_names,
        "correlation": correlations,
        "pvalue": pvalues,
        "fdr": fdr_vals,
        "direction": np.where(correlations > 0, "increasing", "decreasing"),
    })
    results = results[results["fdr"] < fdr_threshold]
    results = results.sort_values("correlation", key=abs, ascending=False)
    results = results.head(n_top).reset_index(drop=True)

    logger.info("Found %d trajectory-correlated genes (FDR < %.2f)", len(results), fdr_threshold)
    return results


def run_dpt(
    adata, *, root_cell: str | None = None, root_cell_type: str | None = None,
    cluster_key: str = "leiden", n_dcs: int = 10,
) -> dict:
    """Run diffusion pseudotime using scanpy.

    Parameters
    ----------
    root_cell : str | None
        Specific cell barcode to use as root.
    root_cell_type : str | None
        Cell type/cluster to select root from (picks cell with min DC1
        within this group, representing the most stem-like cell).
    cluster_key : str
        Column in adata.obs containing cluster/cell-type labels.
    n_dcs : int
        Number of diffusion components.
    """
    ensure_pca(adata)
    ensure_neighbors(adata)

    n_comps = min(n_dcs, adata.obsm["X_pca"].shape[1], adata.n_obs - 2)
    sc.tl.diffmap(adata, n_comps=max(n_comps, 2))

    dc1 = adata.obsm["X_diffmap"][:, 0]

    if root_cell and root_cell in adata.obs_names:
        adata.uns["iroot"] = list(adata.obs_names).index(root_cell)
        logger.info("Using provided root cell: %s", root_cell)
    elif root_cell_type and cluster_key in adata.obs.columns:
        type_mask = adata.obs[cluster_key].astype(str) == str(root_cell_type)
        if type_mask.sum() > 0:
            dc1_subset = dc1[type_mask.values]
            root_idx = np.where(type_mask.values)[0][np.argmin(dc1_subset)]
            adata.uns["iroot"] = int(root_idx)
            root_cell = adata.obs_names[root_idx]
            logger.info(
                "Root cell from type '%s': %s (min DC1 within group)",
                root_cell_type, root_cell,
            )
        else:
            logger.warning("Root cell type '%s' not found in '%s', using max DC1", root_cell_type, cluster_key)
            adata.uns["iroot"] = int(np.argmax(dc1))
            root_cell = adata.obs_names[adata.uns["iroot"]]
    else:
        adata.uns["iroot"] = int(np.argmax(dc1))
        root_cell = adata.obs_names[adata.uns["iroot"]]
        logger.info("Auto-selected root cell: %s (max DC1)", root_cell)

    sc.tl.dpt(adata)

    dpt_vals = adata.obs["dpt_pseudotime"].values
    finite_mask = np.isfinite(dpt_vals)

    per_cluster = {}
    if cluster_key in adata.obs.columns:
        for cl in sorted(adata.obs[cluster_key].unique().tolist(), key=str):
            mask = (adata.obs[cluster_key] == cl) & finite_mask
            if np.sum(mask) > 0:
                per_cluster[str(cl)] = {
                    "mean_pseudotime": float(dpt_vals[mask].mean()),
                    "median_pseudotime": float(np.median(dpt_vals[mask])),
                    "n_cells": int(np.sum(mask)),
                }

    # Find trajectory-correlated genes
    traj_genes_df = find_trajectory_genes(adata)

    result = {
        "method": "dpt",
        "root_cell": root_cell,
        "root_cell_type": root_cell_type,
        "mean_pseudotime": float(dpt_vals[finite_mask].mean()) if np.any(finite_mask) else 0.0,
        "max_pseudotime": float(dpt_vals[finite_mask].max()) if np.any(finite_mask) else 0.0,
        "n_finite": int(np.sum(finite_mask)),
        "per_cluster": per_cluster,
    }
    if not traj_genes_df.empty:
        result["trajectory_genes"] = traj_genes_df
        result["n_trajectory_genes"] = len(traj_genes_df)
        result["top_increasing"] = traj_genes_df[traj_genes_df["direction"] == "increasing"].head(5)["gene"].tolist()
        result["top_decreasing"] = traj_genes_df[traj_genes_df["direction"] == "decreasing"].head(5)["gene"].tolist()

    return result


def run_cellrank(adata, *, n_states: int = 3, use_velocity: bool = False) -> dict:
    """Run CellRank for directed trajectory analysis.

    Parameters
    ----------
    n_states : int
        Number of macrostates to identify.
    use_velocity : bool
        If True and velocity data available, use VelocityKernel + ConnectivityKernel
        (0.8/0.2 weighting). Otherwise use ConnectivityKernel only.
    """
    from .dependency_manager import require
    require("cellrank", feature="CellRank trajectory inference")
    import cellrank as cr

    # Choose kernel based on available data
    if use_velocity and "velocity" in adata.layers:
        try:
            vk = cr.kernels.VelocityKernel(adata).compute_transition_matrix()
            ck = cr.kernels.ConnectivityKernel(adata).compute_transition_matrix()
            kernel = 0.8 * vk + 0.2 * ck
            logger.info("CellRank: using VelocityKernel(0.8) + ConnectivityKernel(0.2)")
        except Exception as exc:
            logger.warning("VelocityKernel failed (%s), using ConnectivityKernel only", exc)
            kernel = cr.kernels.ConnectivityKernel(adata).compute_transition_matrix()
    elif "dpt_pseudotime" in adata.obs.columns:
        try:
            pk = cr.kernels.PseudotimeKernel(adata, time_key="dpt_pseudotime").compute_transition_matrix()
            ck = cr.kernels.ConnectivityKernel(adata).compute_transition_matrix()
            kernel = 0.8 * pk + 0.2 * ck
            logger.info("CellRank: using PseudotimeKernel(0.8) + ConnectivityKernel(0.2)")
        except Exception as exc:
            logger.warning("PseudotimeKernel failed (%s), using ConnectivityKernel only", exc)
            kernel = cr.kernels.ConnectivityKernel(adata).compute_transition_matrix()
    else:
        kernel = cr.kernels.ConnectivityKernel(adata).compute_transition_matrix()
        logger.info("CellRank: using ConnectivityKernel only")

    estimator = cr.estimators.GPCCA(kernel)
    estimator.compute_schur(n_components=20)
    estimator.compute_macrostates(n_states=n_states)

    macro_key = None
    for candidate in ("macrostates_fwd", "macrostates", "term_states_fwd"):
        if candidate in adata.obs.columns:
            macro_key = candidate
            break

    n_macro = adata.obs[macro_key].nunique() if macro_key else 0

    # Try to compute terminal states and fate probabilities
    terminal_states = []
    driver_genes = {}
    try:
        estimator.predict_terminal_states()
        term_key = None
        for candidate in ("terminal_states", "term_states_fwd"):
            if candidate in adata.obs.columns:
                term_key = candidate
                break
        if term_key:
            terminal_states = adata.obs[term_key].dropna().unique().tolist()
            logger.info("CellRank terminal states: %s", terminal_states)

        # Compute fate probabilities
        try:
            estimator.compute_fate_probabilities()
            logger.info("Computed fate probabilities")
        except Exception as exc:
            logger.warning("Fate probabilities failed: %s", exc)

        # Identify driver genes per terminal state
        try:
            for state in terminal_states[:5]:
                drivers = estimator.compute_lineage_drivers(lineages=state)
                if drivers is not None and not drivers.empty:
                    top_drivers = drivers.head(10).index.tolist()
                    driver_genes[state] = top_drivers
        except Exception as exc:
            logger.warning("Driver gene computation failed: %s", exc)

    except Exception as exc:
        logger.warning("Terminal state prediction failed: %s", exc)

    return {
        "method": "cellrank",
        "n_macrostates": n_macro,
        "terminal_states": terminal_states,
        "driver_genes": driver_genes,
        "root_cell": None,
    }


def run_trajectory(
    adata, *, method: str = "dpt", root_cell: str | None = None,
    root_cell_type: str | None = None, cluster_key: str = "leiden",
    n_states: int = 3, use_velocity: bool = False,
) -> dict:
    """Dispatch to the selected trajectory method."""
    n_cells = adata.n_obs
    n_genes = adata.n_vars
    logger.info("Input: %d cells x %d genes", n_cells, n_genes)

    if method == "cellrank":
        try:
            result = run_cellrank(adata, n_states=n_states, use_velocity=use_velocity)
        except Exception as exc:
            logger.warning("CellRank failed (%s), falling back to DPT", exc)
            result = run_dpt(adata, root_cell=root_cell, root_cell_type=root_cell_type, cluster_key=cluster_key)
    else:
        result = run_dpt(adata, root_cell=root_cell, root_cell_type=root_cell_type, cluster_key=cluster_key)

    return {"n_cells": n_cells, "n_genes": n_genes, **result}
