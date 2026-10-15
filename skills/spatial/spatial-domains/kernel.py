"""Lightweight kernel helpers for spatial-domains (spatially weighted Leiden & domain summary)."""
from __future__ import annotations
__all__ = ['spatial_leiden', 'domain_summary']

def spatial_leiden(adata, *, spatial_weight: float=0.3, resolution: float=1.0, n_neighbors: int=15, coord_type: str='generic', key_added: str='spatial_domain', random_state: int=0):
    """Blend transcriptomic and spatial neighbor connectivities and cluster with Leiden."""
    import scanpy as sc, squidpy as sq
    ad = adata.copy()
    if 'connectivities' not in ad.obsp:
        sc.pp.neighbors(ad, n_neighbors=n_neighbors, random_state=random_state)
    if 'spatial_connectivities' not in ad.obsp:
        sq.gr.spatial_neighbors(ad, coord_type=coord_type, delaunay=coord_type == 'generic')
    conn = (1.0 - spatial_weight) * ad.obsp['connectivities'] + spatial_weight * ad.obsp['spatial_connectivities']
    sc.tl.leiden(ad, resolution=resolution, adjacency=conn, key_added=key_added, flavor='igraph', n_iterations=2, random_state=random_state)
    return ad

def domain_summary(adata, *, key: str='spatial_domain') -> pd.DataFrame:
    """Count spots/cells and relative percentages per detected spatial domain."""
    import pandas as pd
    vc = adata.obs[key].value_counts(dropna=False)
    total = max(int(adata.n_obs), 1)
    return pd.DataFrame({'domain': vc.index.astype(str), 'n_spots': vc.values.astype(int), 'proportion': (vc.values / total).round(4)})
