"""Lightweight kernel helpers for spatial-statistics (Squidpy neighborhood enrichment & centrality)."""
from __future__ import annotations
__all__ = ['nhood_enrichment_table', 'spatial_centrality_table']

def nhood_enrichment_table(adata, *, cluster_key: str='leiden', n_perms: int=100, seed: int=0, library_key: str | None=None) -> pd.DataFrame:
    """Compute Squidpy neighborhood enrichment permutation z-scores and return a tidy pair table."""
    import numpy as np, pandas as pd, squidpy as sq
    ad = adata.copy()
    ad.obs[cluster_key] = ad.obs[cluster_key].astype(str).astype('category')
    if 'spatial_connectivities' not in ad.obsp:
        sq.gr.spatial_neighbors(ad, coord_type='generic', delaunay=True, library_key=library_key)
    sq.gr.nhood_enrichment(ad, cluster_key=cluster_key, n_perms=n_perms, seed=seed, show_progress_bar=False)
    cats = list(ad.obs[cluster_key].cat.categories)
    zmat = np.asarray(ad.uns[f'{cluster_key}_nhood_enrichment']['zscore'])
    cmat = np.asarray(ad.uns[f'{cluster_key}_nhood_enrichment']['count'])
    rows = [{'source': cats[i], 'target': cats[j], 'zscore': float(zmat[i, j]), 'count': int(cmat[i, j])} for i in range(len(cats)) for j in range(len(cats))]
    return pd.DataFrame(rows).sort_values('zscore', ascending=False).reset_index(drop=True)

def spatial_centrality_table(adata, *, cluster_key: str='leiden') -> pd.DataFrame:
    """Compute Squidpy spatial graph centrality scores per cluster."""
    import squidpy as sq
    ad = adata.copy()
    ad.obs[cluster_key] = ad.obs[cluster_key].astype(str).astype('category')
    if 'spatial_connectivities' not in ad.obsp:
        sq.gr.spatial_neighbors(ad, coord_type='generic', delaunay=True)
    sq.gr.centrality_scores(ad, cluster_key=cluster_key)
    df = ad.uns[f'{cluster_key}_centrality_scores'].copy()
    df.index.name = 'cluster'
    return df.reset_index()
