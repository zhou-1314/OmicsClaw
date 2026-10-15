"""Lightweight kernel helpers for bulkrna-survival (Kaplan-Meier estimator & stratified log-rank screen)."""
from __future__ import annotations
__all__ = ['kaplan_meier_curve', 'stratified_logrank_screen']

def kaplan_meier_curve(time: pd.Series | np.ndarray, event: pd.Series | np.ndarray) -> pd.DataFrame:
    """Compute non-parametric product-limit Kaplan-Meier survival curve S(t)."""
    import numpy as np, pandas as pd
    t, e = (np.asarray(time, dtype=float), np.asarray(event, dtype=int))
    if (t < 0).any() or not set(np.unique(e)).issubset({0, 1}):
        raise ValueError('Survival time must be non-negative and event must be binary (0/1).')
    surv, rows = (1.0, [{'time': 0.0, 'n_at_risk': int(len(t)), 'n_events': 0, 'survival': 1.0}])
    for ti in np.sort(np.unique(t)):
        n_risk, d_i = (int(np.sum(t >= ti)), int(np.sum((t == ti) & (e == 1))))
        if n_risk > 0 and d_i > 0:
            surv *= 1.0 - d_i / n_risk
        rows.append({'time': float(ti), 'n_at_risk': n_risk, 'n_events': d_i, 'survival': float(surv)})
    return pd.DataFrame(rows)

def stratified_logrank_screen(expr: pd.DataFrame, *, clinical: pd.DataFrame, genes: list[str] | None=None) -> pd.DataFrame:
    """Screen genes by median expression split using the Mantel-Haenszel log-rank test and BH-FDR."""
    import numpy as np, pandas as pd
    from scipy import stats
    clin = clinical.set_index('sample') if 'sample' in clinical.columns else clinical.copy()
    shared = expr.columns.intersection(clin.index)
    if len(shared) < 4:
        raise ValueError('At least 4 shared samples between expression and clinical table are required.')
    sub_expr, sub_clin = (expr[shared], clin.loc[shared])
    t, e = (sub_clin['time'].to_numpy(dtype=float), sub_clin['event'].to_numpy(dtype=int))
    event_times = np.unique(t[e == 1])
    rows = []
    for g in list(expr.index) if genes is None else list(genes):
        x = sub_expr.loc[g].to_numpy(dtype=float)
        cutoff = float(np.median(x))
        high = x > cutoff if (x > cutoff).sum() >= 2 else x >= cutoff
        obs_h = exp_h = var_h = 0.0
        for ti in event_times:
            r1, r0 = (float(np.sum((t >= ti) & high)), float(np.sum((t >= ti) & ~high)))
            r, d1, d = (r1 + r0, float(np.sum((t == ti) & (e == 1) & high)), float(np.sum((t == ti) & (e == 1))))
            if r > 1:
                obs_h += d1
                exp_h += d * (r1 / r)
                var_h += r1 * r0 * d * (r - d) / (r * r * (r - 1.0))
        chi2 = (obs_h - exp_h) ** 2 / var_h if var_h > 1e-12 else 0.0
        rate_h = np.sum(e[high]) / max(np.sum(t[high]), 1e-08)
        rate_l = np.sum(e[~high]) / max(np.sum(t[~high]), 1e-08)
        rows.append({'gene': g, 'cutoff': cutoff, 'n_high': int(high.sum()), 'n_low': int((~high).sum()), 'logrank_stat': float(chi2), 'pvalue': float(stats.chi2.sf(chi2, df=1)), 'hazard_ratio': float((rate_h + 1e-06) / (rate_l + 1e-06))})
    df = pd.DataFrame(rows)
    pvals = df['pvalue'].to_numpy(dtype=float)
    m, order = (len(pvals), np.argsort(pvals))
    padj = np.empty(m, dtype=float)
    padj[order] = np.minimum.accumulate((pvals[order] * m / np.arange(1, m + 1))[::-1])[::-1]
    df['padj'] = np.clip(padj, 0.0, 1.0)
    return df.sort_values('pvalue').reset_index(drop=True)
