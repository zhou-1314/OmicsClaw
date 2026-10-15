"""Lightweight kernel helpers for spatial-communication (LIANA consensus & top L-R filtering)."""
from __future__ import annotations
__all__ = ['run_liana_spatial', 'top_interactions']

def run_liana_spatial(adata, *, groupby: str='cell_type', resource_name: str='consensus', expr_prop: float=0.1) -> pd.DataFrame:
    """Run LIANA consensus ligand-receptor rank aggregation on annotated spatial AnnData."""
    import liana as li
    ad = adata.copy()
    ad.obs[groupby] = ad.obs[groupby].astype(str).astype('category')
    li.mt.rank_aggregate(ad, groupby=groupby, resource_name=resource_name, expr_prop=expr_prop, use_raw=False, verbose=False)
    return ad.uns['liana_res'].copy()

def top_interactions(lr_df: pd.DataFrame, *, n_top: int=20, max_specificity_rank: float=0.05) -> pd.DataFrame:
    """Filter and rank top ligand-receptor pairs by specificity and magnitude rank."""
    df = lr_df.copy()
    if 'specificity_rank' in df.columns:
        df = df[df['specificity_rank'] <= max_specificity_rank]
    sort_col = 'magnitude_rank' if 'magnitude_rank' in df.columns else df.columns[-1]
    return df.sort_values(sort_col, ascending=True).head(n_top).reset_index(drop=True)
