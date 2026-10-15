"""Lightweight kernel helpers for sc-grn (TF-target adjacency & regulon scoring)."""
from __future__ import annotations
__all__ = ['infer_adjacencies', 'regulons_from_adjacencies', 'score_regulons']

def infer_adjacencies(adata, *, tfs, method: str='correlation', layer: str | None=None, n_top: int=50) -> pd.DataFrame:
    """Infer TF -> target importance scores using rank-correlation across cells."""
    import numpy as np, pandas as pd
    from scipy import sparse
    from scipy.stats import rankdata
    tf_list = [str(g) for g in tfs if str(g) in adata.var_names]
    if not tf_list or n_top < 1:
        raise ValueError('No supplied TFs overlap adata.var_names or n_top < 1.')
    mat = adata.layers[layer] if layer and layer in adata.layers else adata.layers['counts'] if 'counts' in adata.layers else adata.X
    dense = mat.toarray().astype(float) if sparse.issparse(mat) else np.asarray(mat, dtype=float)
    ranked = np.apply_along_axis(rankdata, 0, dense)
    centered = ranked - ranked.mean(axis=0, keepdims=True)
    norms = np.maximum(np.linalg.norm(centered, axis=0, keepdims=True), 1e-12)
    unit = centered / norms
    tf_set = set(tf_list)
    target_mask = np.array([g not in tf_set for g in adata.var_names], dtype=bool)
    target_names = np.asarray(adata.var_names)[target_mask]
    rows = []
    for tf in tf_list:
        tf_idx = adata.var_names.get_loc(tf)
        corrs = np.abs(unit[:, tf_idx] @ unit[:, target_mask])
        top_idx = np.argsort(corrs)[::-1][:n_top]
        for idx in top_idx:
            if corrs[idx] > 0:
                rows.append({'TF': tf, 'target': str(target_names[idx]), 'importance': float(corrs[idx])})
    return pd.DataFrame(rows).sort_values('importance', ascending=False, ignore_index=True)

def regulons_from_adjacencies(adjacencies: pd.DataFrame, *, n_top: int=50) -> list[dict]:
    """Group top-N target genes per TF into regulon dictionaries."""
    regulons = []
    for tf, grp in adjacencies.sort_values('importance', ascending=False).groupby('TF', sort=False):
        targets = grp['target'].head(n_top).astype(str).tolist()
        if targets:
            regulons.append({'name': f'regulon_{tf}', 'tf': str(tf), 'targets': targets, 'n_targets': len(targets)})
    return regulons

def score_regulons(adata, regulons: list[dict], *, layer: str | None=None) -> pd.DataFrame:
    """Compute per-cell mean expression score for each regulon dictionary."""
    import numpy as np, pandas as pd
    from scipy import sparse
    mat = adata.layers[layer] if layer and layer in adata.layers else adata.X
    var_set = set(adata.var_names)
    scores = {}
    for reg in regulons:
        genes = [g for g in reg.get('targets', []) if g in var_set]
        if not genes:
            continue
        sub = adata[:, genes].layers[layer] if layer and layer in adata.layers else adata[:, genes].X
        scores[reg['name']] = np.asarray(sub.mean(axis=1)).ravel().astype(float) if sparse.issparse(sub) else np.asarray(sub.mean(axis=1), dtype=float).ravel()
    return pd.DataFrame(scores, index=adata.obs_names)
