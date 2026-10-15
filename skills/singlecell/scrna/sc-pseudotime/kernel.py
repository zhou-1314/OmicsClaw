"""Lightweight kernel helpers for sc-pseudotime (DPT ordering & trajectory-correlated genes)."""
from __future__ import annotations
__all__ = ['pseudotime', 'trajectory_genes', 'pseudotime_table']

def pseudotime(adata, *, method: str='dpt', cluster_key: str='leiden', use_rep: str | None=None, root_cluster: str | None=None, root_cell: str | int | None=None, n_neighbors: int=15, n_pcs: int=50, n_dcs: int=10):
    """Return an AnnData copy annotated with obs['pseudotime'] via Scanpy DPT."""
    import numpy as np, scanpy as sc
    ad = adata.copy()
    rep = use_rep or ('X_pca' if 'X_pca' in ad.obsm else None)
    if rep is None:
        sc.tl.pca(ad, n_comps=max(2, min(n_pcs, ad.n_obs - 1, ad.n_vars - 1)))
        rep = 'X_pca'
    pcs = min(n_pcs, ad.obsm[rep].shape[1]) if rep == 'X_pca' else None
    sc.pp.neighbors(ad, n_neighbors=min(n_neighbors, ad.n_obs - 1), n_pcs=pcs, use_rep=rep)
    sc.tl.diffmap(ad, n_comps=max(2, min(n_dcs, ad.n_obs - 2)))
    if isinstance(root_cell, int):
        ad.uns['iroot'] = int(root_cell)
    elif isinstance(root_cell, str) and root_cell in ad.obs_names:
        ad.uns['iroot'] = int(ad.obs_names.get_loc(root_cell))
    elif root_cluster is not None and cluster_key in ad.obs.columns:
        idx = np.flatnonzero(ad.obs[cluster_key].astype(str) == str(root_cluster))
        if len(idx) == 0:
            raise ValueError(f"root_cluster '{root_cluster}' not found in adata.obs['{cluster_key}'].")
        dm = ad.obsm['X_diffmap'][idx]
        ad.uns['iroot'] = int(idx[np.argmin(np.linalg.norm(dm - dm.mean(axis=0), axis=1))])
    else:
        ad.uns['iroot'] = 0
    sc.tl.dpt(ad, n_dcs=max(2, min(n_dcs, ad.obsm['X_diffmap'].shape[1])))
    ad.obs['pseudotime'] = ad.obs['dpt_pseudotime'].astype(float)
    return ad

def trajectory_genes(adata, *, n_genes: int=50, method: str='pearson') -> pd.DataFrame:
    """Rank genes by correlation with obs['pseudotime'] (pearson or spearman)."""
    import numpy as np, pandas as pd
    from scipy import sparse
    from scipy.stats import rankdata
    pt = np.asarray(adata.obs['pseudotime'], dtype=float)
    finite = np.isfinite(pt)
    pt = pt[finite]
    mat = adata.X[finite].toarray().astype(float) if sparse.issparse(adata.X) else np.asarray(adata.X[finite], dtype=float)
    if method == 'spearman':
        pt = rankdata(pt)
        mat = np.apply_along_axis(rankdata, 0, mat)
    pt_c = pt - pt.mean()
    mat_c = mat - mat.mean(axis=0, keepdims=True)
    denom = np.maximum(np.linalg.norm(pt_c) * np.linalg.norm(mat_c, axis=0), 1e-12)
    corrs = pt_c @ mat_c / denom
    df = pd.DataFrame({'gene': adata.var_names.astype(str), 'correlation': corrs, 'abs_corr': np.abs(corrs)})
    return df.sort_values('abs_corr', ascending=False, ignore_index=True).drop(columns=['abs_corr']).head(n_genes)

def pseudotime_table(adata, *, cluster_key: str='leiden') -> pd.DataFrame:
    """Return per-cell pseudotime and cluster group table."""
    cols = [c for c in (cluster_key, 'pseudotime') if c in adata.obs.columns]
    return adata.obs[cols].copy().reset_index().rename(columns={'index': 'cell'})
