"""Spatial cell-cell communication analysis functions.

Provides LIANA, CellPhoneDB, FastCCC, and CellChat (R) for ligand-receptor analysis.

Includes pathway-level aggregation and signaling role classification
(sender, receiver, mediator, influencer) from community best practices.

Input matrix convention:
  All four CCC methods use log-normalized expression (adata.X), NOT raw counts.
  These methods compute mean L-R co-expression scores, permutation statistics,
  or consensus rankings on continuous expression values.

  - liana:       adata.X (log-normalized); uses adata.raw if available for full gene set
  - cellphonedb: adata.X (log-normalized); do NOT use z-scored/scaled matrix
  - fastccc:     adata.X (log-normalized); standard CCC mode
  - cellchat_r:  adata.X (log-normalized); R CellChat requires normalized+log input

Usage::

    from skills.spatial._lib.communication import run_communication, SUPPORTED_METHODS
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import scanpy as sc

from .adata_utils import get_spatial_key
from .dependency_manager import require

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("liana", "cellphonedb", "fastccc", "cellchat_r")

# All CCC methods use log-normalized expression, not raw counts.
# This is because they compute mean expression scores or rank-based statistics
# on continuous values — not count-based probabilistic models.
NORMALIZED_METHODS = SUPPORTED_METHODS


def _run_liana(adata, *, cell_type_key: str = "leiden", species: str = "human", n_perms: int = 100) -> pd.DataFrame:
    """Run LIANA+ multi-method consensus ranking.

    Uses ``adata.X`` (log-normalized) for scoring.  When ``adata.raw`` is
    available, LIANA reads from it (``use_raw=True``) — in the scanpy
    convention ``adata.raw`` stores the **log-normalized full gene set**
    (before HVG filtering), not raw UMI counts.  This gives LIANA access
    to all genes for L-R matching while keeping log-normalized values.
    """
    li = require("liana", feature="LIANA+ cell communication")
    
    # LIANA defaults to human 'consensus'. For mouse, we explicitly use 'mouseconsensus'
    resource_name = "mouseconsensus" if species.lower() == "mouse" else "consensus"
    
    use_raw = adata.raw is not None
    logger.info("Running LIANA+ rank_aggregate on %s (n_perms=%d, resource=%s) ...",
                "adata.raw (log-normalized, full genes)" if use_raw else "adata.X (log-normalized)",
                n_perms, resource_name)
    
    li.mt.rank_aggregate(
        adata, 
        groupby=cell_type_key, 
        use_raw=use_raw, 
        n_perms=n_perms, 
        resource_name=resource_name,
        verbose=True
    )
    
    if "liana_res" not in adata.uns or adata.uns["liana_res"].empty:
        logger.warning("LIANA+ returned empty results. Check if %s L-R genes are expressed.", species)
        return pd.DataFrame(columns=["ligand", "receptor", "source", "target", "score", "pvalue"])
        
    df = adata.uns["liana_res"].copy()

    col_map = {}
    if "ligand_complex" in df.columns: col_map["ligand_complex"] = "ligand"
    if "receptor_complex" in df.columns: col_map["receptor_complex"] = "receptor"
    if "sender" in df.columns and "source" not in df.columns: col_map["sender"] = "source"
    if "receiver" in df.columns and "target" not in df.columns: col_map["receiver"] = "target"
    if col_map: df = df.rename(columns=col_map)

    # Invert magnitude rank (0 is best -> 1.0 is best) for consistent score interpretation
    if "magnitude_rank" in df.columns: df["score"] = 1.0 - df["magnitude_rank"]
    elif "lr_means" in df.columns: df["score"] = df["lr_means"]
    else: df["score"] = 0.0

    # specificity_rank aggregates cellphonedb p-values and others (0 is most specific)
    if "specificity_rank" in df.columns: df["pvalue"] = df["specificity_rank"]
    else: df["pvalue"] = 0.5

    for col in ["ligand", "receptor", "source", "target", "score", "pvalue"]:
        if col not in df.columns: df[col] = ""

    return df[["ligand", "receptor", "source", "target", "score", "pvalue"]].copy().sort_values("score", ascending=False).reset_index(drop=True)


def _run_cellphonedb(adata, *, cell_type_key: str = "leiden", species: str = "human", n_perms: int = 1000) -> pd.DataFrame:
    """Run CellPhoneDB statistical method.

    Uses ``adata.X`` (log-normalized) — CellPhoneDB requires log-normalized
    expression data for scoring interactions.  Do NOT pass z-scored or scaled
    matrices, as transforms that convert zeros to non-zero values will corrupt
    the interaction scoring (CellPhoneDB v5 docs explicitly warn about this).
    """
    cpdb = require("cellphonedb", feature="CellPhoneDB cell communication")
    from cellphonedb.src.core.methods import cpdb_statistical_analysis_method
    from pathlib import Path
    import tempfile as _tf

    if species != "human":
        raise ValueError("CellPhoneDB supports human data only.")

    cpdb_db_path = None
    try:
        import cellphonedb
        cpdb_pkg_dir = Path(cellphonedb.__file__).parent
        for candidate in [cpdb_pkg_dir / "src" / "core" / "data" / "cellphonedb.zip", cpdb_pkg_dir / "data" / "cellphonedb.zip"]:
            if candidate.exists():
                cpdb_db_path = str(candidate); break
    except Exception: pass

    with _tf.TemporaryDirectory(prefix="cpdb_") as tmp:
        tmp_path = Path(tmp)
        meta_df = pd.DataFrame({"Cell": adata.obs_names, "cell_type": adata.obs[cell_type_key].values})
        meta_df.to_csv(tmp_path / "meta.tsv", sep="\t", index=False)
        
        # Optimize memory during matrix extraction
        X_T = adata.X.T
        counts_df = pd.DataFrame(X_T.toarray() if hasattr(X_T, "toarray") else X_T, index=adata.var_names, columns=adata.obs_names)
        counts_df.to_csv(tmp_path / "counts.tsv", sep="\t")

        logger.info("Running CellPhoneDB statistical analysis (n_perms=%d, outdir=%s)...", n_perms, tmp_path)
        result = cpdb_statistical_analysis_method.call(
            cpdb_file_path=cpdb_db_path, meta_file_path=str(tmp_path / "meta.tsv"),
            counts_file_path=str(tmp_path / "counts.tsv"), counts_data="hgnc_symbol",
            output_path=str(tmp_path), iterations=n_perms, threshold=0.1,
            threads=4
        )

    # Robust parsing of output, handling both legacy tuple and modern dict returns
    if isinstance(result, tuple):
        means_df = result[1] if len(result) > 1 else None
        pvalues_df = result[2] if len(result) > 2 else None
    elif isinstance(result, dict):
        means_df = result.get("means_result", result.get("means"))
        pvalues_df = result.get("pvalues_result", result.get("pvalues"))
    else:
        means_df = None
        pvalues_df = None
        
    if means_df is None or means_df.empty:
        logger.warning("CellPhoneDB returned empty results. No interactions met the minimum expression threshold.")
        return pd.DataFrame(columns=["ligand", "receptor", "source", "target", "score", "pvalue"])

    records = []
    for _, row in means_df.iterrows():
        pair = str(row.get("interacting_pair", ""))
        # Modern CellPhoneDB sets use '_' for ligand-receptor pairs, older versions used '|'
        parts = pair.split("_") if "_" in pair else pair.split("|")
        ligand, receptor = (parts[0] if len(parts) >= 1 else pair), (parts[1] if len(parts) >= 2 else "")
        
        # Dynamically identify cell type pair columns (e.g., 'T_cell|B_cell') to cleanly bypass prepended metadata
        for col in means_df.columns:
            if "|" not in col or col == "interacting_pair":
                continue
                
            src_tgt = str(col).split("|")
            if len(src_tgt) != 2:
                continue
                
            score = float(row.get(col, 0) or 0)
            if score < 1e-6: 
                continue
                
            source, target = src_tgt[0], src_tgt[1]
            pval = float(pvalues_df.loc[row.name, col]) if pvalues_df is not None and col in pvalues_df.columns and row.name in pvalues_df.index else 1.0
            
            records.append({
                "ligand": ligand, "receptor": receptor, "source": source, "target": target, 
                "score": float(f"{score:.4f}"), "pvalue": float(f"{pval:.4f}")
            })

    df = pd.DataFrame(records)
    return df.sort_values("score", ascending=False).reset_index(drop=True) if not df.empty else df


def _run_fastccc(adata, *, cell_type_key: str = "leiden", species: str = "human") -> pd.DataFrame:
    """Run FastCCC — FFT-based communication without permutation testing.

    Uses ``adata.X`` (log-normalized) in standard CCC mode — FastCCC
    benchmarks use the same log-transformed data as CellPhoneDB for
    comparable scoring.  In reference-based mode (not yet implemented here),
    query input could be raw counts with internal rank-based preprocessing.
    """
    require("fastccc", feature="FastCCC cell communication")
    import fastccc
    
    if species != "human": 
        raise ValueError("FastCCC currently supports human data only.")
        
    logger.info("Running FastCCC analysis (FFT-based, no permutations)...")
    try:
        result = fastccc.run(adata, groupby=cell_type_key)
    except Exception as e:
        logger.error("FastCCC execution failed: %s", e)
        return pd.DataFrame(columns=["ligand", "receptor", "source", "target", "score", "pvalue"])

    if result is None or (hasattr(result, "empty") and result.empty):
        logger.warning("FastCCC returned empty results.")
        return pd.DataFrame(columns=["ligand", "receptor", "source", "target", "score", "pvalue"])
        
    df = pd.DataFrame(result)
    
    # Map vendor-specific columns to OmicsClaw standardized keys
    col_map = {"ligand_complex": "ligand", "receptor_complex": "receptor", "sender": "source", "receiver": "target"}
    for old, new in col_map.items():
        if old in df.columns and new not in df.columns: 
            df = df.rename(columns={old: new})
            
    df["score"] = df.get("lr_mean", df.get("score", 0.0))
    df["pvalue"] = df.get("pvalue", 0.0)
    
    for col in ["ligand", "receptor", "source", "target", "score", "pvalue"]:
        if col not in df.columns: 
            df[col] = ""
            
    # Safely cast metrics to standard datatypes to prevent downstream schema breaks
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0).round(4)
    df["pvalue"] = pd.to_numeric(df["pvalue"], errors="coerce").fillna(1.0).round(4)
    
    return df[["ligand", "receptor", "source", "target", "score", "pvalue"]].copy().sort_values("score", ascending=False).reset_index(drop=True)


def _run_cellchat_r(adata, *, cell_type_key: str = "leiden", species: str = "human") -> pd.DataFrame:
    """Run CellChat via R subprocess (requires R package CellChat).

    Uses ``adata.X`` (log-normalized) — CellChat requires "normalized data
    (library-size normalization and then log-transformed)" as documented in
    its official tutorial.  The ``raw.use=TRUE`` parameter in CellChat's
    ``computeCommunProb()`` refers to the signaling gene subset within
    CellChat's internal object, NOT to raw UMI counts.

    The R script computes centrality metrics (sender, receiver, mediator,
    influencer) via ``netAnalysis_computeCentrality()`` and exports pathway-
    level aggregated results alongside L-R pair interactions.
    """
    import tempfile
    from pathlib import Path
    from omicsclaw.core.dependency_manager import validate_r_environment
    from omicsclaw.core.r_script_runner import RScriptRunner
    from omicsclaw.core.r_utils import read_r_result_csv

    validate_r_environment(required_r_packages=["CellChat", "SingleCellExperiment", "zellkonverter"])

    scripts_dir = Path(__file__).resolve().parents[3] / "omicsclaw" / "r_scripts"
    runner = RScriptRunner(scripts_dir=scripts_dir)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_cellchat_sp_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_cellchat.R",
            args=[str(input_path), str(output_dir), cell_type_key, species],
            expected_outputs=["cellchat_results.csv"],
            output_dir=output_dir,
        )

        df = read_r_result_csv(output_dir / "cellchat_results.csv", index_col=None)

    if df.empty:
        return pd.DataFrame(columns=["ligand", "receptor", "source", "target", "score", "pvalue"])

    col_map = {"prob": "score", "pval": "pvalue"}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns and v != k})
    df["score"] = df.get("score", 0.0)
    df["pvalue"] = df.get("pvalue", 0.5)
    for col in ["ligand", "receptor", "source", "target", "score", "pvalue"]:
        if col not in df.columns:
            df[col] = ""
    return df[["ligand", "receptor", "source", "target", "score", "pvalue"]].copy()


def aggregate_by_pathway(lr_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate L-R interactions by signaling pathway.

    Groups interactions by source-target cell type pairs and computes
    pathway-level statistics: total interaction count, mean score,
    and top ligand-receptor pair per pathway.

    Returns a DataFrame with columns: source, target, n_interactions,
    mean_score, top_ligand, top_receptor.
    """
    if lr_df.empty or "source" not in lr_df.columns or "target" not in lr_df.columns:
        return pd.DataFrame()

    grouped = lr_df.groupby(["source", "target"], observed=True)
    records = []
    for (src, tgt), grp in grouped:
        best = grp.loc[grp["score"].idxmax()] if "score" in grp.columns and not grp["score"].isna().all() else grp.iloc[0]
        records.append({
            "source": src,
            "target": tgt,
            "n_interactions": len(grp),
            "mean_score": float(grp["score"].mean()) if "score" in grp.columns else 0.0,
            "top_ligand": best.get("ligand", ""),
            "top_receptor": best.get("receptor", ""),
        })

    return pd.DataFrame(records).sort_values("mean_score", ascending=False).reset_index(drop=True)


def classify_signaling_roles(lr_df: pd.DataFrame) -> pd.DataFrame:
    """Classify each cell type's signaling role.

    Computes four role scores per cell type:
    - **Sender**: Total outgoing interaction strength (sum of scores as source)
    - **Receiver**: Total incoming interaction strength (sum of scores as target)
    - **Hub**: Combined sender + receiver (highly connected)
    - **Dominant role**: 'sender', 'receiver', or 'balanced'

    Returns a DataFrame with columns: cell_type, sender_score, receiver_score,
    hub_score, dominant_role, n_outgoing, n_incoming.
    """
    if lr_df.empty:
        return pd.DataFrame()

    all_types = set()
    if "source" in lr_df.columns:
        all_types.update(lr_df["source"].unique())
    if "target" in lr_df.columns:
        all_types.update(lr_df["target"].unique())

    records = []
    for ct in sorted(all_types, key=str):
        out_mask = lr_df["source"] == ct if "source" in lr_df.columns else pd.Series(False, index=lr_df.index)
        in_mask = lr_df["target"] == ct if "target" in lr_df.columns else pd.Series(False, index=lr_df.index)

        sender_score = float(lr_df.loc[out_mask, "score"].sum()) if "score" in lr_df.columns else 0.0
        receiver_score = float(lr_df.loc[in_mask, "score"].sum()) if "score" in lr_df.columns else 0.0
        n_out = int(out_mask.sum())
        n_in = int(in_mask.sum())
        hub_score = sender_score + receiver_score

        if sender_score > receiver_score * 1.5:
            role = "sender"
        elif receiver_score > sender_score * 1.5:
            role = "receiver"
        else:
            role = "balanced"

        records.append({
            "cell_type": str(ct),
            "sender_score": round(sender_score, 4),
            "receiver_score": round(receiver_score, 4),
            "hub_score": round(hub_score, 4),
            "dominant_role": role,
            "n_outgoing": n_out,
            "n_incoming": n_in,
        })

    return pd.DataFrame(records).sort_values("hub_score", ascending=False).reset_index(drop=True)


def run_communication(adata, *, method: str = "liana", cell_type_key: str = "leiden", species: str = "human", n_perms: int = 100) -> dict:
    """Run cell-cell communication analysis.

    All four methods use ``adata.X`` (log-normalized expression).  Do not pass
    raw counts or z-scored matrices.  Cell type labels must be present in
    ``adata.obs[cell_type_key]``.
    """
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"Unknown method '{method}'. Choose from: {SUPPORTED_METHODS}")
    if cell_type_key not in adata.obs.columns:
        raise ValueError(f"Cell type key '{cell_type_key}' not in adata.obs")

    n_cells, n_genes = adata.n_obs, adata.n_vars
    cell_types = sorted(adata.obs[cell_type_key].unique().tolist(), key=str)

    dispatch = {
        "liana": lambda: _run_liana(adata, cell_type_key=cell_type_key, species=species, n_perms=n_perms),
        "cellphonedb": lambda: _run_cellphonedb(adata, cell_type_key=cell_type_key, species=species, n_perms=n_perms),
        "fastccc": lambda: _run_fastccc(adata, cell_type_key=cell_type_key, species=species),
        "cellchat_r": lambda: _run_cellchat_r(adata, cell_type_key=cell_type_key, species=species),
    }
    lr_df = dispatch[method]()
    sig_df = lr_df[lr_df["pvalue"] < 0.05] if not lr_df.empty else lr_df

    # Pathway-level aggregation and signaling role classification
    pathway_df = aggregate_by_pathway(sig_df if not sig_df.empty else lr_df)
    roles_df = classify_signaling_roles(sig_df if not sig_df.empty else lr_df)

    return {
        "n_cells": n_cells, "n_genes": n_genes, "n_cell_types": len(cell_types),
        "cell_types": cell_types, "cell_type_key": cell_type_key, "method": method,
        "species": species, "n_interactions_tested": len(lr_df), "n_significant": len(sig_df),
        "lr_df": lr_df, "top_df": lr_df.head(50) if not lr_df.empty else lr_df,
        "pathway_df": pathway_df, "signaling_roles_df": roles_df,
    }
