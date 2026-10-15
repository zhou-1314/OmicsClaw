"""Lightweight kernel helpers for spatial-enrichment (spatial pathway scoring & Enrichr ORA)."""
from __future__ import annotations
__all__ = ['score_spatial_pathways', 'ora_enrichr']

def score_spatial_pathways(adata, gene_sets: dict[str, list[str]], *, groupby: str='leiden') -> pd.DataFrame:
    """Score gene sets per spot/cell and return mean pathway activity per spatial group."""
    import scanpy as sc
    ad = adata.copy()
    cols = []
    for term, genes in gene_sets.items():
        valid = [g for g in genes if g in ad.var_names]
        if len(valid) < 2:
            continue
        col = f'score_{term}'
        sc.tl.score_genes(ad, gene_list=valid, score_name=col, use_raw=False)
        cols.append(col)
    if not cols:
        raise ValueError('No gene sets had >=2 genes present in adata.var_names.')
    return ad.obs.groupby(groupby, observed=True)[cols].mean()

def ora_enrichr(marker_genes: list[str], *, gene_sets: str | dict[str, list[str]]='MSigDB_Hallmark_2020', background: list[str] | None=None, fdr: float=0.05) -> pd.DataFrame:
    """Run over-representation analysis (ORA) on domain marker genes via GSEApy Enrichr."""
    import gseapy as gp
    kwargs = {'gene_list': marker_genes, 'gene_sets': gene_sets, 'outdir': None}
    if background is not None:
        kwargs['background'] = background
    enr = gp.enrichr(**kwargs)
    df = enr.results.copy()
    return df[df['Adjusted P-value'] <= fdr].reset_index(drop=True)
