"""Lightweight kernel helpers for metabolomics-normalization (Median, PQN, TIC, Quantile & Log2)."""
from __future__ import annotations
__all__ = ['normalize', 'normalization_summary']

def normalize(data: pd.DataFrame, *, method: str='median') -> pd.DataFrame:
    """Normalize a feature-by-sample metabolomics DataFrame while preserving NaNs."""
    import numpy as np, pandas as pd
    df = data.copy().astype(float)
    m = method.lower()
    if m == 'median':
        col_med = df.median(axis=0).replace(0, np.nan)
        target = float(col_med.median())
        out = df.div(col_med, axis=1) * target
    elif m == 'total':
        col_sum = df.sum(axis=0, min_count=1).replace(0, np.nan)
        target = float(col_sum.median())
        out = df.div(col_sum, axis=1) * target
    elif m == 'pqn':
        ref = df.median(axis=1).replace(0, np.nan)
        quotients = df.div(ref, axis=0).median(axis=0).replace(0, np.nan)
        out = df.div(quotients, axis=1)
    elif m == 'log':
        out = np.log2(df.clip(lower=0.0) + 1.0)
    elif m == 'quantile':
        ranks = df.rank(method='min', na_option='keep')
        sorted_df = pd.DataFrame(np.sort(df.to_numpy(), axis=0), index=df.index, columns=df.columns)
        mean_quantiles = sorted_df.mean(axis=1).to_numpy()
        out = ranks.apply(lambda s: s.map(lambda r: mean_quantiles[int(r) - 1] if pd.notna(r) else np.nan))
    else:
        raise ValueError(f'Unsupported normalization method: {method}')
    out.attrs['run_info'] = {'method': m, 'n_features': int(df.shape[0]), 'n_samples': int(df.shape[1])}
    return out

def normalization_summary(data: pd.DataFrame) -> dict:
    """Return dictionary summary of normalization method and matrix dimensions."""
    return data.attrs.get('run_info') or {'n_features': int(data.shape[0]), 'n_samples': int(data.shape[1])}
