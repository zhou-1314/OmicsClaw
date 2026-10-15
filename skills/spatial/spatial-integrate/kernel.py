"""Lightweight kernel helpers for spatial-integrate (Harmony PCA integration & batch entropy)."""
from __future__ import annotations
__all__ = ['integrate_harmony', 'batch_mixing_summary']

def integrate_harmony(adata, *, batch_key: str='batch', use_rep: str='X_pca', key_added: str='X_pca_harmony', n_neighbors: int=15, random_state: int=0):
    """Integrate multi-slide spatial PCA embeddings with Harmony and rebuild the KNN graph."""
    import numpy as np, scanpy as sc, harmonypy as hm
    ad = adata.copy()
    ad.obs[batch_key] = ad.obs[batch_key].astype(str)
    ho = hm.run_harmony(ad.obsm[use_rep], ad.obs, batch_key, random_state=random_state, verbose=False)
    Z = np.asarray(ho.Z_corr)
    if Z.shape[0] != ad.n_obs:
        Z = Z.T
    ad.obsm[key_added] = Z
    sc.pp.neighbors(ad, use_rep=key_added, n_neighbors=n_neighbors, random_state=random_state)
    sc.tl.umap(ad, random_state=random_state)
    return ad

def batch_mixing_summary(adata, *, batch_key: str='batch', cluster_key: str='leiden') -> pd.DataFrame:
    """Compute per-cluster batch counts and normalized Shannon batch-mixing entropy."""
    import numpy as np, pandas as pd
    ct = pd.crosstab(adata.obs[cluster_key], adata.obs[batch_key])
    probs = ct.div(ct.sum(axis=1), axis=0).to_numpy()
    n_batches = max(ct.shape[1], 2)
    with np.errstate(divide='ignore', invalid='ignore'):
        ent = -np.nansum(np.where(probs > 0, probs * np.log(probs), 0.0), axis=1) / np.log(n_batches)
    return pd.DataFrame({'cluster': ct.index.astype(str), 'n_spots': ct.sum(axis=1).values.astype(int), 'n_batches_present': (ct > 0).sum(axis=1).values.astype(int), 'batch_entropy_norm': np.round(ent, 4)})
