"""Lightweight kernel helpers for metabolomics-quantification (min/median/KNN imputation & TIC/median/log)."""
from __future__ import annotations
__all__ = ['quantify', 'quantification_summary']

def quantify(data: pd.DataFrame, *, impute: str='min', normalize: str='tic') -> pd.DataFrame:
    """Impute missing (0/NaN) metabolomics intensities and apply sample normalization."""
    import numpy as np, pandas as pd
    from sklearn.impute import KNNImputer
    _META_EXCLUDE = {'feature_id', 'mz', 'rt', 'name', 'id'}

    def _detect_sample_cols(df: pd.DataFrame) -> list[str]:
        pref = [c for c in df.columns if str(c).lower().startswith(('sample', 'intensity'))]
        if pref:
            return pref
        return [c for c in df.select_dtypes(include=[np.number]).columns if str(c).lower() not in _META_EXCLUDE]
    cols = _detect_sample_cols(data)
    if not cols:
        raise ValueError('No numeric sample/intensity columns found.')
    mat = data[cols].astype(float).mask(lambda x: x <= 0.0, np.nan)
    if (mat.notna().sum(axis=0) == 0).any():
        raise ValueError('Every sample column must have at least one positive observed intensity.')
    n_missing = int(mat.isna().sum().sum())
    imp = impute.lower()
    if imp == 'min':
        mat = mat.fillna(float(np.nanmin(mat.to_numpy())) * 0.5)
    elif imp == 'median':
        mat = mat.apply(lambda s: s.fillna(s.median()), axis=0)
    elif imp == 'knn':
        k = min(5, max(1, len(mat) - 1))
        arr = KNNImputer(n_neighbors=k).fit_transform(mat.to_numpy())
        mat = pd.DataFrame(arr, index=mat.index, columns=mat.columns)
    else:
        raise ValueError(f'Unknown imputation method: {impute}')
    norm = normalize.lower()
    if norm == 'tic':
        sums = mat.sum(axis=0)
        mat = mat.div(sums, axis=1) * float(sums.median())
    elif norm == 'median':
        meds = mat.median(axis=0)
        mat = mat.div(meds, axis=1) * float(meds.median())
    elif norm == 'log':
        mat = np.log2(mat.clip(lower=0.0) + 1.0)
    else:
        raise ValueError(f'Unknown normalization method: {normalize}')
    out = data.copy()
    out[cols] = mat
    out.attrs['run_info'] = {'impute': imp, 'normalize': norm, 'n_features': int(len(out)), 'n_samples': len(cols), 'n_missing_imputed': n_missing}
    return out

def quantification_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of missing values imputed and normalization method."""
    return table.attrs.get('run_info') or {'n_features': int(len(table))}
