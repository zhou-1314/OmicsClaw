"""Lightweight kernel helpers for bulkrna-cosinor-rhythm (24-hour Cosinor OLS fit)."""
from __future__ import annotations
__all__ = ['parse_timecourse_columns', 'fit_cosinor_24h', 'summarize_rhythmic_genes']

def parse_timecourse_columns(columns: pd.Index | list[str]) -> tuple[list[str], np.ndarray]:
    """Extract valid T{hour}_R{rep} column names and their numeric hour vector."""
    import re, numpy as np
    _COL_RE = re.compile('^T(\\d+(?:\\.\\d+)?)_R(\\d+)$')
    valid_cols, hours = ([], [])
    for c in columns:
        m = _COL_RE.match(str(c).strip())
        if m:
            valid_cols.append(c)
            hours.append(float(m.group(1)))
    if len(valid_cols) < 3:
        raise ValueError('At least 3 valid T{hour}_R{rep} columns are required for 24h cosinor fitting.')
    return (valid_cols, np.asarray(hours, dtype=float))

def fit_cosinor_24h(timecourse: pd.DataFrame, *, r2_cutoff: float=0.8, rel_amp_cutoff: float=0.2) -> pd.DataFrame:
    """Fit deterministic 24-hour single-component cosinor OLS per gene."""
    import numpy as np, pandas as pd
    from scipy import stats
    df = timecourse.loc[~timecourse.index.duplicated(keep='first')].copy()
    valid_cols, _ = parse_timecourse_columns(df.columns)
    sub = df[valid_cols].dropna(axis=1, thresh=int(np.ceil(0.8 * len(df))))
    valid_cols, hours = parse_timecourse_columns(sub.columns)
    Y = sub[valid_cols].to_numpy(dtype=float)
    omega = 2.0 * np.pi * hours / 24.0
    X = np.column_stack([np.ones_like(hours), np.cos(omega), np.sin(omega)])
    betas, _, _, _ = np.linalg.lstsq(X, Y.T, rcond=None)
    mesor, b_cos, b_sin = (betas[0], betas[1], betas[2])
    amp = np.hypot(b_cos, b_sin)
    rel_amp = amp / np.maximum(np.abs(mesor), 1e-08)
    acrophase_hr = np.mod(np.arctan2(b_sin, b_cos), 2.0 * np.pi) * (24.0 / (2.0 * np.pi))
    ss_res = np.sum((Y - (X @ betas).T) ** 2, axis=1)
    ss_tot = np.sum((Y - Y.mean(axis=1, keepdims=True)) ** 2, axis=1)
    r2 = np.where(ss_tot > 1e-12, np.clip(1.0 - ss_res / ss_tot, 0.0, 1.0), 0.0)
    dof = max(len(hours) - 3, 1)
    f_stat = np.where(ss_res > 1e-12, (ss_tot - ss_res) / 2.0 / (ss_res / dof), 0.0)
    return pd.DataFrame({'gene': df.index, 'mesor': mesor, 'beta_cos': b_cos, 'beta_sin': b_sin, 'amplitude': amp, 'relative_amplitude': rel_amp, 'acrophase_hr': acrophase_hr, 'r_squared': r2, 'pvalue': stats.f.sf(f_stat, 2, dof), 'is_rhythmic': (r2 >= r2_cutoff) & (rel_amp >= rel_amp_cutoff)}).set_index('gene')

def summarize_rhythmic_genes(result: pd.DataFrame) -> dict[str, int]:
    """Summarize total tested and rhythmic genes."""
    return {'n_tested': int(len(result)), 'n_rhythmic': int(result['is_rhythmic'].sum())}
