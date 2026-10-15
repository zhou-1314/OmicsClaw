"""Lightweight kernel helpers for sc-velocity-prep (splicing layer merge & QC summary)."""
from __future__ import annotations
__all__ = ['merge_velocity_layers', 'velocity_layer_summary', 'top_splicing_genes']

def merge_velocity_layers(base_adata, velocity_adata):
    """Align barcodes/genes and copy spliced, unspliced (and ambiguous) layers onto base_adata."""
    import pandas as pd

    def _clean_barcodes(index: pd.Index) -> pd.Index:
        return pd.Index(index.astype(str)).str.replace('^.*:', '', regex=True).str.replace('(-\\d+|x)$', '', regex=True)
    for req in ('spliced', 'unspliced'):
        if req not in velocity_adata.layers:
            raise ValueError(f"velocity_adata is missing required layer '{req}'.")
    b_ad, v_ad = (base_adata.copy(), velocity_adata.copy())
    b_map = pd.Series(b_ad.obs_names, index=_clean_barcodes(b_ad.obs_names))
    v_map = pd.Series(v_ad.obs_names, index=_clean_barcodes(v_ad.obs_names))
    common_bc = b_map.index.intersection(v_map.index)
    common_genes = b_ad.var_names.intersection(v_ad.var_names)
    if len(common_bc) == 0 or len(common_genes) == 0:
        raise ValueError('No overlapping cell barcodes or genes between base_adata and velocity_adata.')
    out = b_ad[b_map.loc[common_bc].to_numpy(), common_genes].copy()
    v_sub = v_ad[v_map.loc[common_bc].to_numpy(), common_genes]
    for layer in ('spliced', 'unspliced', 'ambiguous'):
        if layer in v_sub.layers:
            out.layers[layer] = v_sub.layers[layer].copy()
    return out

def velocity_layer_summary(adata) -> pd.DataFrame:
    """Summarize total counts, fraction of total, mean per cell, and nonzero fraction per splicing layer."""
    import numpy as np, pandas as pd
    from scipy import sparse
    layers = [k for k in ('spliced', 'unspliced', 'ambiguous') if k in adata.layers]
    if not layers:
        raise ValueError("No splicing layers ('spliced', 'unspliced') found in adata.layers.")
    totals = {k: float(adata.layers[k].sum()) for k in layers}
    grand_total = max(sum(totals.values()), 1e-12)
    rows = []
    for k in layers:
        mat = adata.layers[k]
        nnz = int(mat.nnz) if sparse.issparse(mat) else int(np.count_nonzero(mat))
        rows.append({'layer': k, 'total_counts': totals[k], 'fraction_of_total': totals[k] / grand_total, 'mean_per_cell': totals[k] / max(adata.n_obs, 1), 'nonzero_fraction': nnz / max(adata.n_obs * adata.n_vars, 1)})
    return pd.DataFrame(rows)

def top_splicing_genes(adata, *, n_top: int=30) -> pd.DataFrame:
    """Rank genes by combined spliced + unspliced counts and report unspliced fraction."""
    import numpy as np, pandas as pd
    s = np.asarray(adata.layers['spliced'].sum(axis=0)).ravel().astype(float)
    u = np.asarray(adata.layers['unspliced'].sum(axis=0)).ravel().astype(float)
    tot = s + u
    df = pd.DataFrame({'gene': adata.var_names.astype(str), 'spliced_counts': s, 'unspliced_counts': u, 'unspliced_fraction': u / np.maximum(tot, 1e-12)})
    return df.assign(_tot=tot).sort_values('_tot', ascending=False, ignore_index=True).drop(columns=['_tot']).head(n_top)
