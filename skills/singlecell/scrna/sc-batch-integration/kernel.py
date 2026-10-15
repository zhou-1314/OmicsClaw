"""Lightweight kernel helpers for sc-batch-integration (Harmony / BBKNN / Scanorama integration)."""
from __future__ import annotations
__all__ = ['integrate_batches', 'batch_mixing_summary']

def integrate_batches(adata, *, batch_key: str='batch', method: str='harmony', n_pcs: int=30, n_neighbors: int=15, random_state: int=0):
    """Integrate multi-sample AnnData across batch_key and rebuild neighbor graph + UMAP."""
    import scanpy as sc, scanpy.external as sce
    ad = adata.copy()
    ad.obs[batch_key] = ad.obs[batch_key].astype(str).astype('category')
    if 'X_pca' not in ad.obsm:
        sc.pp.highly_variable_genes(ad, n_top_genes=min(2000, ad.n_vars), batch_key=batch_key)
        sc.tl.pca(ad, n_comps=min(n_pcs, ad.n_obs - 1, ad.n_vars - 1), random_state=random_state)
    if method == 'harmony':
        sce.pp.harmony_integrate(ad, key=batch_key, basis='X_pca', adjusted_basis='X_pca_harmony')
        sc.pp.neighbors(ad, use_rep='X_pca_harmony', n_neighbors=n_neighbors, random_state=random_state)
    elif method == 'bbknn':
        sce.pp.bbknn(ad, batch_key=batch_key, n_pcs=min(n_pcs, ad.obsm['X_pca'].shape[1]))
    elif method == 'scanorama':
        ad = ad[ad.obs[batch_key].argsort()].copy()
        sce.pp.scanorama_integrate(ad, key=batch_key, basis='X_pca', adjusted_basis='X_scanorama')
        sc.pp.neighbors(ad, use_rep='X_scanorama', n_neighbors=n_neighbors, random_state=random_state)
    else:
        raise ValueError(f'Unsupported integration method: {method}')
    sc.tl.umap(ad, random_state=random_state)
    return ad

def batch_mixing_summary(adata, *, batch_key: str='batch', cluster_key: str='leiden') -> pd.DataFrame:
    """Return a cluster-by-batch contingency table to inspect batch mixing."""
    import pandas as pd
    if cluster_key not in adata.obs or batch_key not in adata.obs:
        return pd.DataFrame({'batch': adata.obs[batch_key].value_counts()})
    return pd.crosstab(adata.obs[cluster_key], adata.obs[batch_key])
