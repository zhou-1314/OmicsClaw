"""Spatial CNV inference functions.

Provides inferCNVpy and Numbat for copy number variation analysis.

Input matrix convention (per-method):
  - infercnvpy: adata.X (log-normalized) — computes log-fold-change vs reference
  - numbat:     adata.layers["counts"] (raw integer UMI) — count-based CNV model,
                plus allele counts and normalized reference expression

Usage::

    from skills.spatial._lib.cnv import run_cnv, SUPPORTED_METHODS
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import sparse

from .dependency_manager import require

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("infercnvpy", "numbat")

# Numbat requires raw integer counts; infercnvpy uses log-normalized adata.X.
COUNT_BASED_METHODS = ("numbat",)


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


def validate_reference(adata, reference_key: str | None, reference_cat: list[str]) -> None:
    """Validate that reference key and categories exist in adata."""
    if reference_key is None:
        return
    if reference_key not in adata.obs.columns:
        raise ValueError(f"'{reference_key}' not in adata.obs. Available: {list(adata.obs.columns)}")
    avail = set(adata.obs[reference_key].unique())
    missing = set(reference_cat) - avail
    if missing:
        raise ValueError(f"Categories {sorted(missing)} not in '{reference_key}'. Available: {sorted(avail)}")


def run_infercnvpy(adata, *, reference_key: str | None = None, reference_cat: list[str] | None = None,
                   window_size: int = 100, step: int = 10, dynamic_threshold: float | None = 1.5) -> dict:
    """Infer CNV using inferCNVpy.

    Uses ``adata.X`` (log-normalized) — inferCNVpy subtracts the reference
    expression in log-space (equivalent to log-fold-change) and smooths across
    genomic windows.  The method explicitly requires normalized, log-transformed
    input per its documentation.

    Also requires gene genomic position annotations (chromosome, start, end)
    in ``adata.var`` and optionally a reference cell group in ``adata.obs``.
    """
    require("infercnvpy", feature="CNV inference")
    import infercnvpy as cnv

    req_cols = {"chromosome", "start", "end"}
    if not req_cols.issubset(adata.var.columns):
        missing = req_cols - set(adata.var.columns)
        raise ValueError(
            f"inferCNVpy requires genomic annotations. Missing adata.var columns: {list(missing)}. "
            "Please ensure gene positions are mapped before running CNV."
        )

    logger.info("Running inferCNVpy on adata.X (log-normalized), window=%d, step=%d", window_size, step)
    
    cnv.tl.infercnv(
        adata, 
        reference_key=reference_key, 
        reference_cat=reference_cat,
        window_size=window_size, 
        step=step, 
        dynamic_threshold=dynamic_threshold
    )
    
    logger.info("Computing overall CNV anomaly scores per cell...")
    cnv.tl.cnv_score(adata)

    cnv_score_col = "cnv_score" if "cnv_score" in adata.obs.columns else None
    
    if cnv_score_col:
        # Fill any NaNs that might have emerged during sliding window edge cases
        if adata.obs[cnv_score_col].isna().any():
            adata.obs[cnv_score_col] = adata.obs[cnv_score_col].fillna(0.0)
            
        mean_score = float(adata.obs[cnv_score_col].mean())
        threshold = float(adata.obs[cnv_score_col].quantile(0.9))
        high_cnv_pct = float((adata.obs[cnv_score_col] > threshold).mean() * 100)
    else:
        mean_score = 0.0
        high_cnv_pct = 0.0

    return {
        "method": "infercnvpy", 
        "n_genes": adata.n_vars,
        "mean_cnv_score": float(f"{mean_score:.4f}"), 
        "high_cnv_fraction_pct": float(f"{high_cnv_pct:.2f}"),
        "cnv_score_key": cnv_score_col,
    }


def run_numbat(adata, *, reference_key: str | None = None, reference_cat: list[str] | None = None) -> dict:
    """Haplotype-aware CNV inference via R Numbat subprocess.

    Uses raw integer UMI counts from ``adata.layers["counts"]`` — Numbat's
    model explicitly requires a gene-by-cell integer UMI count matrix as its
    expression input.  Do NOT pass log-normalized data.

    Additionally requires:
      - ``adata.obsm["allele_counts"]``: phased allele counts DataFrame
        (from ``pileup_and_phase.R``) with columns cell/snp_id/CHROM/POS/AD/DP/GT/gene
      - Optional ``lambdas_ref``: gene x cell_type normalized reference expression

    Falls back to ``adata.X`` with a warning if no counts layer is available.
    """
    import tempfile
    import anndata as ad
    from pathlib import Path
    from omicsclaw.core.dependency_manager import validate_r_environment
    from omicsclaw.core.r_script_runner import RScriptRunner
    from omicsclaw.core.r_utils import read_r_result_csv

    validate_r_environment(required_r_packages=["numbat", "SingleCellExperiment", "zellkonverter"])

    if "allele_counts" not in adata.obsm:
        raise ValueError("Numbat requires allele count data in adata.obsm['allele_counts']")

    counts_layer = _get_counts_layer(adata)
    
    # Construct lightweight AnnData to avoid copying large unrelated layers/graphs/images
    if counts_layer is not None:
        logger.info("Numbat: using adata.layers['%s'] (raw integer counts)", counts_layer)
        export_X = adata.layers[counts_layer].copy()
    else:
        logger.warning(
            "Numbat: no 'counts' layer or adata.raw found; will use adata.X. "
            "If adata.X is log-normalized, Numbat results will be incorrect. "
            "Ensure preprocessing saves raw counts: adata.layers['counts'] = adata.X.copy()"
        )
        export_X = adata.X.copy()
        
    adata_export = ad.AnnData(
        X=export_X,
        obs=adata.obs.copy(),
        var=adata.var.copy(),
        obsm={"allele_counts": adata.obsm["allele_counts"].copy()}
    )
    logger.info("Numbat: prepared lightweight AnnData (dropped heavy uns/obsp arrays) for R export")

    scripts_dir = Path(__file__).resolve().parents[3] / "omicsclaw" / "r_scripts"
    runner = RScriptRunner(scripts_dir=scripts_dir)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_numbat_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "numbat_input.h5ad"
        adata_export.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        args = [str(input_path), str(output_dir)]
        if reference_key:
            args.append(reference_key)
        if reference_cat:
            args.append(",".join(reference_cat))

        logger.info("Spawning external R process for Numbat...")
        runner.run_script(
            "sp_numbat.R",
            args=args,
            expected_outputs=["numbat_results.csv"],
            output_dir=output_dir,
        )

        result_df = read_r_result_csv(output_dir / "numbat_results.csv", index_col=None)
        
        # Store results back into original adata safely
        if result_df is not None and not result_df.empty:
            adata.uns["numbat_calls"] = result_df.to_dict("records")
            logger.info("Successfully joined %d Numbat CNV segment calls", len(result_df))
        else:
            logger.warning("Numbat returned empty CNV calls")

    return {
        "method": "numbat",
        "n_genes": adata.n_vars,
        "mean_cnv_score": 0.0,
        "high_cnv_fraction_pct": 0.0,
        "n_cnv_calls": len(result_df) if result_df is not None and not result_df.empty else 0,
    }


def run_cnv(adata, *, method: str = "infercnvpy", reference_key: str | None = None,
            reference_cat: list[str] | str | None = None, window_size: int = 100, step: int = 10) -> dict:
    """Run CNV inference. Returns summary dict.

    Input matrix is selected per-method:
      - infercnvpy: adata.X (log-normalized)
      - numbat: adata.layers["counts"] (raw integer UMI counts)
    """
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"Unknown CNV method '{method}'. Choose from: {SUPPORTED_METHODS}")

    # Safely cast string to list for the reference categories to prevent iteration bugs
    if isinstance(reference_cat, str):
        reference_cat = [reference_cat]
    reference_cat = reference_cat or []

    validate_reference(adata, reference_key, reference_cat)

    logger.info("Starting CNV inference workflow using method '%s' (%d cells, %d genes)...", method, adata.n_obs, adata.n_vars)

    if method == "numbat":
        result = run_numbat(adata, reference_key=reference_key, reference_cat=reference_cat)
    elif method == "infercnvpy":
        result = run_infercnvpy(adata, reference_key=reference_key, reference_cat=reference_cat,
                                window_size=window_size, step=step)
    else:
        raise NotImplementedError(f"Handler for method '{method}' is not implemented.")
                                
    return {"n_cells": adata.n_obs, "n_genes": adata.n_vars, **result}
