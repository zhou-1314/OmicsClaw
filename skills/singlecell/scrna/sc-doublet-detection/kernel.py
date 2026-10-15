"""Lightweight kernel helpers for sc-doublet-detection (per-sample Scrublet doublet scoring)."""
from __future__ import annotations
__all__ = ['detect_doublets', 'doublet_summary', 'group_doublet_rates']

def detect_doublets(adata, *, batch_key: str | None=None, expected_doublet_rate: float=0.06, threshold: float | None=None, random_state: int=0):
    """Run Scrublet doublet detection (per-sample when batch_key is set) on raw counts."""
    import scanpy as sc
    ad = adata.copy()
    if 'counts' in ad.layers:
        ad.X = ad.layers['counts'].copy()
    sc.pp.scrublet(ad, batch_key=batch_key if batch_key and batch_key in ad.obs else None, expected_doublet_rate=expected_doublet_rate, threshold=threshold, random_state=random_state)
    ad.obs['doublet_classification'] = ad.obs['predicted_doublet'].map({True: 'doublet', False: 'singlet'})
    return ad

def doublet_summary(adata) -> pd.DataFrame:
    """Summarize total singlets, predicted doublets, and overall doublet percentage."""
    import pandas as pd
    vc = adata.obs['predicted_doublet'].value_counts()
    n_dbl = int(vc.get(True, 0))
    n_sng = int(vc.get(False, 0))
    tot = max(len(adata), 1)
    return pd.DataFrame([{'classification': 'singlet', 'n_cells': n_sng, 'pct': 100.0 * n_sng / tot}, {'classification': 'doublet', 'n_cells': n_dbl, 'pct': 100.0 * n_dbl / tot}])

def group_doublet_rates(adata, *, groupby: str) -> pd.DataFrame:
    """Compute doublet counts, rates, and median scores per batch or cluster."""
    grp = adata.obs.groupby(groupby, observed=True)
    return grp.agg(n_cells=('predicted_doublet', 'count'), n_doublets=('predicted_doublet', lambda s: int(s.astype(bool).sum())), doublet_rate=('predicted_doublet', lambda s: float(s.astype(bool).mean())), median_score=('doublet_score', 'median')).reset_index()
