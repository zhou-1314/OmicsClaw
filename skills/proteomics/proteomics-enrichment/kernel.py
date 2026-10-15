"""Lightweight kernel helpers for proteomics-enrichment (Fisher exact ORA with BH-FDR)."""
from __future__ import annotations
__all__ = ['enrich', 'enrichment_summary']

def enrich(genes: list[str], *, pathway_db: dict[str, list[str]] | None=None, background_size: int | None=None, method: str='ora') -> pd.DataFrame:
    """Over-representation analysis (ORA) via one-sided Fisher's exact test + BH-FDR."""
    import numpy as np, pandas as pd
    from scipy import stats
    if not pathway_db:
        raise ValueError('pathway_db mapping (pathway -> list of gene/protein IDs) is required.')
    if method != 'ora':
        raise ValueError(f"Unsupported method: {method}. Only 'ora' is implemented.")
    query = {str(g).strip().upper() for g in genes if str(g).strip()}
    norm_db = {k: {str(m).strip().upper() for m in v if str(m).strip()} for k, v in pathway_db.items()}
    universe = set().union(*norm_db.values()) | query
    bg_n = max(int(background_size or len(universe) + 1), len(universe) + 1)
    rows = []
    for pw, members in norm_db.items():
        hits = sorted(query & members)
        a = len(hits)
        b = max(len(query) - a, 0)
        c = max(len(members) - a, 0)
        d = max(bg_n - a - b - c, 0)
        odds, pval = stats.fisher_exact([[a, b], [c, d]], alternative='greater')
        rows.append({'pathway': pw, 'overlap': a, 'pathway_size': len(members), 'odds_ratio': float(odds) if np.isfinite(odds) else 0.0, 'pvalue': float(pval), 'matched_genes': ';'.join(hits)})
    df = pd.DataFrame(rows)
    if len(df):
        p = df['pvalue'].to_numpy(dtype=float)
        n = len(p)
        order = np.argsort(p)
        fdr = np.empty(n, dtype=float)
        fdr[order] = np.minimum.accumulate((p[order] * n / np.arange(1, n + 1))[::-1])[::-1]
        df['fdr'] = np.clip(fdr, 0.0, 1.0)
        df = df.sort_values(['fdr', 'pvalue']).reset_index(drop=True)
    df.attrs['run_info'] = {'method': method, 'n_query': len(query), 'background_size': bg_n}
    return df

def enrichment_summary(table: pd.DataFrame, *, alpha: float=0.05) -> dict:
    """Summarize total tested pathways and FDR-significant pathways."""
    sig = table[table['fdr'] < alpha] if 'fdr' in table.columns else table.iloc[0:0]
    return {'n_pathways': int(len(table)), 'n_significant': int(len(sig))}
