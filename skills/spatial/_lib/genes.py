"""Spatially variable gene (SVG) detection algorithms.

Provides multiple methods for identifying genes with spatial patterns:
  - morans:    Moran's I spatial autocorrelation via Squidpy (default)
  - spatialde: Gaussian process regression via SpatialDE (old) / SpatialDE2
  - sparkx:    Non-parametric kernel test via SPARK-X in R
  - flashs:    Randomized kernel approximation (Python native, fast)

Input matrix convention (per-method):
  - morans:    adata.X (log-normalized) — spatial autocorrelation on continuous values
  - spatialde: adata.layers["counts"] (raw) — NaiveDE stabilizes counts internally
  - sparkx:    adata.layers["counts"] (raw) — count-based kernel test
  - flashs:    adata.layers["counts"] (raw) — designed for sparse count matrices

Usage::

    from skills.spatial._lib.genes import run_morans, run_spatialde, SUPPORTED_METHODS

    df, summary = run_morans(adata, n_top_genes=20)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import sparse

from .adata_utils import get_spatial_key, require_spatial_coords

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("morans", "spatialde", "sparkx", "flashs")

# Methods that require raw counts rather than log-normalized expression.
COUNT_BASED_METHODS = ("spatialde", "sparkx", "flashs")


def _get_dense_expression(adata, gene_mask=None, *, layer: str | None = None) -> np.ndarray:
    """Return a dense (n_obs, n_genes) array, optionally subsetting columns.

    Parameters
    ----------
    layer : str or None
        If given, read from ``adata.layers[layer]``; otherwise read ``adata.X``.
    """
    X = adata.layers[layer] if layer is not None else adata.X
    if gene_mask is not None:
        X = X[:, gene_mask]
    if sparse.issparse(X):
        return X.toarray()
    return np.asarray(X)


def _get_counts_layer(adata) -> str | None:
    """Return the name of the raw-counts layer, or None if unavailable.

    Looks for ``layers["counts"]`` (standard convention set by preprocessing).
    Falls back to ``adata.raw`` by copying into a temporary layer.
    """
    if "counts" in adata.layers:
        return "counts"
    if adata.raw is not None:
        logger.info("No 'counts' layer found; copying from adata.raw")
        adata.layers["counts"] = adata.raw.X.copy()
        return "counts"
    return None


# ---------------------------------------------------------------------------
# Moran's I
# ---------------------------------------------------------------------------


def run_morans(
    adata, *, n_top_genes: int = 20, fdr_threshold: float = 0.05,
    n_neighs: int = 6, n_perms: int = 100,
) -> tuple[pd.DataFrame, dict]:
    """Compute Moran's I for all genes and return ranked SVG table + summary.

    Uses ``adata.X`` (log-normalized) — Squidpy's ``spatial_autocorr`` computes
    spatial autocorrelation on continuous expression values, not raw counts.
    """
    import squidpy as sq

    spatial_key = require_spatial_coords(adata)
    logger.info("Computing spatial autocorrelation (Moran's I) on adata.X (log-normalized) for %d genes ...", adata.n_vars)

    sq.gr.spatial_neighbors(adata, n_neighs=n_neighs, coord_type="generic", spatial_key=spatial_key)
    sq.gr.spatial_autocorr(adata, mode="moran", n_perms=n_perms, n_jobs=1)

    if "moranI" not in adata.uns:
        raise RuntimeError("squidpy did not produce 'moranI' results")

    df = adata.uns["moranI"].copy()
    df["gene"] = df.index

    if "pval_norm" in df.columns:
        sig = df[(df["I"] > 0) & (df["pval_norm"] < fdr_threshold)].copy()
    else:
        sig = df[df["I"] > 0].copy()

    sig = sig.sort_values("I", ascending=False)
    top = sig.head(n_top_genes)

    summary = {
        "method": "morans", "n_genes_tested": len(df),
        "n_significant": len(sig), "n_top_reported": len(top),
        "fdr_threshold": fdr_threshold, "top_genes": top["gene"].tolist(),
    }
    logger.info("Moran's I: %d/%d genes significant, reporting top %d", len(sig), len(df), len(top))
    return df, summary


# ---------------------------------------------------------------------------
# SpatialDE
# ---------------------------------------------------------------------------


def run_spatialde(
    adata, *, n_top_genes: int = 20, fdr_threshold: float = 0.05, omnibus: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """SpatialDE SVG detection with Gaussian process regression.

    Uses raw counts from ``adata.layers["counts"]`` — NaiveDE.stabilize()
    performs variance-stabilizing transformation internally, which requires
    integer-like counts as input (not already log-normalized values).
    Falls back to ``adata.X`` with a warning if no counts layer is available.
    """
    from .dependency_manager import require

    # scipy compat shims for SpatialDE 1.x
    import scipy as _scipy
    _NUMPY_COMPAT_ATTRS = [
        "arange", "array", "argsort", "bool_", "concatenate", "diag", "dot",
        "empty", "exp", "eye", "float64", "inf", "int32", "log", "log2",
        "newaxis", "ones", "sqrt", "sum", "zeros", "zeros_like", "isnan",
        "nan", "pi", "linspace", "meshgrid",
    ]
    for _attr in _NUMPY_COMPAT_ATTRS:
        if not hasattr(_scipy, _attr) and hasattr(np, _attr):
            setattr(_scipy, _attr, getattr(np, _attr))

    import scipy.misc as _scipy_misc
    if not hasattr(_scipy_misc, "derivative"):
        def _derivative_compat(func, x0, dx=1.0, n=1, args=(), order=3):
            if n == 1:
                return (func(x0 + dx, *args) - func(x0 - dx, *args)) / (2.0 * dx)
            if n == 2:
                return (func(x0 + dx, *args) - 2.0 * func(x0, *args) + func(x0 - dx, *args)) / dx**2
            from math import comb
            ho = order >> 1
            weights = np.array([(-1) ** (n - k + ho) * comb(n, abs(k - ho)) for k in range(order)], dtype=float)
            vals = np.array([func(x0 + (k - ho) * dx, *args) for k in range(order)])
            return np.dot(weights, vals) / dx**n
        _scipy_misc.derivative = _derivative_compat

    require("spatialde", feature="SpatialDE spatially variable gene detection")
    import SpatialDE
    import NaiveDE

    spatial_key = require_spatial_coords(adata)
    coords = adata.obsm[spatial_key]
    logger.info("Running SpatialDE on %d genes ...", adata.n_vars)

    # Use raw counts for NaiveDE stabilize — it expects integer-like values.
    counts_layer = _get_counts_layer(adata)
    if counts_layer is not None:
        logger.info("SpatialDE: using adata.layers['%s'] (raw counts)", counts_layer)
        X_input = adata.layers[counts_layer]
    else:
        logger.warning(
            "SpatialDE: no 'counts' layer found; falling back to adata.X. "
            "Results may be suboptimal — NaiveDE.stabilize() expects raw counts."
        )
        X_input = adata.X

    if sparse.issparse(X_input):
        X_input = X_input.toarray()
    else:
        X_input = np.asarray(X_input)

    counts = pd.DataFrame(X_input, index=adata.obs_names, columns=adata.var_names)
    gene_totals = counts.sum(axis=0)
    counts = counts.T[gene_totals >= 3].T
    if counts.shape[1] == 0:
        raise ValueError("All genes have < 3 total counts")
    logger.info("SpatialDE: %d genes remain after count filter", counts.shape[1])

    sample_info = pd.DataFrame(
        {"x": coords[:, 0], "y": coords[:, 1], "total_counts": counts.sum(axis=1)},
        index=adata.obs_names,
    )

    norm_expr = NaiveDE.stabilize(counts.T).T
    resid_expr = NaiveDE.regress_out(sample_info, norm_expr.T, "np.log(total_counts)").T

    gene_var = resid_expr.var(axis=0)
    resid_expr = resid_expr.loc[:, gene_var > 0]
    if resid_expr.shape[1] == 0:
        raise ValueError("All genes have zero variance after normalization")

    X = sample_info[["x", "y"]]
    results = SpatialDE.run(X, resid_expr)

    aeh_results = None
    if omnibus:
        sign_results = results.query("qval < @fdr_threshold")
        if len(sign_results) >= 5:
            l_aeh = float(sign_results["l"].median())
            n_patterns = min(max(3, len(sign_results) // 10), 10)
            try:
                aeh_results, _ = SpatialDE.spatial_patterns(X, resid_expr, sign_results, C=n_patterns, l=l_aeh, verbosity=0)
            except Exception as e:
                logger.warning("AEH failed (non-fatal): %s", e)

    results = results.sort_values("qval")
    col_map = {"g": "gene", "qval": "pval_norm", "LLR": "I"}
    df = results.rename(columns=col_map)
    if "gene" not in df.columns and "g" in results.columns:
        df["gene"] = results["g"]
    df = df.set_index("gene", drop=False)

    sig = df[df["pval_norm"] < fdr_threshold].copy()
    top = sig.head(n_top_genes)

    summary = {
        "method": "spatialde", "n_genes_tested": len(df),
        "n_significant": len(sig), "n_top_reported": len(top),
        "fdr_threshold": fdr_threshold, "top_genes": top["gene"].tolist(),
    }
    if aeh_results is not None:
        summary["aeh_patterns"] = int(aeh_results["pattern"].nunique())

    adata.uns["spatialde_results"] = results
    logger.info("SpatialDE: %d/%d genes significant", len(sig), len(df))
    return df, summary


# ---------------------------------------------------------------------------
# SPARK-X
# ---------------------------------------------------------------------------


def run_sparkx(
    adata, *, n_top_genes: int = 20, fdr_threshold: float = 0.05, n_max_genes: int = 5000,
) -> tuple[pd.DataFrame, dict]:
    """SPARK-X non-parametric kernel test for SVG detection (R via subprocess).

    Uses raw counts from ``adata.layers["counts"]`` — SPARK-X is designed to
    operate on a count matrix directly (not log-normalized expression).
    Falls back to ``adata.X`` with a warning if no counts layer is available.
    """
    import tempfile
    from pathlib import Path
    from omicsclaw.core.dependency_manager import validate_r_environment
    from omicsclaw.core.r_script_runner import RScriptRunner
    from omicsclaw.core.r_utils import read_r_result_csv

    validate_r_environment(required_r_packages=["SPARK"])

    spatial_key = require_spatial_coords(adata)
    coords = adata.obsm[spatial_key][:, :2]

    # Determine raw-counts layer for SPARK-X input.
    counts_layer = _get_counts_layer(adata)
    if counts_layer is not None:
        logger.info("SPARK-X: using adata.layers['%s'] (raw counts)", counts_layer)
    else:
        logger.warning(
            "SPARK-X: no 'counts' layer found; falling back to adata.X. "
            "SPARK-X expects raw count data for proper statistical modeling."
        )
        counts_layer = None  # _get_dense_expression will read adata.X

    if adata.n_vars > n_max_genes:
        logger.info("Subsetting to top %d HVGs for SPARK-X", n_max_genes)
        if "highly_variable" in adata.var.columns:
            hvg_mask = adata.var["highly_variable"].values
            if hvg_mask.sum() > n_max_genes:
                hvg_idx = np.where(hvg_mask)[0][:n_max_genes]
                hvg_mask = np.zeros(adata.n_vars, dtype=bool)
                hvg_mask[hvg_idx] = True
        else:
            gene_var = np.var(_get_dense_expression(adata, layer=counts_layer), axis=0)
            top_idx = np.argsort(gene_var)[-n_max_genes:]
            hvg_mask = np.zeros(adata.n_vars, dtype=bool)
            hvg_mask[top_idx] = True
        adata_sub = adata[:, hvg_mask].copy()
    else:
        adata_sub = adata

    X_dense = _get_dense_expression(adata_sub, layer=counts_layer)
    gene_names = list(adata_sub.var_names)
    logger.info("Running SPARK-X on %d genes (raw counts) ...", len(gene_names))

    scripts_dir = Path(__file__).resolve().parents[3] / "omicsclaw" / "r_scripts"
    runner = RScriptRunner(scripts_dir=scripts_dir)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_sparkx_") as tmpdir:
        tmpdir = Path(tmpdir)

        # Write counts (genes x spots) and coords
        counts_df = pd.DataFrame(X_dense.T, index=gene_names, columns=adata_sub.obs_names)
        counts_df.to_csv(tmpdir / "counts.csv")

        coords_df = pd.DataFrame(coords, index=adata_sub.obs_names, columns=["x", "y"])
        coords_df.to_csv(tmpdir / "coords.csv")

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sp_sparkx.R",
            args=[str(tmpdir / "counts.csv"), str(tmpdir / "coords.csv"), str(output_dir)],
            expected_outputs=["sparkx_results.csv"],
            output_dir=output_dir,
        )

        res_df = read_r_result_csv(output_dir / "sparkx_results.csv")

    # Ensure expected columns
    if "pval" not in res_df.columns and "combinedPval" in res_df.columns:
        res_df = res_df.rename(columns={"combinedPval": "pval"})
    if "qval" not in res_df.columns and "adjustedPval" in res_df.columns:
        res_df = res_df.rename(columns={"adjustedPval": "qval"})
    if "gene" not in res_df.columns:
        res_df["gene"] = res_df.index

    res_df = res_df.rename(columns={"pval": "pval_norm"})
    res_df["I"] = -np.log10(res_df["pval_norm"].clip(lower=1e-300))
    res_df = res_df.set_index("gene", drop=False).sort_values("pval_norm")

    sig = res_df[res_df["pval_norm"] < fdr_threshold].copy()
    top = sig.head(n_top_genes)

    summary = {
        "method": "sparkx", "n_genes_tested": len(res_df),
        "n_significant": len(sig), "n_top_reported": len(top),
        "fdr_threshold": fdr_threshold, "top_genes": top["gene"].tolist(),
    }
    logger.info("SPARK-X: %d/%d genes significant", len(sig), len(res_df))
    return res_df, summary


# ---------------------------------------------------------------------------
# FlashS
# ---------------------------------------------------------------------------


def run_flashs(
    adata, *, n_top_genes: int = 20, fdr_threshold: float = 0.05, n_rand_features: int = 500,
) -> tuple[pd.DataFrame, dict]:
    """FlashS randomized-kernel SVG detection (Python native, fast).

    Uses raw counts from ``adata.layers["counts"]`` — FlashS is designed to
    exploit sparsity and count structure of ST data for its three-part test
    (binary presence, rank intensity, raw count).
    Falls back to ``adata.X`` with a warning if no counts layer is available.
    """
    from scipy.stats import chi2

    spatial_key = require_spatial_coords(adata)
    coords = adata.obsm[spatial_key][:, :2].astype(np.float64)
    n_obs, n_genes = adata.shape
    logger.info("Running FlashS on %d genes (%d spots) ...", n_genes, n_obs)

    bandwidth = np.median(np.std(coords, axis=0))
    if bandwidth < 1e-10:
        bandwidth = 1.0

    rng = np.random.RandomState(42)
    m = n_rand_features
    omega = rng.randn(2, m) / bandwidth
    phase = rng.uniform(0, 2 * np.pi, m)

    Z = np.sqrt(2.0 / m) * np.cos(coords @ omega + phase)
    Z = Z - Z.mean(axis=0)

    # Use raw counts for FlashS — designed for sparse count matrices.
    counts_layer = _get_counts_layer(adata)
    if counts_layer is not None:
        logger.info("FlashS: using adata.layers['%s'] (raw counts)", counts_layer)
    else:
        logger.warning(
            "FlashS: no 'counts' layer found; falling back to adata.X. "
            "FlashS is designed for raw sparse count matrices."
        )
    X_dense = _get_dense_expression(adata, layer=counts_layer)
    X_centered = X_dense - X_dense.mean(axis=0)
    XtZ = X_centered.T @ Z
    stat = np.sum(XtZ ** 2, axis=1) / n_obs

    pvalues = 1 - chi2.cdf(stat * n_obs, df=m)

    from statsmodels.stats.multitest import multipletests
    _, qvalues, _, _ = multipletests(pvalues, method="fdr_bh")

    df = pd.DataFrame({"gene": adata.var_names, "I": stat, "pval_norm": pvalues, "qval": qvalues})
    df = df.set_index("gene", drop=False).sort_values("pval_norm")

    sig = df[df["pval_norm"] < fdr_threshold].copy()
    top = sig.head(n_top_genes)

    summary = {
        "method": "flashs", "n_genes_tested": len(df),
        "n_significant": len(sig), "n_top_reported": len(top),
        "fdr_threshold": fdr_threshold, "n_random_features": n_rand_features,
        "bandwidth": float(bandwidth), "top_genes": top["gene"].tolist(),
    }
    logger.info("FlashS: %d/%d genes significant", len(sig), len(df))
    return df, summary


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

METHOD_DISPATCH = {
    "morans": run_morans,
    "spatialde": run_spatialde,
    "sparkx": run_sparkx,
    "flashs": run_flashs,
}
