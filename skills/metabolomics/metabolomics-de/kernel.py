"""Lightweight kernel helpers for metabolomics-de (two-group log2 fold change, Welch t-test & BH-FDR)."""
from __future__ import annotations
__all__ = ['differential_expression', 'significant_features', 'de_summary']

def differential_expression(data: pd.DataFrame, *, group_a_prefix: str='ctrl', group_b_prefix: str='treat') -> pd.DataFrame:
    """Compute two-group metabolomics log2 fold change, Welch t-test, and BH-FDR."""
    import numpy as np, pandas as pd
    from scipy import stats
    df = data.set_index(data.columns[0]) if not np.issubdtype(data.iloc[:, 0].dtype, np.number) else data.copy()
    cols_a = [c for c in df.columns if str(c).startswith(group_a_prefix)]
    cols_b = [c for c in df.columns if str(c).startswith(group_b_prefix)]
    if not cols_a or not cols_b or set(cols_a) & set(cols_b):
        raise ValueError(f"Groups '{group_a_prefix}' and '{group_b_prefix}' must be non-empty and disjoint.")
    mat_a = np.log2(np.clip(df[cols_a].to_numpy(dtype=float), 0.0, None) + 1.0)
    mat_b = np.log2(np.clip(df[cols_b].to_numpy(dtype=float), 0.0, None) + 1.0)
    lfc = np.nanmean(mat_b, axis=1) - np.nanmean(mat_a, axis=1)
    t_stat, pvals = stats.ttest_ind(mat_b, mat_a, axis=1, equal_var=False, nan_policy='omit')
    p = np.nan_to_num(pvals, nan=1.0)
    n = len(p)
    order = np.argsort(p)
    fdr = np.empty(n, dtype=float)
    if n > 0:
        fdr[order] = np.minimum.accumulate((p[order] * n / np.arange(1, n + 1))[::-1])[::-1]
    out = pd.DataFrame({'feature_id': df.index, 'mean_a': np.nanmean(mat_a, axis=1), 'mean_b': np.nanmean(mat_b, axis=1), 'log2fc': lfc, 't_stat': np.nan_to_num(t_stat, nan=0.0), 'pvalue': p, 'fdr': np.clip(fdr, 0.0, 1.0)})
    out.attrs['run_info'] = {'group_a_cols': cols_a, 'group_b_cols': cols_b, 'n_features': int(len(out))}
    return out

def significant_features(results: pd.DataFrame, *, alpha: float=0.05, lfc_threshold: float=0.0) -> pd.DataFrame:
    """Filter differential metabolite table by FDR alpha and optional |log2fc| threshold."""
    return results[(results['fdr'] < alpha) & (results['log2fc'].abs() >= lfc_threshold)].reset_index(drop=True)

def de_summary(results: pd.DataFrame, *, alpha: float=0.05) -> dict:
    """Return dictionary summary of tested and significant differential metabolites."""
    sig = significant_features(results, alpha=alpha)
    return {'n_tested': int(len(results)), 'n_significant': int(len(sig))}
