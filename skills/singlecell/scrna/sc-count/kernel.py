"""Lightweight kernel helpers for sc-count (importing and canonicalizing single-cell count matrices)."""
from __future__ import annotations
__all__ = ['load_count_matrix', 'canonicalize_counts', 'count_summary']

def load_count_matrix(path: str, *, gex_only: bool=True):
    """Load a 10x H5, 10x MTX directory, or H5AD count output into AnnData."""
    import pathlib, scanpy as sc
    p = pathlib.Path(path)
    if p.is_dir():
        ad = sc.read_10x_mtx(p, var_names='gene_symbols', gex_only=gex_only)
    elif p.suffix == '.h5':
        ad = sc.read_10x_h5(p, gex_only=gex_only)
    else:
        ad = sc.read_h5ad(p)
    ad.var_names_make_unique()
    ad.obs_names_make_unique()
    return ad

def canonicalize_counts(adata, *, sample_id: str | None=None):
    """Ensure raw counts are stored in layers['counts'] and adata.raw with unique identifiers."""
    ad = adata.copy()
    ad.var_names_make_unique()
    ad.obs_names_make_unique()
    if sample_id is not None:
        ad.obs['sample_id'] = str(sample_id)
    ad.layers['counts'] = ad.X.copy()
    ad.raw = ad.copy()
    return ad

def count_summary(adata) -> dict:
    """Return total barcodes, features, median UMIs, and median detected genes."""
    import numpy as np
    mat = adata.layers['counts'] if 'counts' in adata.layers else adata.X
    umis = np.asarray(mat.sum(axis=1)).ravel()
    genes = np.asarray((mat > 0).sum(axis=1)).ravel()
    return {'n_cells': int(adata.n_obs), 'n_genes': int(adata.n_vars), 'median_umis': float(np.median(umis)) if len(umis) else 0.0, 'median_genes': float(np.median(genes)) if len(genes) else 0.0}
