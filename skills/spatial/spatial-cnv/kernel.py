"""Lightweight kernel helpers for spatial-cnv (infercnvpy sliding-window CNV & summary)."""
from __future__ import annotations
__all__ = ['run_infercnv', 'cnv_summary']

def run_infercnv(adata, *, reference_key: str | None=None, reference_cat: list[str] | None=None, window_size: int=100, step: int=10):
    """Run infercnvpy sliding-window copy-number inference and compute per-spot CNV score."""
    import infercnvpy as cnv
    ad = adata.copy()
    cnv.tl.infercnv(ad, reference_key=reference_key, reference_cat=reference_cat, window_size=window_size, step=step)
    cnv.tl.pca(ad)
    cnv.pp.neighbors(ad)
    cnv.tl.leiden(ad)
    cnv.tl.cnv_score(ad)
    return ad

def cnv_summary(adata, *, groupby: str='cnv_leiden', score_key: str='cnv_score') -> pd.DataFrame:
    """Summarize mean and median CNV aberration scores across clusters or spatial domains."""
    import numpy as np, pandas as pd
    if score_key not in adata.obs:
        X_cnv = adata.obsm['X_cnv']
        vals = np.asarray(X_cnv.toarray() if hasattr(X_cnv, 'toarray') else X_cnv)
        adata.obs[score_key] = np.mean(np.abs(vals), axis=1)
    grp = adata.obs.groupby(groupby, observed=True)[score_key]
    return pd.DataFrame({'group': grp.mean().index.astype(str), 'n_spots': grp.count().values.astype(int), 'mean_cnv_score': grp.mean().values.round(4), 'median_cnv_score': grp.median().values.round(4)})
