"""Lightweight kernel helpers for metabolomics-statistics (ttest/wilcoxon/anova/kruskal + BH-FDR)."""
from __future__ import annotations
__all__ = ['test_groups', 'significant_table']

def test_groups(data: pd.DataFrame, *, method: str='ttest', alpha: float=0.05, group1_prefix: str | None=None, group2_prefix: str | None=None, group1_cols: list[str] | None=None, group2_cols: list[str] | None=None) -> pd.DataFrame:
    """Run two-group univariate test (ttest, wilcoxon, anova, kruskal) with BH-FDR adjustment."""
    import numpy as np, pandas as pd
    from scipy import stats

    def _resolve_groups(df: pd.DataFrame, g1_pref, g2_pref, g1_cols, g2_cols) -> tuple[list[str], list[str]]:
        if (g1_cols is None) ^ (g2_cols is None) or (g1_pref is None) ^ (g2_pref is None):
            raise ValueError('Supply both group1 and group2 selectors together.')
        num_cols = list(df.select_dtypes(include=[np.number]).columns)
        if g1_cols is not None and g2_cols is not None:
            c1, c2 = (list(g1_cols), list(g2_cols))
        elif g1_pref is not None and g2_pref is not None:
            c1 = [c for c in num_cols if str(c).startswith(g1_pref)]
            c2 = [c for c in num_cols if str(c).startswith(g2_pref)]
        else:
            mid = len(num_cols) // 2
            c1, c2 = (num_cols[:mid], num_cols[mid:])
        if not c1 or not c2 or set(c1) & set(c2):
            raise ValueError('Resolved sample groups must be non-empty and disjoint.')
        return (c1, c2)
    c1, c2 = _resolve_groups(data, group1_prefix, group2_prefix, group1_cols, group2_cols)
    a, b = (data[c1].to_numpy(dtype=float), data[c2].to_numpy(dtype=float))
    m1, m2 = (np.nanmean(a, axis=1), np.nanmean(b, axis=1))
    lfc = np.log2(np.clip(m2, 1e-12, None) / np.clip(m1, 1e-12, None))
    m = method.lower()
    if m == 'ttest':
        st, pv = stats.ttest_ind(b, a, axis=1, equal_var=False, nan_policy='omit')
    elif m in {'wilcoxon', 'anova', 'kruskal'}:
        fn = {'wilcoxon': stats.ranksums, 'anova': stats.f_oneway, 'kruskal': stats.kruskal}[m]
        res = [fn(b[i, ~np.isnan(b[i])], a[i, ~np.isnan(a[i])]) for i in range(len(data))]
        st, pv = (np.array([r.statistic for r in res], dtype=float), np.array([r.pvalue for r in res], dtype=float))
    else:
        raise ValueError(f'Unsupported method: {method}')
    p = np.nan_to_num(pv, nan=1.0)
    n = len(p)
    order = np.argsort(p)
    fdr = np.empty(n, dtype=float)
    if n > 0:
        fdr[order] = np.minimum.accumulate((p[order] * n / np.arange(1, n + 1))[::-1])[::-1]
    fdr = np.clip(fdr, 0.0, 1.0)
    out = pd.DataFrame({'mean_group1': m1, 'mean_group2': m2, 'log2fc': lfc, 'statistic': np.nan_to_num(st, nan=0.0), 'pvalue': p, 'fdr': fdr, 'significant': fdr < alpha}, index=data.index)
    out.attrs['run_info'] = {'method': m, 'alpha': float(alpha), 'group1_cols': c1, 'group2_cols': c2, 'n_significant': int((fdr < alpha).sum())}
    return out

def significant_table(results: pd.DataFrame, *, alpha: float=0.05) -> pd.DataFrame:
    """Return subset of statistically significant features (fdr < alpha)."""
    return results[results['fdr'] < alpha].copy()
