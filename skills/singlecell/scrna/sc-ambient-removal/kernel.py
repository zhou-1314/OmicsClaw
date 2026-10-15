"""Lightweight kernel helpers for sc-ambient-removal (ambient RNA profile estimation & subtraction)."""
from __future__ import annotations
__all__ = ['remove_ambient', 'estimate_ambient_profile', 'ambient_summary']

def estimate_ambient_profile(adata, *, empty_droplets=None) -> np.ndarray:
    """Estimate normalized gene-wise ambient RNA profile from empty droplets or low-UMI cells."""
    import numpy as np
    ref = empty_droplets if empty_droplets is not None else adata
    mat = ref.layers['counts'] if 'counts' in ref.layers else ref.X
    gene_sums = np.asarray(mat.sum(axis=0)).ravel().astype(np.float64)
    total = gene_sums.sum()
    return gene_sums / max(total, 1.0)

def remove_ambient(adata, *, contamination: float=0.05, empty_droplets=None, layer: str='counts'):
    """Subtract estimated ambient RNA contamination from count matrix and clip at zero."""
    import numpy as np, scipy.sparse as sp
    if not 0.0 <= contamination < 1.0:
        raise ValueError('contamination must be in [0.0, 1.0).')
    ad = adata.copy()
    raw = ad.layers[layer].copy() if layer in ad.layers else ad.X.copy()
    ad.layers['counts'] = raw.copy()
    profile = estimate_ambient_profile(ad, empty_droplets=empty_droplets)
    dense = raw.toarray().astype(np.float32) if sp.issparse(raw) else np.asarray(raw, dtype=np.float32)
    cell_totals = dense.sum(axis=1, keepdims=True)
    corrected = np.clip(dense - contamination * (cell_totals * profile[None, :]), 0.0, None)
    ad.X = sp.csr_matrix(corrected) if sp.issparse(raw) else corrected
    ad.uns['ambient_correction'] = {'contamination': float(contamination), 'method': 'simple'}
    return ad

def ambient_summary(adata, *, raw_layer: str='counts') -> pd.DataFrame:
    """Compare per-cell total counts before and after ambient RNA removal."""
    import numpy as np, pandas as pd
    before = np.asarray(adata.layers[raw_layer].sum(axis=1)).ravel() if raw_layer in adata.layers else np.asarray(adata.X.sum(axis=1)).ravel()
    after = np.asarray(adata.X.sum(axis=1)).ravel()
    red_pct = float(100.0 * (1.0 - after.sum() / max(before.sum(), 1.0)))
    return pd.DataFrame([{'mean_counts_before': float(before.mean()), 'mean_counts_after': float(after.mean()), 'reduction_pct': red_pct}])
