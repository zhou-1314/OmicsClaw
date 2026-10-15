"""Lightweight kernel helpers for spatial-annotate (marker scoring & label summary)."""
from __future__ import annotations
__all__ = ['score_marker_labels', 'annotation_summary']

def score_marker_labels(adata, markers: dict[str, list[str]], *, key_added: str='cell_type', min_score: float=0.0):
    """Score marker gene panels per spot/cell and assign the top-scoring cell type."""
    import numpy as np, pandas as pd, scanpy as sc
    ad = adata.copy()
    score_cols = []
    for label, genes in markers.items():
        valid = [g for g in genes if g in ad.var_names]
        if not valid:
            continue
        col = f'_score_{label}'
        sc.tl.score_genes(ad, gene_list=valid, score_name=col, use_raw=False)
        score_cols.append((label, col))
    if not score_cols:
        raise ValueError('No marker genes matched adata.var_names.')
    mat = np.column_stack([ad.obs[col].to_numpy() for _, col in score_cols])
    best_idx = np.argmax(mat, axis=1)
    best_score = mat[np.arange(ad.n_obs), best_idx]
    names = [label for label, _ in score_cols]
    assigned = [names[i] if s >= min_score else 'Unassigned' for i, s in zip(best_idx, best_score)]
    ad.obs[key_added] = pd.Categorical(assigned)
    ad.obs[f'{key_added}_score'] = best_score
    ad.obs.drop(columns=[col for _, col in score_cols], inplace=True)
    return ad

def annotation_summary(adata, *, key: str='cell_type') -> pd.DataFrame:
    """Summarize spot/cell counts and proportions per annotated cell type."""
    import pandas as pd
    vc = adata.obs[key].value_counts(dropna=False)
    total = max(int(adata.n_obs), 1)
    return pd.DataFrame({'cell_type': vc.index.astype(str), 'n_spots': vc.values.astype(int), 'proportion': (vc.values / total).round(4)})
