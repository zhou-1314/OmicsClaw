"""Lightweight kernel helpers for sc-enrichment (GSEApy Over-Representation Analysis & Preranked GSEA)."""
from __future__ import annotations
__all__ = ['run_ora', 'run_prerank_gsea', 'enrichment_summary']

def run_ora(gene_list: list[str], *, gene_sets: str | dict='MSigDB_Hallmark_2020', background: list[str] | int | None=None, cutoff: float=0.05) -> pd.DataFrame:
    """Run hypergeometric Over-Representation Analysis (ORA) via gseapy.enrich."""
    import gseapy as gp
    enr = gp.enrich(gene_list=list(gene_list), gene_sets=gene_sets, background=background, cutoff=cutoff, outdir=None)
    return enr.results.sort_values('Adjusted P-value', ascending=True).reset_index(drop=True)

def run_prerank_gsea(ranking_df: pd.DataFrame, *, gene_col: str='names', score_col: str='scores', gene_sets: str | dict='MSigDB_Hallmark_2020', min_size: int=5, max_size: int=500, permutation_num: int=100, seed: int=123) -> pd.DataFrame:
    """Run Preranked GSEA via gseapy.prerank on a deduplicated gene-by-score table."""
    import gseapy as gp
    rnk = ranking_df[[gene_col, score_col]].dropna().assign(_abs=lambda d: d[score_col].abs()).sort_values('_abs', ascending=False).drop_duplicates(subset=[gene_col]).sort_values(score_col, ascending=False)[[gene_col, score_col]]
    pre = gp.prerank(rnk=rnk, gene_sets=gene_sets, min_size=min_size, max_size=max_size, permutation_num=permutation_num, seed=seed, outdir=None, verbose=False)
    return pre.res2d.sort_values('FDR q-val', ascending=True).reset_index(drop=True)

def enrichment_summary(df: pd.DataFrame, *, fdr_col: str='Adjusted P-value', fdr: float=0.05) -> dict:
    """Summarize total tested gene sets and FDR-significant pathways."""
    col = fdr_col if fdr_col in df.columns else 'FDR q-val' if 'FDR q-val' in df.columns else df.columns[-1]
    sig = df[df[col].astype(float) < fdr] if not df.empty else df
    return {'n_tested': int(len(df)), 'n_significant': int(len(sig))}
