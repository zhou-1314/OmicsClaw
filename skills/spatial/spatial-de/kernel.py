"""Lightweight kernel helpers for spatial-de (Scanpy spatial domain DE & pseudobulk)."""
from __future__ import annotations
__all__ = ['spatial_de', 'de_summary', 'spatial_pseudobulk']

def spatial_de(adata, *, groupby: str='leiden', method: str='wilcoxon', layer: str | None=None) -> pd.DataFrame:
    """Rank differentially expressed genes across spatial domains or niches."""
    import scanpy as sc
    ad = adata.copy()
    sc.tl.rank_genes_groups(ad, groupby=groupby, method=method, layer=layer, use_raw=False)
    return sc.get.rank_genes_groups_df(ad, group=None)

def de_summary(df: pd.DataFrame, *, fdr: float=0.05) -> dict:
    """Count total tested and FDR-significant spatially differential genes."""
    sig = df[df['pvals_adj'] < fdr] if 'pvals_adj' in df.columns else df.iloc[0:0]
    return {'n_tested': int(len(df)), 'n_significant': int(len(sig))}

def spatial_pseudobulk(adata, *, sample_key: str='slide_id', condition_key: str='condition', domain_key: str | None=None, target_domain: str | None=None, layer: str | None='counts', min_spots: int=10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate raw spot counts by slide_id (and optional domain) for pseudobulk DESeq2 analysis."""
    import numpy as np, pandas as pd
    sub_ad = adata[adata.obs[domain_key].astype(str) == str(target_domain)] if (domain_key and target_domain is not None and domain_key in adata.obs) else adata
    mat = sub_ad.layers[layer] if (layer and layer in sub_ad.layers) else sub_ad.X
    rows, meta = {}, {}
    for (slide, cond), idx in sub_ad.obs.groupby([sample_key, condition_key], observed=True).groups.items():
        if len(idx) < min_spots:
            continue
        arr = np.asarray(mat[sub_ad.obs.index.get_indexer(idx)].sum(axis=0)).ravel()
        rows[str(slide)] = np.rint(arr).astype(int)
        meta[str(slide)] = {condition_key: str(cond), 'n_spots': int(len(idx))}
    if not rows:
        return pd.DataFrame(columns=sub_ad.var_names), pd.DataFrame(columns=[condition_key, 'n_spots'])
    counts_df = pd.DataFrame.from_dict(rows, orient='index', columns=sub_ad.var_names)
    counts_df = counts_df.loc[:, counts_df.sum(axis=0) >= 10]
    return counts_df, pd.DataFrame.from_dict(meta, orient='index')
