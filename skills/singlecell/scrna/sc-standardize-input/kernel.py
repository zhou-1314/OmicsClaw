"""Lightweight kernel helpers for sc-standardize-input (AnnData count contract canonicalization)."""
from __future__ import annotations
__all__ = ['standardize', 'infer_species', 'contract_summary']

def infer_species(adata) -> str:
    """Infer 'human' or 'mouse' from gene symbol casing (UPPER -> human, Title -> mouse)."""
    names = [str(g) for g in adata.var_names[:500] if not str(g).startswith(('ENS', 'ENSMUS'))]
    if not names:
        return 'human'
    upper = sum((1 for g in names if g.isupper()))
    title = sum((1 for g in names if len(g) > 1 and g[0].isupper() and g[1:].islower()))
    return 'mouse' if title > upper else 'human'

def standardize(adata, *, species: str='auto', make_var_unique: bool=True):
    """Canonicalize an external AnnData so raw counts live in layers['counts'] and adata.raw."""
    ad = adata.copy()
    if make_var_unique:
        ad.var_names_make_unique()
        ad.obs_names_make_unique()
    source = 'X'
    if 'counts' in ad.layers:
        source = "layers['counts']"
        ad.X = ad.layers['counts'].copy()
    elif ad.raw is not None and ad.raw.shape == ad.shape:
        source = 'raw'
        ad.X = ad.raw.X.copy()
    else:
        ad.layers['counts'] = ad.X.copy()
    ad.raw = ad.copy()
    resolved_species = infer_species(ad) if species == 'auto' else species
    ad.uns['omicsclaw_matrix_contract'] = {'counts_source': source, 'species': resolved_species}
    return ad

def contract_summary(adata) -> dict:
    """Verify whether layers['counts'] exists and whether counts are non-negative integers."""
    import numpy as np
    mat = adata.layers['counts'] if 'counts' in adata.layers else adata.X
    data = mat.data if hasattr(mat, 'data') else np.asarray(mat).ravel()
    sample = data[:10000] if len(data) else np.array([0.0])
    is_int = bool(np.all(sample >= 0) and np.allclose(sample, np.round(sample)))
    return {'n_obs': int(adata.n_obs), 'n_vars': int(adata.n_vars), 'has_counts_layer': 'counts' in adata.layers, 'integer_like': is_int}
