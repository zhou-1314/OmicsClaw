"""Lightweight kernel helpers for sc-filter (QC threshold & doublet cell/gene filtering)."""
from __future__ import annotations
__all__ = ['filter_cells', 'retention_summary', 'tissue_presets']

def tissue_presets() -> dict[str, dict[str, float | int]]:
    """Return standard tissue-specific QC filtering presets."""
    return {'pbmc': {'min_genes': 200, 'max_genes': 2500, 'max_mt_percent': 5.0}, 'brain': {'min_genes': 200, 'max_genes': 6000, 'max_mt_percent': 5.0}, 'tumor': {'min_genes': 200, 'max_genes': 7500, 'max_mt_percent': 20.0}, 'default': {'min_genes': 200, 'max_genes': 5000, 'max_mt_percent': 20.0}}

def filter_cells(adata, *, min_genes: int=200, max_genes: int | None=None, min_counts: int | None=None, max_counts: int | None=None, max_mt_percent: float | None=20.0, min_cells: int=3, remove_doublets: bool=True):
    """Filter low-quality cells, putative doublets, and lowly expressed genes."""
    import scanpy as sc
    ad = adata.copy()
    if 'n_genes_by_counts' not in ad.obs or 'pct_counts_mt' not in ad.obs:
        ad.var['mt'] = ad.var_names.str.startswith(('MT-', 'mt-'))
        sc.pp.calculate_qc_metrics(ad, qc_vars=['mt'], inplace=True)
    mask = ad.obs['n_genes_by_counts'] >= min_genes
    if max_genes is not None:
        mask &= ad.obs['n_genes_by_counts'] <= max_genes
    if min_counts is not None:
        mask &= ad.obs['total_counts'] >= min_counts
    if max_counts is not None:
        mask &= ad.obs['total_counts'] <= max_counts
    if max_mt_percent is not None and 'pct_counts_mt' in ad.obs:
        mask &= ad.obs['pct_counts_mt'] < max_mt_percent
    if remove_doublets and 'predicted_doublet' in ad.obs:
        mask &= ~ad.obs['predicted_doublet'].astype(bool)
    ad = ad[mask].copy()
    if min_cells > 0:
        sc.pp.filter_genes(ad, min_cells=min_cells)
    return ad

def retention_summary(before, after) -> pd.DataFrame:
    """Summarize cell and gene retention counts and percentages."""
    import pandas as pd
    return pd.DataFrame([{'feature': 'cells', 'before': int(before.n_obs), 'after': int(after.n_obs), 'retained_pct': 100.0 * after.n_obs / max(before.n_obs, 1)}, {'feature': 'genes', 'before': int(before.n_vars), 'after': int(after.n_vars), 'retained_pct': 100.0 * after.n_vars / max(before.n_vars, 1)}])
