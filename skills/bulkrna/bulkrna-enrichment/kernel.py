"""Lightweight kernel helpers for bulkrna-enrichment (hypergeometric ORA & GSEA prerank metric)."""
from __future__ import annotations
__all__ = ['hypergeometric_ora', 'build_prerank_series']

def hypergeometric_ora(query_genes: list[str] | set[str], *, gene_sets: dict[str, list[str]], background_genes: list[str] | set[str] | None=None) -> pd.DataFrame:
    """One-sided hypergeometric Over-Representation Analysis (ORA) with BH-FDR correction."""
    import numpy as np, pandas as pd
    from scipy import stats
    bg = set(background_genes) if background_genes is not None else {g for gs in gene_sets.values() for g in gs}
    q_set = set(query_genes) & bg
    M, n = (len(bg), len(q_set))
    if M == 0 or n == 0:
        return pd.DataFrame(columns=['term', 'overlap_count', 'term_size', 'fold_enrichment', 'pvalue', 'padj', 'overlap_genes'])
    rows = []
    for term, members in gene_sets.items():
        t_set = set(members) & bg
        N = len(t_set)
        if N == 0:
            continue
        overlap = sorted(q_set & t_set)
        k = len(overlap)
        pval = float(stats.hypergeom.sf(k - 1, M, N, n)) if k > 0 else 1.0
        rows.append({'term': term, 'overlap_count': k, 'term_size': N, 'fold_enrichment': float(k / n / (N / M)), 'pvalue': pval, 'overlap_genes': ';'.join(overlap)})
    df = pd.DataFrame(rows)
    if df.empty:
        df['padj'] = []
        return df
    pvals = df['pvalue'].to_numpy(dtype=float)
    m = len(pvals)
    order = np.argsort(pvals)
    padj = np.empty(m, dtype=float)
    padj[order] = np.minimum.accumulate((pvals[order] * m / np.arange(1, m + 1))[::-1])[::-1]
    df['padj'] = np.clip(padj, 0.0, 1.0)
    return df.sort_values(['padj', 'pvalue']).reset_index(drop=True)

def build_prerank_series(de_results: pd.DataFrame) -> pd.Series:
    """Build deduplicated signed -log10(pvalue) ranking Series for GSEA prerank."""
    import numpy as np
    df = de_results.copy()
    if 'gene' not in df.columns:
        df = df.reset_index().rename(columns={df.index.name or 'index': 'gene'})
    df = df.dropna(subset=['gene', 'log2FoldChange', 'pvalue'])
    df['rank_score'] = np.sign(df['log2FoldChange']) * -np.log10(df['pvalue'].clip(lower=1e-300))
    df = df.reindex(df['rank_score'].abs().sort_values(ascending=False).index).drop_duplicates('gene')
    return df.set_index('gene')['rank_score'].sort_values(ascending=False)
