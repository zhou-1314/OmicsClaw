"""Lightweight kernel helpers for spatial-raw-processing (barcode-coordinate AnnData assembly & QC)."""
from __future__ import annotations
__all__ = ['build_spatial_anndata', 'raw_qc_summary']

def build_spatial_anndata(counts_df: pd.DataFrame, coords_df: pd.DataFrame) -> ad.AnnData:
    """Align spot/barcode counts with (x, y) spatial coordinates into a CSR raw_counts AnnData."""
    import anndata as ad, numpy as np, pandas as pd
    from scipy import sparse
    common = counts_df.index.intersection(coords_df.index)
    if len(common) == 0:
        raise ValueError('No overlapping barcodes between counts_df and coords_df.')
    c_sub = counts_df.loc[common]
    xy_sub = coords_df.loc[common, ['x', 'y']].astype(float)
    mat = sparse.csr_matrix(c_sub.to_numpy(dtype=np.float32))
    adata = ad.AnnData(X=mat, obs=pd.DataFrame(index=common.astype(str)), var=pd.DataFrame(index=c_sub.columns.astype(str)))
    adata.layers['counts'] = mat.copy()
    adata.obsm['spatial'] = xy_sub.to_numpy(dtype=float)
    adata.obs['x'] = xy_sub['x'].to_numpy()
    adata.obs['y'] = xy_sub['y'].to_numpy()
    adata.obs['total_counts'] = np.asarray(mat.sum(axis=1)).ravel()
    adata.obs['n_genes_by_counts'] = np.asarray((mat > 0).sum(axis=1)).ravel()
    return adata

def raw_qc_summary(adata: ad.AnnData) -> dict:
    """Summarize barcode count, gene count, and per-spot UMI statistics for raw spatial data."""
    import numpy as np
    tc = np.asarray(adata.X.sum(axis=1)).ravel()
    ng = np.asarray((adata.X > 0).sum(axis=1)).ravel()
    return {'n_spots': int(adata.n_obs), 'n_genes': int(adata.n_vars), 'total_counts': int(tc.sum()), 'median_counts_per_spot': float(np.median(tc)) if len(tc) else 0.0, 'median_genes_per_spot': float(np.median(ng)) if len(ng) else 0.0}
