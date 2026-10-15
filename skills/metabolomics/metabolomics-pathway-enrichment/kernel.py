"""Lightweight kernel helpers for metabolomics-pathway-enrichment (Hypergeometric ORA + BH-FDR)."""
from __future__ import annotations
__all__ = ['enrich', 'enrichment_summary']

def enrich(data: Iterable[str], *, method: str='ora', pathways: dict[str, dict] | None=None) -> pd.DataFrame:
    """Run hypergeometric Over-Representation Analysis (ORA) on metabolite names against pathways."""
    import numpy as np, pandas as pd
    from scipy import stats
    if method != 'ora':
        raise ValueError(f"Unsupported method '{method}'. Only 'ora' is implemented.")
    if not pathways:
        raise ValueError("Explicit 'pathways' mapping (pathway -> {'kegg_id': ..., 'metabolites': [...]}) is required.")
    query_raw = {str(x).strip().lower() for x in data if str(x).strip()}
    norm_pw = {}
    for pw_name, info in pathways.items():
        members = info['metabolites'] if isinstance(info, dict) else info
        kegg_id = info.get('kegg_id', '') if isinstance(info, dict) else ''
        norm_pw[pw_name] = (kegg_id, {str(m).strip().lower(): str(m).strip() for m in members if str(m).strip()})
    universe = set().union(*(m_map.keys() for _, m_map in norm_pw.values()))
    query = query_raw & universe
    M, N = (max(len(universe), 1), len(query))
    rows = []
    for pw_name, (kegg_id, m_map) in norm_pw.items():
        hit_keys = sorted(query & set(m_map.keys()))
        k, n = (len(hit_keys), len(m_map))
        if k == 0:
            continue
        exp = N * (n / M)
        pval = float(stats.hypergeom.sf(k - 1, M, n, N))
        rows.append({'pathway': pw_name, 'kegg_id': kegg_id, 'hits': k, 'pathway_size': n, 'expected': exp, 'fold_enrichment': float(k / max(exp, 1e-09)), 'pvalue': pval, 'matched_metabolites': ';'.join((m_map[h] for h in hit_keys))})
    cols = ['pathway', 'kegg_id', 'hits', 'pathway_size', 'expected', 'fold_enrichment', 'pvalue', 'fdr', 'matched_metabolites']
    if not rows:
        out = pd.DataFrame(columns=cols)
    else:
        out = pd.DataFrame(rows)
        p = out['pvalue'].to_numpy(dtype=float)
        order = np.argsort(p)
        fdr = np.empty(len(p), dtype=float)
        fdr[order] = np.minimum.accumulate((p[order] * len(p) / np.arange(1, len(p) + 1))[::-1])[::-1]
        out['fdr'] = np.clip(fdr, 0.0, 1.0)
        out = out[cols].sort_values(['fdr', 'pvalue']).reset_index(drop=True)
    out.attrs['run_info'] = {'method': method, 'n_query': len(query_raw), 'n_background': len(universe), 'n_enriched_rows': int(len(out))}
    return out

def enrichment_summary(table: pd.DataFrame, *, alpha: float=0.05) -> dict:
    """Return dictionary summary of enriched metabolic pathways."""
    sig = table[table['fdr'] < alpha] if 'fdr' in table.columns else table.iloc[0:0]
    return {'n_pathways_with_hits': int(len(table)), 'n_significant': int(len(sig))}
