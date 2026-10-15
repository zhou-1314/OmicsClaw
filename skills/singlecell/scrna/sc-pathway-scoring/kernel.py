"""Lightweight kernel helpers for sc-pathway-scoring (per-cell Scanpy score_genes & AUCell)."""
from __future__ import annotations
__all__ = ['score_gene_sets', 'group_scores', 'top_pathways']

def score_gene_sets(adata, gene_sets: dict[str, list[str]], *, method: str='score_genes_py', auc_threshold: float=0.05, ctrl_size: int=50, random_state: int=42) -> pd.DataFrame:
    """Score per-cell pathway activity using Scanpy score_genes or rank-based AUCell."""
    import numpy as np, pandas as pd, scanpy as sc
    ad = adata.copy()
    scores = {}
    if method == 'score_genes_py':
        for name, genes in gene_sets.items():
            present = [g for g in genes if g in ad.var_names]
            if present:
                sc.tl.score_genes(ad, present, ctrl_size=min(ctrl_size, max(len(present), 1)), score_name=f'_s_{name}', random_state=random_state, use_raw=False)
                scores[name] = ad.obs[f'_s_{name}'].values
    elif method == 'aucell_py':
        dense = ad.X.toarray() if hasattr(ad.X, 'toarray') else np.asarray(ad.X)
        k = max(int(np.ceil(auc_threshold * ad.n_vars)), 1)
        top_idx = np.argsort(-dense, axis=1)[:, :k]
        gene_to_col = {g: i for i, g in enumerate(ad.var_names)}
        for name, genes in gene_sets.items():
            cols = [gene_to_col[g] for g in genes if g in gene_to_col]
            if cols:
                in_top = np.isin(top_idx, cols)
                scores[name] = in_top.cumsum(axis=1).sum(axis=1) / float(len(cols) * k)
    else:
        raise ValueError(f'Unsupported scoring method: {method}')
    return pd.DataFrame(scores, index=ad.obs_names)

def group_scores(adata, scores_df: pd.DataFrame, *, groupby: str='leiden') -> pd.DataFrame:
    """Compute mean pathway activity score per cluster or cell-type group."""
    return scores_df.groupby(adata.obs[groupby].astype(str), observed=True).mean()

def top_pathways(scores_df: pd.DataFrame, *, n_top: int=20) -> pd.DataFrame:
    """Rank pathways by mean absolute score across all cells."""
    import pandas as pd
    mean_abs = scores_df.abs().mean(axis=0).sort_values(ascending=False).head(n_top)
    return pd.DataFrame({'pathway': mean_abs.index, 'mean_abs_score': mean_abs.values})
