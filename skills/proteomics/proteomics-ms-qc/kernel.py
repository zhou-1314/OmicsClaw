"""Lightweight kernel helpers for proteomics-ms-qc (completeness & intensity CV QC)."""
from __future__ import annotations
__all__ = ['quality_control', 'sample_completeness']

def sample_completeness(data: pd.DataFrame) -> dict[str, float]:
    """Compute percentage of non-missing (>0 and non-NaN) proteins per sample column."""
    import numpy as np, pandas as pd
    _META_COLS = {'id', 'protein_id', 'mw', 'mol_weight', 'length', 'peptides', 'unique_peptides', 'razor_peptides', 'score', 'qvalue', 'q_value', 'q-value', 'pep', 'fdr', 'reverse', 'contaminant'}

    def _select_sample_cols(data: pd.DataFrame) -> list[str]:
        num_cols = list(data.select_dtypes(include=[np.number]).columns)
        filtered = [c for c in num_cols if c.lower() not in _META_COLS]
        cols = filtered or num_cols
        if len(data) == 0 or not cols:
            raise ValueError('Input table must contain at least one protein row and one numeric intensity column.')
        return cols
    cols = _select_sample_cols(data)
    mat = data[cols].replace(0, np.nan).astype(float)
    return {c: float(mat[c].notna().mean() * 100.0) for c in cols}

def quality_control(data: pd.DataFrame) -> pd.DataFrame:
    """Return a one-row QC summary DataFrame with missing rate and median/mean intensity CV."""
    import numpy as np, pandas as pd
    _META_COLS = {'id', 'protein_id', 'mw', 'mol_weight', 'length', 'peptides', 'unique_peptides', 'razor_peptides', 'score', 'qvalue', 'q_value', 'q-value', 'pep', 'fdr', 'reverse', 'contaminant'}

    def _select_sample_cols(data: pd.DataFrame) -> list[str]:
        num_cols = list(data.select_dtypes(include=[np.number]).columns)
        filtered = [c for c in num_cols if c.lower() not in _META_COLS]
        cols = filtered or num_cols
        if len(data) == 0 or not cols:
            raise ValueError('Input table must contain at least one protein row and one numeric intensity column.')
        return cols
    cols = _select_sample_cols(data)
    mat = data[cols].replace(0, np.nan).astype(float)
    miss_pct = float(mat.isna().mean().mean() * 100.0)
    row_mean = mat.mean(axis=1)
    row_std = mat.std(axis=1, ddof=1)
    cv = (row_std / row_mean.replace(0, np.nan) * 100.0).dropna()
    summary = {'n_proteins': int(len(mat)), 'n_samples': int(len(cols)), 'missing_rate_pct': miss_pct, 'completeness_pct': 100.0 - miss_pct, 'median_cv_pct': float(cv.median()) if len(cv) else 0.0, 'mean_cv_pct': float(cv.mean()) if len(cv) else 0.0}
    out = pd.DataFrame([summary])
    out.attrs['run_info'] = {**summary, 'sample_columns': cols, 'per_sample_completeness_pct': sample_completeness(data)}
    return out
