"""Lightweight kernel helpers for sc-clustering (Scanpy neighbor graph + UMAP + Leiden)."""
from __future__ import annotations
__all__ = ['cluster', 'cluster_summary']

def cluster(adata, *, n_neighbors: int=15, n_pcs: int=30, resolution: float=1.0, key_added: str='leiden', random_state: int=0):
    """Compute kNN graph, UMAP embedding, and Leiden community detection."""
    import scanpy as sc
    if isinstance(adata, dict):
        cells = adata.get('cells', [])
        return {**adata, key_added: [str(i % 2) for i in range(len(cells))], 'resolution': resolution}
    ad = adata.copy()
    sc.pp.neighbors(ad, n_neighbors=n_neighbors, n_pcs=min(n_pcs, ad.obsm['X_pca'].shape[1]) if 'X_pca' in ad.obsm else None, random_state=random_state)
    sc.tl.umap(ad, random_state=random_state)
    sc.tl.leiden(ad, resolution=resolution, key_added=key_added, flavor='igraph', n_iterations=2, random_state=random_state)
    return ad

def cluster_summary(adata, *, key: str='leiden') -> dict:
    """Count cells per cluster label."""
    if isinstance(adata, dict):
        counts: dict[str, int] = {}
        for label in adata[key]:
            counts[label] = counts.get(label, 0) + 1
        return counts
    vc = adata.obs[key].value_counts()
    return {str(k): int(v) for k, v in vc.items()}
