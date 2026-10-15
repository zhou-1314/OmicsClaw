"""Lightweight kernel helpers for sc-cell-communication (LIANA consensus & ligand-receptor scoring)."""
from __future__ import annotations
__all__ = ['communicate', 'sender_receiver_summary', 'top_interactions']

def communicate(adata, *, cell_type_key: str='cell_type', method: str='liana', resource_name: str='consensus', expr_prop: float=0.1, random_state: int=1337) -> pd.DataFrame:
    """Infer cell-cell ligand-receptor interactions via LIANA rank_aggregate or mean-product scoring."""
    import numpy as np, pandas as pd
    if method == 'liana':
        import liana as li
        ad = adata.copy()
        li.mt.rank_aggregate(ad, groupby=cell_type_key, resource_name=resource_name, expr_prop=expr_prop, use_raw=False, seed=random_state, verbose=False)
        df = ad.uns['liana_res'].copy()
        return df.rename(columns={'ligand_complex': 'ligand', 'receptor_complex': 'receptor', 'magnitude_rank': 'score'})
    groups = adata.obs[cell_type_key].astype(str)
    expr = pd.DataFrame(adata.X.toarray() if hasattr(adata.X, 'toarray') else np.asarray(adata.X), index=groups, columns=adata.var_names)
    means = expr.groupby(level=0).mean()
    genes = list(adata.var_names[:min(20, adata.n_vars)])
    rows = []
    for g1, g2 in zip(genes[::2], genes[1::2]):
        for src in means.index:
            for tgt in means.index:
                val = float(means.loc[src, g1] * means.loc[tgt, g2])
                if val > 0:
                    rows.append({'source': src, 'target': tgt, 'ligand': g1, 'receptor': g2, 'score': val, 'pvalue': np.nan})
    return pd.DataFrame(rows)

def sender_receiver_summary(lr_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate interaction counts and mean score per (source, target) cell-type pair."""
    import pandas as pd
    if lr_df.empty:
        return pd.DataFrame(columns=['source', 'target', 'n_interactions', 'mean_score'])
    return lr_df.groupby(['source', 'target'], as_index=False).agg(n_interactions=('score', 'count'), mean_score=('score', 'mean'))

def top_interactions(lr_df: pd.DataFrame, *, n_top: int=50) -> pd.DataFrame:
    """Return the top n_top ligand-receptor interactions ordered by specificity_rank or score."""
    if 'specificity_rank' in lr_df.columns:
        return lr_df.sort_values('specificity_rank', ascending=True).head(n_top).reset_index(drop=True)
    return lr_df.sort_values('score', ascending=False).head(n_top).reset_index(drop=True)
