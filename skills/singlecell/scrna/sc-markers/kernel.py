"""Lightweight kernel helpers for sc-markers (cluster marker ranking & specificity filtering)."""
from __future__ import annotations
__all__ = ['find_markers', 'top_markers', 'cluster_marker_summary']

def find_markers(adata, *, groupby: str='leiden', method: str='wilcoxon', min_in_group_fraction: float=0.25, min_fold_change: float=0.25, max_out_group_fraction: float=0.5) -> pd.DataFrame:
    """Rank cluster marker genes on log-normalized expression and apply specificity filters."""
    import scanpy as sc
    ad = adata.copy()
    sc.tl.rank_genes_groups(ad, groupby=groupby, method=method, pts=True, use_raw=False)
    df = sc.get.rank_genes_groups_df(ad, group=None)
    if {'pct_nz_group', 'pct_nz_reference', 'logfoldchanges'}.issubset(df.columns):
        filtered = df[(df['pct_nz_group'] >= min_in_group_fraction) & (df['pct_nz_reference'] <= max_out_group_fraction) & (df['logfoldchanges'] >= min_fold_change)]
        if not filtered.empty:
            return filtered.reset_index(drop=True)
    return df.reset_index(drop=True)

def top_markers(markers_df: pd.DataFrame, *, n_top: int=10) -> pd.DataFrame:
    """Return top n_top markers per group sorted by adjusted p-value and log-fold change."""
    sort_cols = [c for c in ['group', 'pvals_adj', 'scores'] if c in markers_df.columns]
    asc = [True, True, False][:len(sort_cols)]
    return markers_df.sort_values(sort_cols, ascending=asc).groupby('group', observed=True).head(n_top).reset_index(drop=True)

def cluster_marker_summary(markers_df: pd.DataFrame, *, fdr: float=0.05) -> pd.DataFrame:
    """Summarize marker count and top marker gene per cluster."""
    sig = markers_df[markers_df['pvals_adj'] < fdr] if 'pvals_adj' in markers_df.columns else markers_df
    if sig.empty:
        sig = markers_df
    return sig.groupby('group', observed=True).agg(n_markers=('names', 'count'), top_gene=('names', 'first'), max_lfc=('logfoldchanges', 'max') if 'logfoldchanges' in sig.columns else ('scores', 'max')).reset_index()
