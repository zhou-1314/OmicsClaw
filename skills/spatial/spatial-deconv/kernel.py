"""Lightweight kernel helpers for spatial-deconv (proportion normalization & summary)."""
from __future__ import annotations
__all__ = ['attach_proportions', 'deconv_summary']

def attach_proportions(adata, prop_df: pd.DataFrame, *, key_added: str='deconv_proportions', dominant_key: str='dominant_cell_type'):
    """Normalize spot-level cell-type abundances to proportions summing to 1 and store in AnnData."""
    import pandas as pd
    ad = adata.copy()
    aligned = prop_df.reindex(ad.obs_names).fillna(0.0).clip(lower=0.0)
    row_sums = aligned.sum(axis=1).replace(0.0, 1.0)
    norm_df = aligned.div(row_sums, axis=0)
    ad.obsm[key_added] = norm_df
    ad.uns[f'{key_added}_cell_types'] = list(norm_df.columns)
    ad.obs[dominant_key] = pd.Categorical(norm_df.idxmax(axis=1))
    return ad

def deconv_summary(adata, *, key: str='deconv_proportions') -> pd.DataFrame:
    """Compute mean proportion, SD, and dominant-spot count per deconvolved cell type."""
    import numpy as np, pandas as pd
    prop = adata.obsm[key]
    if not isinstance(prop, pd.DataFrame):
        cols = adata.uns.get(f'{key}_cell_types', [f'ct_{i}' for i in range(prop.shape[1])])
        prop = pd.DataFrame(np.asarray(prop), index=adata.obs_names, columns=cols)
    dom = prop.idxmax(axis=1).value_counts()
    return pd.DataFrame({'cell_type': prop.columns.astype(str), 'mean_proportion': prop.mean(axis=0).values.round(4), 'sd_proportion': prop.std(axis=0).values.round(4), 'n_dominant_spots': [int(dom.get(c, 0)) for c in prop.columns]})
