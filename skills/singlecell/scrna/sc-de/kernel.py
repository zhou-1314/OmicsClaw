"""Lightweight kernel helpers for sc-de (Scanpy rank_genes_groups + PyDESeq2 pseudobulk)."""
from __future__ import annotations
__all__ = ['rank_genes', 'pseudobulk_aggregate', 'de_summary']

def rank_genes(adata, *, groupby: str='leiden', method: str='wilcoxon', layer: str | None=None) -> pd.DataFrame:
    """Run non-parametric/parametric cluster marker ranking via scanpy.tl.rank_genes_groups."""
    import scanpy as sc
    ad = adata.copy()
    sc.tl.rank_genes_groups(ad, groupby=groupby, method=method, layer=layer, use_raw=False)
    return sc.get.rank_genes_groups_df(ad, group=None)

def pseudobulk_aggregate(adata, *, sample_key: str, group_key: str, layer: str='counts') -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sum raw integer counts per (sample, group) replicate for PyDESeq2 pseudobulk analysis."""
    import numpy as np, pandas as pd
    mat = adata.layers[layer] if layer in adata.layers else adata.X
    codes = adata.obs[[sample_key, group_key]].astype(str).agg('__'.join, axis=1)
    rows, meta = ({}, {})
    for key, idx in adata.obs.groupby(codes, observed=True).groups.items():
        if len(idx) < 10:
            continue
        sub = mat[adata.obs.index.get_indexer(idx)]
        rows[key] = np.asarray(sub.sum(axis=0)).ravel().astype(int)
        first = adata.obs.loc[idx[0]]
        meta[key] = {sample_key: str(first[sample_key]), group_key: str(first[group_key]), 'n_cells': len(idx)}
    return (pd.DataFrame.from_dict(rows, orient='index', columns=adata.var_names), pd.DataFrame.from_dict(meta, orient='index'))

def de_summary(df: pd.DataFrame, *, pval_col: str='pvals_adj', lfc_col: str='logfoldchanges', fdr: float=0.05) -> dict:
    """Summarize total tested and FDR-significant genes."""
    sig = df[df[pval_col] < fdr] if pval_col in df.columns else df.iloc[0:0]
    up = int((sig[lfc_col] > 0).sum()) if lfc_col in sig.columns else 0
    return {'n_tested': int(len(df)), 'n_significant': int(len(sig)), 'n_up': up, 'n_down': int(len(sig) - up)}
