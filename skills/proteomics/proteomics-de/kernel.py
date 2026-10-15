"""Lightweight kernel helpers for proteomics-de (Welch/Wilcoxon two-group DE & BH-FDR)."""
from __future__ import annotations
__all__ = ['differential_abundance', 'significant', 'de_summary']

def differential_abundance(data: pd.DataFrame, *, group1: list[str] | None=None, group2: list[str] | None=None, method: str='ttest') -> pd.DataFrame:
    """Two-group differential protein abundance on log2 intensities with BH-FDR."""
    import numpy as np, pandas as pd
    from scipy import stats

    def _bh_fdr(pvals: np.ndarray) -> np.ndarray:
        p = np.nan_to_num(np.asarray(pvals, dtype=float), nan=1.0)
        n = len(p)
        if n == 0:
            return p
        order = np.argsort(p)
        fdr = np.empty(n, dtype=float)
        fdr[order] = np.minimum.accumulate((p[order] * n / np.arange(1, n + 1))[::-1])[::-1]
        return np.clip(fdr, 0.0, 1.0)
    df = data.set_index(data.columns[0]) if not np.issubdtype(data.iloc[:, 0].dtype, np.number) else data.copy()
    num_cols = list(df.select_dtypes(include=[np.number]).columns)
    g1 = group1 or [c for c in num_cols if c.lower().startswith(('ctrl', 'control', 'g1'))] or num_cols[:len(num_cols) // 2]
    g2 = group2 or [c for c in num_cols if c.lower().startswith(('treat', 'case', 'g2'))] or [c for c in num_cols if c not in g1]
    mat = df[g1 + g2].replace(0, np.nan).astype(float)
    if np.nanmax(mat.to_numpy()) > 50.0:
        mat = np.log2(mat)
    a, b = (mat[g1].to_numpy(), mat[g2].to_numpy())
    lfc = np.nanmean(b, axis=1) - np.nanmean(a, axis=1)
    if method == 'wilcoxon':
        res = [stats.ranksums(b[i, ~np.isnan(b[i])], a[i, ~np.isnan(a[i])]) for i in range(len(mat))]
        stat_v, pvals = (np.array([r.statistic for r in res]), np.array([r.pvalue for r in res]))
    else:
        stat_v, pvals = stats.ttest_ind(b, a, axis=1, equal_var=False, nan_policy='omit')
    fdr = _bh_fdr(pvals)
    out = pd.DataFrame({'protein_id': mat.index, 'mean_group1': np.nanmean(a, axis=1), 'mean_group2': np.nanmean(b, axis=1), 'log2fc': lfc, 'stat': stat_v, 'pvalue': np.nan_to_num(pvals, nan=1.0), 'fdr': fdr})
    out.attrs['run_info'] = {'method': method, 'group1': g1, 'group2': g2, 'n_proteins': int(len(out))}
    return out

def significant(results: pd.DataFrame, *, alpha: float=0.05, log2fc_threshold: float=0.0) -> pd.DataFrame:
    """Filter differential abundance results by BH-FDR alpha and absolute log2 fold change."""
    return results[(results['fdr'] < alpha) & (results['log2fc'].abs() >= log2fc_threshold)].reset_index(drop=True)

def de_summary(results: pd.DataFrame, *, alpha: float=0.05, log2fc_threshold: float=1.0) -> dict:
    """Return summary dictionary of tested and up/down regulated proteins."""
    sig = significant(results, alpha=alpha, log2fc_threshold=log2fc_threshold)
    return {'n_tested': int(len(results)), 'n_significant': int(len(sig)), 'n_up': int((sig['log2fc'] > 0).sum()), 'n_down': int((sig['log2fc'] < 0).sum())}
