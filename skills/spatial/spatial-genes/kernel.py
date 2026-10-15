"""Lightweight kernel helpers for spatial-genes (Squidpy Moran's I SVG ranking & summary)."""
from __future__ import annotations
__all__ = ['morans_svg', 'svg_summary']

def morans_svg(adata, *, genes: list[str] | None=None, n_perms: int=100, coord_type: str='generic', n_neighs: int=6, library_key: str | None=None) -> pd.DataFrame:
    """Compute Moran's I spatial autocorrelation scores and return the ranked SVG table."""
    import squidpy as sq
    ad = adata.copy()
    if 'spatial_connectivities' not in ad.obsp:
        sq.gr.spatial_neighbors(ad, coord_type=coord_type, n_neighs=n_neighs, delaunay=coord_type == 'generic', library_key=library_key)
    sq.gr.spatial_autocorr(ad, mode='moran', genes=genes, n_perms=n_perms, n_jobs=1)
    df = ad.uns['moranI'].copy()
    df.index.name = 'gene'
    return df.reset_index()

def svg_summary(df: pd.DataFrame, *, fdr: float=0.05, fdr_col: str='pval_norm_fdr_bh') -> dict:
    """Count total tested and FDR-significant spatially variable genes."""
    sig = df[df[fdr_col] < fdr] if fdr_col in df.columns else df.iloc[0:0]
    top = sig['gene'].head(10).tolist() if 'gene' in sig.columns else sig.index[:10].tolist()
    return {'n_tested': int(len(df)), 'n_significant': int(len(sig)), 'top_genes': top}
