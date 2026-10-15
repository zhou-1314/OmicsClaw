"""Lightweight kernel helpers for bulkrna-de (PyDESeq2 / Welch t-test bulk RNA-seq DE)."""
from __future__ import annotations
__all__ = ['differential_expression', 'de_summary']

def differential_expression(counts: pd.DataFrame, *, control_cols: list[str], treat_cols: list[str], min_count: int=10) -> pd.DataFrame:
    """Two-group differential expression on log2(CPM+1) with BH-FDR adjustment."""
    import numpy as np, pandas as pd
    from scipy import stats
    sub = counts[control_cols + treat_cols]
    keep = sub.sum(axis=1) >= min_count
    sub = sub.loc[keep]
    cpm = sub.div(sub.sum(axis=0).replace(0, 1), axis=1) * 1000000.0
    log_expr = np.log2(cpm + 1.0)
    ctrl, treat = (log_expr[control_cols].values, log_expr[treat_cols].values)
    lfc = treat.mean(axis=1) - ctrl.mean(axis=1)
    _, pvals = stats.ttest_ind(treat, ctrl, axis=1, equal_var=False, nan_policy='omit')
    pvals = np.nan_to_num(pvals, nan=1.0)
    n = len(pvals)
    order = np.argsort(pvals)
    padj = np.empty(n, dtype=float)
    padj[order] = np.minimum.accumulate((pvals[order] * n / np.arange(1, n + 1))[::-1])[::-1]
    padj = np.clip(padj, 0.0, 1.0)
    return pd.DataFrame({'gene': sub.index, 'log2FoldChange': lfc, 'pvalue': pvals, 'padj': padj}).set_index('gene')

def de_summary(df: pd.DataFrame, *, padj_cutoff: float=0.05, lfc_cutoff: float=1.0) -> dict:
    """Summarize significant up/down regulated genes."""
    sig = df[(df['padj'] < padj_cutoff) & (df['log2FoldChange'].abs() >= lfc_cutoff)]
    return {'n_tested': int(len(df)), 'n_significant': int(len(sig)), 'n_up': int((sig['log2FoldChange'] > 0).sum()), 'n_down': int((sig['log2FoldChange'] < 0).sum())}
