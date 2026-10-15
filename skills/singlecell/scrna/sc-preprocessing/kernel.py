"""Lightweight kernel helpers for sc-preprocessing (Scanpy QC + log1p + HVG + PCA)."""
from __future__ import annotations
__all__ = ['preprocess', 'qc_summary']

def preprocess(adata, *, min_genes: int=200, min_cells: int=3, max_mt_pct: float=20.0, n_top_hvg: int=2000, n_pcs: int=30, species: str='human'):
    """Standard single-cell QC, count-layer preservation, log1p normalization, HVG, and PCA."""
    import scanpy as sc
    ad = adata.copy()
    mt_prefix = 'MT-' if species.lower() == 'human' else 'mt-'
    ad.var['mt'] = ad.var_names.str.startswith(mt_prefix)
    sc.pp.calculate_qc_metrics(ad, qc_vars=['mt'], inplace=True)
    if min_genes > 0:
        sc.pp.filter_cells(ad, min_genes=min_genes)
    if min_cells > 0:
        sc.pp.filter_genes(ad, min_cells=min_cells)
    if max_mt_pct < 100.0 and 'pct_counts_mt' in ad.obs:
        ad = ad[ad.obs['pct_counts_mt'] < max_mt_pct].copy()
    ad.layers['counts'] = ad.X.copy()
    sc.pp.normalize_total(ad, target_sum=10000.0)
    sc.pp.log1p(ad)
    sc.pp.highly_variable_genes(ad, n_top_genes=min(n_top_hvg, ad.n_vars), layer='counts', flavor='seurat_v3')
    sc.tl.pca(ad, n_comps=min(n_pcs, ad.n_obs - 1, ad.n_vars - 1))
    return ad

def qc_summary(adata) -> dict:
    """Return cell/gene counts and median QC metrics."""
    return {'n_cells': int(adata.n_obs), 'n_genes': int(adata.n_vars)}
