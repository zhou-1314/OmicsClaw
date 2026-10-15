"""Lightweight kernel helpers for bulkrna-deconvolution (NNLS cell-type proportion estimation)."""
from __future__ import annotations
__all__ = ['nnls_deconvolve', 'summarize_dominant_types']

def nnls_deconvolve(counts: pd.DataFrame, *, signature: pd.DataFrame) -> pd.DataFrame:
    """Estimate sample-by-cell-type proportions via Non-Negative Least Squares (NNLS)."""
    import numpy as np, pandas as pd
    from scipy.optimize import nnls
    shared = counts.index.intersection(signature.index)
    if len(shared) == 0:
        raise ValueError('No shared genes between bulk counts and signature matrix.')
    Y = counts.loc[shared].to_numpy(dtype=float)
    S = signature.loc[shared].to_numpy(dtype=float)
    if np.any(Y < 0) or np.any(S < 0):
        raise ValueError('Bulk expression and signature matrices must be non-negative (linear scale).')
    n_samples, n_types = (Y.shape[1], S.shape[1])
    props = np.zeros((n_samples, n_types), dtype=float)
    residuals = {}
    for j, sample in enumerate(counts.columns):
        w, rnorm = nnls(S, Y[:, j])
        total = float(w.sum())
        props[j] = w / total if total > 0 else np.full(n_types, 1.0 / n_types)
        residuals[str(sample)] = float(rnorm)
    out = pd.DataFrame(props, index=counts.columns, columns=signature.columns)
    out.attrs['residuals'] = residuals
    out.attrs['n_shared_genes'] = int(len(shared))
    return out

def summarize_dominant_types(proportions: pd.DataFrame) -> pd.DataFrame:
    """Return dominant cell type, maximum fraction, and NNLS residual per sample."""
    import numpy as np, pandas as pd
    residuals = proportions.attrs.get('residuals', {})
    return pd.DataFrame({'sample': proportions.index, 'dominant_cell_type': proportions.idxmax(axis=1).values, 'max_proportion': proportions.max(axis=1).values, 'nnls_residual': [residuals.get(str(s), np.nan) for s in proportions.index]}).set_index('sample')
