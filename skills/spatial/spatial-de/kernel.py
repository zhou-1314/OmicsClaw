"""Lightweight kernel helpers for spatial-de (Scanpy spatial domain DE & pseudobulk)."""
from __future__ import annotations
__all__ = ['spatial_de', 'de_summary']

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
