"""Lightweight kernel helpers for spatial-microenvironment-subset (cKDTree radius niche extraction)."""
from __future__ import annotations
__all__ = ['subset_microenvironment', 'niche_composition']

def subset_microenvironment(adata, *, cell_type_key: str, center_types: list[str], radius: float, spatial_key: str='spatial'):
    """Extract center cells/spots and all neighbors within physical Euclidean radius."""
    import numpy as np, pandas as pd
    from scipy.spatial import cKDTree
    coords = np.asarray(adata.obsm[spatial_key], dtype=float)
    is_center = adata.obs[cell_type_key].astype(str).isin([str(c) for c in center_types]).to_numpy()
    center_coords = coords[is_center]
    if len(center_coords) == 0:
        raise ValueError(f"No cells matched center_types={center_types} in obs['{cell_type_key}'].")
    tree = cKDTree(center_coords)
    dists, _ = tree.query(coords, k=1)
    keep = dists <= float(radius)
    sub = adata[keep].copy()
    sub.obs['dist_to_center'] = dists[keep]
    sub.obs['niche_role'] = pd.Categorical(np.where(is_center[keep], 'center', 'neighbor'), categories=['center', 'neighbor'])
    return sub

def niche_composition(adata_sub, *, cell_type_key: str, role_key: str='niche_role') -> pd.DataFrame:
    """Tabulate cell-type counts and proportions across center and neighbor microenvironment roles."""
    import pandas as pd
    ct = pd.crosstab(adata_sub.obs[cell_type_key], adata_sub.obs[role_key])
    ct['total'] = ct.sum(axis=1)
    ct['proportion'] = (ct['total'] / max(int(adata_sub.n_obs), 1)).round(4)
    return ct.reset_index()
