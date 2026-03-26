"""Spatial enrichment analysis functions.

Provides enrichR, GSEA, and ssGSEA for pathway enrichment analysis.

Supports multiple ranking metrics for GSEA (scores, logfoldchanges, stat),
multiple database categories, and leading edge gene extraction.

Usage::

    from skills.spatial._lib.enrichment import run_enrichment, SUPPORTED_METHODS
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import scanpy as sc

from .dependency_manager import require

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("enrichr", "gsea", "ssgsea")

# Ranking metric preference order: test statistic > scores > logFC
RANKING_METRICS = ("scores", "logfoldchanges", "stat")

_GENESET_LIBRARY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "GO_Biological_Process": ("GO_Biological_Process_2025", "GO_Biological_Process_2023"),
    "GO_Molecular_Function": ("GO_Molecular_Function_2025", "GO_Molecular_Function_2023"),
    "GO_Cellular_Component": ("GO_Cellular_Component_2025", "GO_Cellular_Component_2023"),
    "KEGG_Pathways": ("KEGG_2021_Human",),
    "Reactome_Pathways": ("Reactome_Pathways_2024", "Reactome_2022"),
    "MSigDB_Hallmark": ("MSigDB_Hallmark_2020",),
    "MSigDB_Oncogenic": ("MSigDB_Oncogenic_Signatures",),
    "MSigDB_Immunologic": ("MSigDB_Immunologic_Signatures",),
}


def resolve_library(key: str, organism: str = "human") -> dict:
    """Load first available library variant from gseapy."""
    import gseapy as gp
    last_err = None
    if key in _GENESET_LIBRARY_CANDIDATES:
        for lib in _GENESET_LIBRARY_CANDIDATES[key]:
            try:
                return gp.get_library(lib, organism=organism)
            except Exception as e:
                last_err = e
        logger.warning(f"Could not load mapped gene set candidates for '{key}', falling back to explicit name. Error: {last_err}")
    
    try:
        return gp.get_library(key, organism=organism)
    except Exception as e:
        raise RuntimeError(f"Could not resolve gene library '{key}' for organism '{organism}': {e}")


def _ensure_ranked_genes(adata, groupby: str, n_genes: int | None = None) -> pd.DataFrame:
    """Ensure rank_genes_groups is calculated to prevent redundant massive computations."""
    params = adata.uns.get("rank_genes_groups", {}).get("params", {})
    if params.get("groupby") != groupby:
        logger.info("Computing Wilcoxon rank_genes_groups for metadata column: '%s'...", groupby)
        sc.tl.rank_genes_groups(adata, groupby=groupby, method="wilcoxon", n_genes=n_genes)
    else:
        logger.info("Reusing existing rank_genes_groups cache for '%s'", groupby)
        
    return sc.get.rank_genes_groups_df(adata, group=None)


def run_enrichr(adata, *, groupby: str = "leiden", source: str = "GO_Biological_Process",
                species: str = "human", n_top_genes: int = 100) -> pd.DataFrame:
    """Run per-cluster Enrichr via gseapy."""
    require("gseapy", feature="pathway enrichment (Enrichr)")
    import gseapy as gp

    markers_df = _ensure_ranked_genes(adata, groupby, n_genes=n_top_genes)
    candidates = _GENESET_LIBRARY_CANDIDATES.get(source, (source,))
    lib_name = candidates[0]

    all_records = []
    groups = sorted(markers_df["group"].unique().tolist(), key=str)
    logger.info("Running EnrichR across %d groups using library '%s'...", len(groups), lib_name)
    
    for grp in groups:
        gene_list = markers_df[markers_df["group"] == grp].head(n_top_genes)["names"].tolist()
        if not gene_list: continue
        try:
            enr = gp.enrichr(gene_list=gene_list, gene_sets=lib_name, organism=species, outdir=None, no_plot=True)
            res = enr.results.copy() if hasattr(enr, 'results') else enr.res2d.copy()
            res["cluster"] = str(grp)
            all_records.append(res)
        except Exception as exc:
            logger.warning("Enrichr failed for cluster %s: %s", grp, exc)

    if all_records:
        df = pd.concat(all_records, ignore_index=True)
        col_map = {"Term": "gene_set", "Adjusted P-value": "pvalue_adj", "P-value": "pvalue",
                   "Genes": "genes", "Overlap": "overlap"}
        return df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return pd.DataFrame()


def run_gsea(adata, *, groupby: str = "leiden", source: str = "MSigDB_Hallmark",
             species: str = "human", ranking_metric: str = "scores") -> pd.DataFrame:
    """Run GSEA pre-ranked via gseapy."""
    require("gseapy", feature="GSEA")
    import gseapy as gp

    markers_df = _ensure_ranked_genes(adata, groupby)
    gene_sets = resolve_library(source, organism=species)

    # Resolve ranking metric with fallback
    metric = ranking_metric
    if metric not in markers_df.columns:
        for fallback in RANKING_METRICS:
            if fallback in markers_df.columns:
                metric = fallback
                break
    logger.info("GSEA ranking metric chosen: '%s'", metric)

    all_records = []
    groups = sorted(markers_df["group"].unique().tolist(), key=str)
    logger.info("Running pre-ranked GSEA across %d groups...", len(groups))
    
    for grp in groups:
        grp_df = markers_df[markers_df["group"] == grp].dropna(subset=[metric])
        rnk = grp_df.set_index("names")[metric].sort_values(ascending=False)
        if len(rnk) < 10: continue
        try:
            pre_res = gp.prerank(rnk=rnk, gene_sets=gene_sets, min_size=5, max_size=1000,
                                 permutation_num=100, outdir=None, seed=42, verbose=False)
            res = pre_res.res2d.copy()
            res["cluster"] = str(grp)
            res = res.rename(columns={"Term": "gene_set", "NES": "nes", "NOM p-val": "pvalue", "FDR q-val": "pvalue_adj"})

            # Extract leading edge genes
            if "Lead_genes" in res.columns:
                res["leading_edge"] = res["Lead_genes"]
            elif "lead_genes" in res.columns:
                res["leading_edge"] = res["lead_genes"]

            all_records.append(res)
        except Exception as exc:
            logger.warning("GSEA failed for cluster %s: %s", grp, exc)

    return pd.concat(all_records, ignore_index=True) if all_records else pd.DataFrame()


def run_ssgsea(adata, *, groupby: str = "leiden", source: str = "MSigDB_Hallmark",
               species: str = "human") -> pd.DataFrame:
    """Run ssGSEA on cluster pseudobulks via gseapy to prevent excessive memory/time."""
    require("gseapy", feature="ssGSEA")
    import gseapy as gp
    from scipy import sparse

    gene_sets = resolve_library(source, organism=species)
    
    logger.info("Computing pseudobulk metrics for ssGSEA by '%s' to prevent RAM OOM...", groupby)
    df_list = []
    groups = adata.obs[groupby].unique()
    
    for cluster in groups:
        mask = adata.obs[groupby] == cluster
        X_sub = adata.X[mask]
        mean_expr = np.asarray(X_sub.mean(axis=0)).flatten()
        df_list.append(pd.Series(mean_expr, index=adata.var_names, name=str(cluster)))
        
    expr_df = pd.concat(df_list, axis=1)

    logger.info("Running gseapy.ssgsea on dense pseudobulk matrix (genes=%d, clusters=%d)...", expr_df.shape[0], expr_df.shape[1])
    ss = gp.ssgsea(data=expr_df, gene_sets=gene_sets, outdir=None, no_plot=True, min_size=5, max_size=1000)
    
    res = ss.res2d.copy()
    
    # gseapy ssgsea res2d returns a matrix of (Term x Samples) or similar, we must melt to tidy format
    if "Term" in res.columns:
        res = res.set_index("Term")
        
    res = res.reset_index().melt(id_vars=res.index.name or "Term", var_name="cluster", value_name="score")
    res = res.rename(columns={res.columns[0]: "gene_set"})
    
    res["pvalue"] = float("nan")
    res["pvalue_adj"] = float("nan")
    
    return res


def run_enrichment(adata, *, method: str = "enrichr", groupby: str = "leiden",
                   source: str = "GO_Biological_Process", species: str = "human",
                   gene_set: str | None = None) -> dict:
    """Run pathway enrichment analysis."""
    require("gseapy", feature="pathway enrichment")

    if method not in SUPPORTED_METHODS:
        raise ValueError(f"Unknown method '{method}'. Choose from: {SUPPORTED_METHODS}")
    if groupby not in adata.obs.columns:
        raise ValueError(f"groupby column '{groupby}' not found in adata.obs")

    effective_source = gene_set or source
    dispatch = {
        "enrichr": lambda: run_enrichr(adata, groupby=groupby, source=effective_source, species=species),
        "gsea": lambda: run_gsea(adata, groupby=groupby, source=effective_source, species=species),
        "ssgsea": lambda: run_ssgsea(adata, groupby=groupby, source=effective_source, species=species),
    }
    logger.info("Dispatching %s algorithm (source=%s, species=%s)", method.upper(), effective_source, species)
    enrich_df = dispatch[method]()
    
    n_sig = int(enrich_df["pvalue_adj"].dropna().lt(0.05).sum()) if not enrich_df.empty else 0

    return {
        "n_cells": adata.n_obs, "n_genes": adata.n_vars,
        "n_clusters": int(adata.obs[groupby].nunique()),
        "method": method, "source": source, "groupby": groupby,
        "n_terms_tested": len(enrich_df.dropna(subset=['cluster'])) if not enrich_df.empty else 0, 
        "n_significant": n_sig,
        "enrich_df": enrich_df,
    }
