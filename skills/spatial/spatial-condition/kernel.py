"""Lightweight kernel helpers for spatial-condition (sample-level pseudobulk & DE summary)."""
from __future__ import annotations
__all__ = ['pseudobulk_aggregate', 'condition_de_summary']

def pseudobulk_aggregate(adata, *, sample_key: str, condition_key: str, cluster_key: str | None=None, layer: str | None='counts', min_cells: int=10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate raw integer spot/cell counts per biological sample (and optional domain)."""
    import numpy as np, pandas as pd
    mat = adata.layers[layer] if layer and layer in adata.layers else adata.X
    group_cols = [sample_key, condition_key] + ([cluster_key] if cluster_key else [])
    rows, meta = ({}, {})
    for keys, idx in adata.obs.groupby(group_cols, observed=True).groups.items():
        if len(idx) < min_cells:
            continue
        sample_id = str(keys[0]) if not cluster_key else f'{keys[0]}__{keys[2]}'
        sub = mat[adata.obs.index.get_indexer(idx)]
        rows[sample_id] = np.asarray(sub.sum(axis=0)).ravel().astype(int)
        meta[sample_id] = {sample_key: str(keys[0]), condition_key: str(keys[1]), 'n_spots': int(len(idx))}
        if cluster_key:
            meta[sample_id][cluster_key] = str(keys[2])
    counts_df = pd.DataFrame.from_dict(rows, orient='index', columns=adata.var_names)
    counts_df = counts_df.loc[:, counts_df.sum(axis=0) >= 10]
    meta_df = pd.DataFrame.from_dict(meta, orient='index')
    return (counts_df, meta_df)

def condition_de_summary(df: pd.DataFrame, *, fdr: float=0.05, lfc_thresh: float=0.5) -> dict:
    """Summarize total tested, up-regulated, and down-regulated genes across conditions."""
    pcol = 'padj' if 'padj' in df.columns else 'pvals_adj'
    lcol = 'log2FoldChange' if 'log2FoldChange' in df.columns else 'logfoldchanges'
    sig = df[df[pcol] < fdr] if pcol in df.columns else df.iloc[0:0]
    n_up = int((sig[lcol] >= lfc_thresh).sum()) if lcol in sig.columns else 0
    n_down = int((sig[lcol] <= -lfc_thresh).sum()) if lcol in sig.columns else 0
    return {'n_tested': int(len(df)), 'n_significant': int(len(sig)), 'n_up': n_up, 'n_down': n_down}
