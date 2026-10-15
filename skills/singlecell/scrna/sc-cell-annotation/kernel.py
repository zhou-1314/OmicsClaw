"""Lightweight kernel helpers for sc-cell-annotation (CellTypist / marker overlap scoring)."""
from __future__ import annotations
__all__ = ['annotate', 'annotation_summary']

def annotate(adata, *, method: str='markers', markers: dict[str, list[str]] | None=None, cluster_key: str='leiden', key_added: str='cell_type'):
    """Assign cell-type labels via marker gene overlap or CellTypist."""
    import scanpy as sc
    ad = adata.copy()
    if method == 'celltypist':
        import celltypist
        preds = celltypist.annotate(ad, majority_voting=True)
        ad.obs[key_added] = preds.predicted_labels['majority_voting'].astype(str)
        return ad
    marker_map = markers or {}
    for ct, genes in marker_map.items():
        valid = [g for g in genes if g in ad.var_names]
        if valid:
            sc.tl.score_genes(ad, valid, score_name=f'_score_{ct}')
    score_cols = [f'_score_{ct}' for ct in marker_map if f'_score_{ct}' in ad.obs]
    if score_cols:
        ad.obs[key_added] = ad.obs[score_cols].idxmax(axis=1).str.replace('^_score_', '', regex=True)
        ad.obs.drop(columns=score_cols, inplace=True)
    else:
        ad.obs[key_added] = ad.obs[cluster_key].astype(str) if cluster_key in ad.obs else 'unassigned'
    return ad

def annotation_summary(adata, *, key: str='cell_type') -> pd.DataFrame:
    """Return cell counts and proportions per assigned cell type."""
    import pandas as pd
    vc = adata.obs[key].value_counts()
    return pd.DataFrame({'cell_type': vc.index.astype(str), 'n_cells': vc.values, 'fraction': vc.values / max(len(adata), 1)})
