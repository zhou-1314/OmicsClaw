"""Lightweight kernel helpers for spatial-trajectory (diffusion pseudotime & cluster ordering)."""
from __future__ import annotations
__all__ = ['run_dpt_spatial', 'pseudotime_summary']

def run_dpt_spatial(adata, *, root_key: str | None=None, root_val: str | None=None, root_idx: int=0, n_dcs: int=10):
    """Run diffusion map and Diffusion Pseudotime (DPT) from an explicit root cell or cluster."""
    import numpy as np, scanpy as sc
    ad = adata.copy()
    if root_key and root_val is not None and (root_key in ad.obs):
        cand = np.flatnonzero(ad.obs[root_key].astype(str).to_numpy() == str(root_val))
        ad.uns['iroot'] = int(cand[0]) if len(cand) > 0 else int(root_idx)
    elif 'iroot' not in ad.uns:
        ad.uns['iroot'] = int(root_idx)
    if 'connectivities' not in ad.obsp:
        sc.pp.neighbors(ad, n_neighbors=15, random_state=0)
    sc.tl.diffmap(ad, n_comps=min(n_dcs, ad.n_obs - 1), random_state=0)
    sc.tl.dpt(ad, n_dcs=min(n_dcs, ad.obsm['X_diffmap'].shape[1]))
    return ad

def pseudotime_summary(adata, *, groupby: str='leiden', pt_key: str='dpt_pseudotime') -> pd.DataFrame:
    """Summarize mean and median pseudotime per cluster ordered from origin to terminal state."""
    import numpy as np, pandas as pd
    sub = adata.obs[[groupby, pt_key]].replace([np.inf, -np.inf], np.nan).dropna()
    grp = sub.groupby(groupby, observed=True)[pt_key]
    df = pd.DataFrame({'group': grp.mean().index.astype(str), 'n_spots': grp.count().values.astype(int), 'mean_pseudotime': grp.mean().values.round(4), 'median_pseudotime': grp.median().values.round(4)})
    return df.sort_values('median_pseudotime', ascending=True).reset_index(drop=True)
